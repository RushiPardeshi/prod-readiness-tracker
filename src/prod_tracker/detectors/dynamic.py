"""Stage 2 — dynamic checks: actually run build/test in CI. Ground truth.

TODO: execute each check's `command_hint`, capture the exit code, and emit a
Finding on failure (confidence 1.0).
"""

from __future__ import annotations

from pathlib import Path

from ..models import Check, Finding, RepoProfile


def run(target: Path, checks: list[Check], profile: RepoProfile) -> list[Finding]:
    return []
