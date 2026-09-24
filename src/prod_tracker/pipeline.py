"""Orchestrates the detection pipeline and returns a RunReport.

Wiring only. Stages 0-4 gate checks by archetype, group them by stage, and call
each detector (the detectors themselves do the real work; judge/verify are
still stubs — see detectors/). Stages 5 (score/gate), 6 (ledger upsert), and 7
(surface) live here per CLAUDE.md's repository layout note.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from .detectors import deterministic, dynamic, judge, verify
from .ledger import DebtItem, LedgerSummary, Status, init_db, upsert
from .llm import LLMConfig
from .models import Check, Finding, RepoProfile, Ruleset


class RunReport(BaseModel):
    profile: RepoProfile
    total_checks: int
    applicable: int
    skipped: int
    plan: dict[str, int] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    ledger: LedgerSummary = Field(default_factory=LedgerSummary)
    auto_comment: list[DebtItem] = Field(default_factory=list)
    backlog: list[DebtItem] = Field(default_factory=list)


def run(
    target: Path,
    ruleset: Ruleset,
    profile: RepoProfile,
    llm_config: LLMConfig | None = None,
    *,
    build_command: str | None = None,
    test_command: str | None = None,
    ledger_path: Path | str | None = None,
    repo: str | None = None,
    sha: str | None = None,
) -> RunReport:
    checks = ruleset.all_checks()
    applicable = [c for c in checks if c.applies(profile.archetype)]

    by_stage: dict[str, list[Check]] = {"deterministic": [], "dynamic": [], "judge": []}
    for c in applicable:
        by_stage[c.stage()].append(c)

    findings: list[Finding] = []
    findings += deterministic.run(target, by_stage["deterministic"], profile)
    findings += dynamic.run(
        target,
        by_stage["dynamic"],
        profile,
        build_command=build_command,
        test_command=test_command,
    )

    if llm_config is None or llm_config.enabled:
        judged = judge.run(target, by_stage["judge"], profile, llm_config)
        judged = verify.run(target, judged, profile, llm_config)  # stage 4 — adversarial verify
        findings += judged

    # Stage 6: ledger upsert. `evaluated_check_ids` deliberately excludes the
    # judge stage — judge.py is still a stub (item 7), and reconciling against
    # a check_id that wasn't really re-evaluated would falsely mark real debt
    # as resolved the moment it's silently skipped.
    ledger_summary = LedgerSummary()
    auto_comment: list[DebtItem] = []
    backlog: list[DebtItem] = []

    if ledger_path is not None:
        evaluated = deterministic.evaluated_check_ids(by_stage["deterministic"])
        evaluated |= dynamic.evaluated_check_ids(
            target,
            by_stage["dynamic"],
            profile,
            build_command=build_command,
            test_command=test_command,
        )

        conn = init_db(ledger_path)
        try:
            touched, ledger_summary = upsert(
                conn,
                findings,
                repo=repo or _detect_repo_name(target),
                evaluated_check_ids=evaluated,
                check_version=ruleset.version,
                sha=sha or _detect_sha(target),
            )
        finally:
            conn.close()

        # Stage 5 + 7: route only freshly-open items — anything a human already
        # triaged (acknowledged/wontfix/accepted) stays out of surfacing, and
        # resolved items don't belong in a "what's outstanding" view either.
        for item in touched:
            if item.status is not Status.open:
                continue
            destination = auto_comment if ruleset.routing.route(item.severity, item.confidence) == "auto_comment" else backlog
            destination.append(item)

    return RunReport(
        profile=profile,
        total_checks=len(checks),
        applicable=len(applicable),
        skipped=len(checks) - len(applicable),
        plan={k: len(v) for k, v in by_stage.items()},
        findings=findings,
        ledger=ledger_summary,
        auto_comment=auto_comment,
        backlog=backlog,
    )


def _git(target: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(["git", *args], cwd=target, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _detect_repo_name(target: Path) -> str:
    remote = _git(target, "remote", "get-url", "origin")
    if remote:
        name = remote.rstrip("/").rsplit("/", 1)[-1]
        return name[: -len(".git")] if name.endswith(".git") else name
    return target.resolve().name


def _detect_sha(target: Path) -> str:
    return _git(target, "rev-parse", "HEAD") or "unknown"
