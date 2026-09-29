"""Pydantic models: the checks.yaml ruleset, the repo profile, and findings.

The ruleset models mirror checks.yaml (the source of truth). `Finding` doubles
as the schema the LLM judge is forced to emit via structured output.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    S1 = "S1"  # Critical
    S2 = "S2"  # High
    S3 = "S3"  # Medium
    S4 = "S4"  # Low


class Archetype(str, Enum):
    server = "server"
    worker = "worker"
    cron = "cron"
    cli = "cli"
    lib = "lib"


class Detection(str, Enum):
    det = "det"
    dyn = "dyn"
    judge = "judge"
    abs = "abs"


class Priority(str, Enum):
    v1 = "v1"
    v2 = "v2"


# detection class -> which pipeline stage runs the check
_STAGE = {
    Detection.det: "deterministic",
    Detection.dyn: "dynamic",
    Detection.judge: "judge",
    Detection.abs: "judge",
}


class Check(BaseModel):
    """One check definition from checks.yaml. Unknown keys are ignored."""

    id: str
    title: str
    severity: Severity
    detection: Detection
    applies_to: list[Archetype] = Field(default_factory=list)
    priority: Priority | None = None
    rationale: str | None = None
    signals: list[str] = Field(default_factory=list)
    judge_prompt: str | None = None
    verify: str | None = None
    command_hint: str | None = None
    remediation: str | None = None

    def stage(self) -> str:
        """Pipeline stage this check runs in (deterministic | dynamic | judge)."""
        return _STAGE[self.detection]

    def applies(self, archetype: str | None) -> bool:
        """Archetype gate. Unknown archetype -> can't gate, so include it."""
        if archetype is None or archetype == "unknown" or not self.applies_to:
            return True
        return archetype in {a.value for a in self.applies_to}


class Dimension(BaseModel):
    id: str
    factor: int | None = None  # None for the 13th (resilience) dimension
    title: str
    checks: list[Check] = Field(default_factory=list)


class RoutingConfig(BaseModel):
    """Stage 5 gate: how a scored finding gets surfaced. Mirrors checks.yaml `routing`."""

    auto_comment_severities: list[Severity] = Field(default_factory=lambda: [Severity.S1, Severity.S2])
    auto_comment_min_confidence: float = Field(default=0.8, ge=0.0, le=1.0)

    def route(self, severity: Severity, confidence: float) -> str:
        if severity in self.auto_comment_severities and confidence >= self.auto_comment_min_confidence:
            return "auto_comment"
        return "backlog"


class Ruleset(BaseModel):
    version: int
    dimensions: list[Dimension] = Field(default_factory=list)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)

    def all_checks(self) -> list[Check]:
        return [c for d in self.dimensions for c in d.checks]


class RepoProfile(BaseModel):
    path: str
    language: str = "unknown"
    archetype: str = "unknown"
    has_dockerfile: bool = False
    has_ci: bool = False
    entrypoints: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    """A single tracked violation; also the LLM judge's structured-output schema."""

    check_id: str
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    file: str
    anchor: str | None = None   # symbol/function, NOT a line number
    evidence: str               # snippet, or "searched X, found none"
    rationale: str | None = None
