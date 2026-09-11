"""Stage 4 — adversarial verify: try to REFUTE each judge finding.

Survivors only. Defaults to 'refuted' under uncertainty. This is the primary
anti-hallucination / false-positive defense — it's what keeps the tool from
inheriting an AI's confident-but-wrong claim.

TODO: for each finding, call the model with the check's `verify` instruction;
keep only findings the refutation pass fails to knock down.
"""

from __future__ import annotations

from pathlib import Path

from ..llm import LLMConfig
from ..models import Finding, RepoProfile


def run(
    target: Path,
    findings: list[Finding],
    profile: RepoProfile,
    llm_config: LLMConfig | None = None,
) -> list[Finding]:
    _ = (target, profile, llm_config)
    return findings
