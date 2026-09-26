"""Stage 3 — LLM judge for `judge`/`abs` checks.

Uses the configured LLM provider with structured output (the Finding schema)
and MANDATORY concrete code evidence citation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from ..llm import LLMConfig, config_from_env, generate_structured, is_available
from ..models import Check, Detection, Finding, Priority, RepoProfile

SUPPORTED_CHECK_IDS = {
    "stateless.in-memory-shared-state",
    "disposability.no-sigterm-handler",
    "resilience.data-race-shared-mutable-state",
    "resilience.missing-io-timeouts",
}

_IGNORED_DIR_NAMES = {".git", "node_modules", "dist", "build", "__pycache__", ".pytest_cache"}
_SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".go", ".rs", ".rb", ".java"}
_MAX_CONTEXT_FILES = 15
_MAX_CONTEXT_BYTES = 50_000


class CandidateFinding(BaseModel):
    check_id: str
    file: str
    anchor: str | None = None
    evidence: str
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    rationale: str | None = None


class JudgeOutput(BaseModel):
    findings: list[CandidateFinding] = Field(default_factory=list)


def evaluated_check_ids(checks: list[Check]) -> set[str]:
    """Check IDs this stage actually executes (v1 + implemented)."""
    return {c.id for c in checks if c.id in SUPPORTED_CHECK_IDS and (c.priority is None or c.priority is Priority.v1)}


def run(
    target: Path,
    checks: list[Check],
    profile: RepoProfile,
    llm_config: LLMConfig | None = None,
    client: Any | None = None,
) -> list[Finding]:
    config = llm_config or config_from_env()
    if not config.enabled:
        return []
    if not is_available(config, client=client):
        return []

    root = target.resolve()
    applicable_checks = [
        c for c in checks
        if c.id in SUPPORTED_CHECK_IDS and c.judge_prompt and c.applies(profile.archetype)
    ]
    if not applicable_checks:
        return []

    code_context = _gather_code_context(root, profile)
    if not code_context.strip():
        return []

    findings: list[Finding] = []
    for check in applicable_checks:
        findings.extend(_judge_check(root, check, profile, code_context, config, client))

    return findings


def _judge_check(
    root: Path,
    check: Check,
    profile: RepoProfile,
    code_context: str,
    config: LLMConfig,
    client: Any | None,
) -> list[Finding]:
    system_prompt = (
        "You are an expert production-readiness auditor analyzing code for specific operational defects.\n"
        "Analyze the provided code context and determine if it violates the specified check.\n\n"
        "Strict Requirements:\n"
        "1. Report a finding ONLY if you have concrete, verifiable code evidence directly visible in the code context.\n"
        "2. Do NOT report speculative, theoretical, or hallucinated issues.\n"
        "3. Every finding MUST cite:\n"
        "   - file: the exact relative file path where the issue occurs\n"
        "   - anchor: the enclosing function/class/symbol name (NOT a line number)\n"
        "   - evidence: exact quoted code snippet showing the violation (or for absence checks, specify the entrypoint inspected and what was missing)\n"
        "   - confidence: float between 0.0 and 1.0 reflecting certainty\n"
        "   - rationale: why this violates the check and the exact failure mode\n"
        "4. If no concrete violation exists, return an empty findings list.\n"
    )

    user_prompt = (
        f"Repository Archetype: {profile.archetype}\n"
        f"Language: {profile.language}\n"
        f"Entrypoints: {', '.join(profile.entrypoints) or 'none detected'}\n\n"
        f"Check to Evaluate:\n"
        f"ID: {check.id}\n"
        f"Title: {check.title}\n"
        f"Severity: {check.severity.value}\n"
        f"Rationale: {check.rationale or ''}\n\n"
        f"Evaluation Prompt:\n{check.judge_prompt}\n\n"
        f"Code Context:\n{code_context}\n"
    )

    try:
        output: JudgeOutput = generate_structured(
            config,
            system=system_prompt,
            user=user_prompt,
            response_model=JudgeOutput,
            client=client,
        )
    except Exception:
        # LLM call failure or refusal yields no findings rather than crashing
        return []

    validated: list[Finding] = []
    for candidate in output.findings:
        if candidate.check_id != check.id:
            continue
        finding = _validate_and_build_finding(root, check, candidate)
        if finding is not None:
            validated.append(finding)

    return validated


def _validate_and_build_finding(root: Path, check: Check, candidate: CandidateFinding) -> Finding | None:
    """Anti-hallucination barrier: verify cited file and evidence actually exist in code."""
    file_rel = Path(candidate.file.strip())
    file_abs = (root / file_rel).resolve()

    # Ensure cited file exists and is within target root
    try:
        file_abs.relative_to(root)
    except ValueError:
        return None

    if not file_abs.is_file():
        return None

    file_text = file_abs.read_text(encoding="utf-8", errors="ignore")

    # For non-absence checks, snippet citation is mandatory and must match file content
    if check.detection != Detection.abs:
        if not candidate.evidence or not candidate.evidence.strip():
            return None
        # Verify exact snippet or whitespace-normalized snippet
        norm_evidence = " ".join(candidate.evidence.split())
        norm_text = " ".join(file_text.split())
        if candidate.evidence.strip() not in file_text and norm_evidence not in norm_text:
            return None

    return Finding(
        check_id=check.id,
        severity=check.severity,
        confidence=min(1.0, max(0.0, candidate.confidence)),
        file=str(file_rel),
        anchor=candidate.anchor,
        evidence=candidate.evidence,
        rationale=candidate.rationale or check.rationale,
    )


def _gather_code_context(root: Path, profile: RepoProfile) -> str:
    """Collect source files for the judge, prioritizing entrypoints within token limits."""
    selected_files: list[Path] = []
    total_bytes = 0

    # 1. Add entrypoints first
    for ep in profile.entrypoints:
        ep_path = (root / ep.split(":", 1)[0]).resolve()
        if ep_path.is_file() and ep_path not in selected_files:
            selected_files.append(ep_path)
            total_bytes += ep_path.stat().st_size

    # 2. Add other source files
    for path in _iter_source_files(root):
        if len(selected_files) >= _MAX_CONTEXT_FILES or total_bytes >= _MAX_CONTEXT_BYTES:
            break
        if path not in selected_files:
            selected_files.append(path)
            total_bytes += path.stat().st_size

    chunks: list[str] = []
    for path in selected_files:
        try:
            rel = path.relative_to(root)
            content = path.read_text(encoding="utf-8", errors="ignore")
            chunks.append(f"--- File: {rel} ---\n{content}\n")
        except (OSError, ValueError):
            continue

    return "\n".join(chunks)


def _is_ignored_dir(name: str) -> bool:
    if name in _IGNORED_DIR_NAMES or name.endswith(".nosync"):
        return True
    return name == "venv" or name.startswith(".venv")


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in _SOURCE_EXTENSIONS:
            continue
        if any(_is_ignored_dir(part) for part in path.parts):
            continue
        yield path
