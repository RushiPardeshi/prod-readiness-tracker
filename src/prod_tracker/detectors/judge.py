"""Stage 3 — LLM judge for `judge`/`abs` checks.

Uses the configured LLM provider with structured output (the Finding schema)
and MANDATORY evidence citation. Anthropic is the default provider; OpenAI is
kept available behind the same provider interface.
"""

from __future__ import annotations

from pathlib import Path

from ..llm import LLMConfig
from ..models import Check, Finding, RepoProfile


def run(
    target: Path,
    checks: list[Check],
    profile: RepoProfile,
    llm_config: LLMConfig | None = None,
) -> list[Finding]:
    _ = (target, checks, profile, llm_config)
    # TODO: call the configured provider with check.judge_prompt + diff/context,
    # force the Finding schema, and require an evidence location.
    return []
