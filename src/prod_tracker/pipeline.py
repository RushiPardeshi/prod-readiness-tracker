"""Orchestrates the detection pipeline and returns a RunReport.

Wiring only: it gates checks by archetype, groups them by stage, and calls each
detector. The detectors themselves are stubs for now (see detectors/).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from .detectors import deterministic, dynamic, judge, verify
from .llm import LLMConfig
from .models import Check, Finding, RepoProfile, Ruleset


class RunReport(BaseModel):
    profile: RepoProfile
    total_checks: int
    applicable: int
    skipped: int
    plan: dict[str, int] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)


def run(
    target: Path,
    ruleset: Ruleset,
    profile: RepoProfile,
    llm_config: LLMConfig | None = None,
    *,
    build_command: str | None = None,
    test_command: str | None = None,
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

    # TODO: stage 5 (score/gate), stage 6 (ledger upsert), stage 7 (surface).

    return RunReport(
        profile=profile,
        total_checks=len(checks),
        applicable=len(applicable),
        skipped=len(checks) - len(applicable),
        plan={k: len(v) for k, v in by_stage.items()},
        findings=findings,
    )
