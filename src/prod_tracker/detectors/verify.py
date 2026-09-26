"""Stage 4 — adversarial verify: try to REFUTE each judge finding.

Survivors only. Defaults to 'refuted' under uncertainty. This is the primary
anti-hallucination / false-positive defense — it's what keeps the tool from
inheriting an AI's confident-but-wrong claim.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..llm import LLMConfig, config_from_env, generate_structured, is_available
from ..models import Check, Finding, RepoProfile


class VerificationResult(BaseModel):
    refuted: bool = Field(description="True if the finding is refuted/invalid/mitigated; False if confirmed.")
    reason: str = Field(description="Detailed rationale for refuting or confirming the finding.")
    adjusted_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optional adjusted confidence if the finding survives refutation.",
    )


def run(
    target: Path,
    findings: list[Finding],
    profile: RepoProfile,
    llm_config: LLMConfig | None = None,
    *,
    checks: list[Check] | None = None,
    client: Any | None = None,
) -> list[Finding]:
    """Adversarially test each candidate finding to try to refute it."""
    if not findings:
        return []

    config = llm_config or config_from_env()
    if not config.enabled:
        return []
    if not is_available(config, client=client):
        # Uncertainty / missing credentials defaults to refuted
        return []

    root = target.resolve()
    checks_by_id = {c.id: c for c in (checks or [])}
    survivors: list[Finding] = []

    for finding in findings:
        check = checks_by_id.get(finding.check_id)
        if _verify_finding(root, finding, check, profile, config, client):
            survivors.append(finding)

    return survivors


def _verify_finding(
    root: Path,
    finding: Finding,
    check: Check | None,
    profile: RepoProfile,
    config: LLMConfig,
    client: Any | None,
) -> bool:
    """Returns True if finding survives refutation (i.e. is NOT refuted)."""
    verify_instruction = (check.verify if check and check.verify else None) or (
        "Try to refute: is this issue mitigated elsewhere, handled by a framework default, "
        "or a harmless non-issue? Default to refuted if uncertain."
    )

    file_path = (root / finding.file).resolve()
    file_context = ""
    if file_path.is_file():
        file_context = file_path.read_text(encoding="utf-8", errors="ignore")[:30_000]

    system_prompt = (
        "You are an adversarial verification auditor for production software.\n"
        "A previous analysis flagged a potential production defect. Your job is to try to REFUTE it.\n"
        "Look for mitigating code, enclosing safeguards, framework guarantees, or reasons why this is not a defect.\n\n"
        "CRITICAL RULES:\n"
        "1. Be skeptical of the finding. Try hard to knock it down.\n"
        "2. If the finding is refuted, mitigated, false-positive, or harmless: set refuted = true.\n"
        "3. DEFAULTS TO REFUTED UNDER UNCERTAINTY: If you are uncertain or the evidence is ambiguous, set refuted = true.\n"
        "4. ONLY set refuted = false if you can conclusively confirm that the finding is real, unmitigated, and critical.\n"
    )

    user_prompt = (
        f"Check ID: {finding.check_id}\n"
        f"Check Title: {check.title if check else finding.check_id}\n"
        f"Archetype: {profile.archetype}\n\n"
        f"Proposed Finding:\n"
        f"- File: {finding.file}\n"
        f"- Anchor: {finding.anchor or 'N/A'}\n"
        f"- Evidence: {finding.evidence}\n"
        f"- Rationale: {finding.rationale or 'N/A'}\n"
        f"- Proposed Confidence: {finding.confidence}\n\n"
        f"Refutation Instructions:\n{verify_instruction}\n\n"
        f"File Context ({finding.file}):\n"
        f"{file_context}\n"
    )

    try:
        result: VerificationResult = generate_structured(
            config,
            system=system_prompt,
            user=user_prompt,
            response_model=VerificationResult,
            client=client,
        )
    except Exception:
        # On error or uncertainty, default to refuted
        return False

    if result.refuted:
        return False

    # Finding survived refutation! Update confidence if model provided an adjusted score
    if result.adjusted_confidence is not None:
        finding.confidence = min(1.0, max(0.0, result.adjusted_confidence))

    return True
