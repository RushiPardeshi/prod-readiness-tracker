"""Stage 2 — dynamic checks: actually run build/test in CI. Ground truth.

Commands are auto-detected from the repo profile and manifest files, but can
be overridden per-run (CLI flag or env var — see cli.py) for repos whose
build/test entrypoint isn't a plain manifest script. Detection is intentionally
conservative: if no command can be determined confidently, the check is
skipped rather than guessed, since a wrong guess would fabricate a ground-truth
failure that isn't real.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Check, Finding, Priority, RepoProfile

_DEFAULT_TIMEOUT_SECONDS = 300
_OUTPUT_TAIL_CHARS = 2000

_JS_RUNNERS: tuple[tuple[str, str], ...] = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
)
_JS_BUILD_COMMANDS = {"npm": "npm run build", "yarn": "yarn build", "pnpm": "pnpm run build", "bun": "bun run build"}
_JS_TEST_COMMANDS = {"npm": "npm test", "yarn": "yarn test", "pnpm": "pnpm test", "bun": "bun test"}


@dataclass
class _CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def failed(self) -> bool:
        return self.timed_out or self.exit_code != 0


def run(
    target: Path,
    checks: list[Check],
    profile: RepoProfile,
    *,
    build_command: str | None = None,
    test_command: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> list[Finding]:
    root = target.resolve()
    by_id = {check.id: check for check in checks if check.priority is Priority.v1}
    findings: list[Finding] = []

    for check_id, command in plan(target, checks, profile, build_command=build_command, test_command=test_command).items():
        findings.extend(_check_command(root, by_id[check_id], command, timeout))

    return findings


def plan(
    target: Path,
    checks: list[Check],
    profile: RepoProfile,
    *,
    build_command: str | None = None,
    test_command: str | None = None,
) -> dict[str, str | None]:
    """Resolve a command per runnable check_id without executing anything.

    A value of None means the command could not be determined and the check
    will be skipped — used by the ledger to know which check_ids were genuinely
    attempted this run (a check with no resolvable command was NOT evaluated,
    so its prior findings must not be reconciled as resolved).
    """
    root = target.resolve()
    by_id = {check.id: check for check in checks if check.priority is Priority.v1}
    resolved: dict[str, str | None] = {}

    if "build_release_run.build-fails" in by_id:
        resolved["build_release_run.build-fails"] = build_command or _detect_build_command(root, profile)

    if "admin_processes.tests-failing" in by_id:
        resolved["admin_processes.tests-failing"] = test_command or _detect_test_command(root, profile)

    return resolved


def evaluated_check_ids(
    target: Path,
    checks: list[Check],
    profile: RepoProfile,
    *,
    build_command: str | None = None,
    test_command: str | None = None,
) -> set[str]:
    """check_ids that will actually run a command this run (resolvable command only)."""
    resolved = plan(target, checks, profile, build_command=build_command, test_command=test_command)
    return {check_id for check_id, command in resolved.items() if command}


def _check_command(root: Path, check: Check, command: str | None, timeout: int) -> list[Finding]:
    if not command:
        return []
    result = _run_command(command, root, timeout)
    if result is None or not result.failed:
        return []
    return [_finding(check, evidence=_failure_evidence(result, timeout))]


def _run_command(command: str, root: Path, timeout: int) -> _CommandResult | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts:
        return None
    try:
        completed = subprocess.run(parts, cwd=root, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as exc:
        return _CommandResult(
            command=command,
            exit_code=-1,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            timed_out=True,
        )
    return _CommandResult(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _failure_evidence(result: _CommandResult, timeout: int) -> str:
    status = f"timed out after {timeout}s" if result.timed_out else f"exited {result.exit_code}"
    tail = _tail(result.stderr) or _tail(result.stdout)
    evidence = f"`{result.command}` {status}"
    if tail:
        evidence += f": {tail}"
    return evidence


def _tail(text: str) -> str:
    return text.strip()[-_OUTPUT_TAIL_CHARS:]


def _finding(check: Check, *, evidence: str) -> Finding:
    return Finding(
        check_id=check.id,
        severity=check.severity,
        confidence=1.0,
        file=".",
        evidence=evidence,
        rationale=check.rationale,
    )


def _detect_build_command(root: Path, profile: RepoProfile) -> str | None:
    if profile.language == "python":
        pyproject = _read_toml(root / "pyproject.toml") or {}
        if "build-system" not in pyproject:
            return None
        return "uv build" if (root / "uv.lock").exists() else "python -m build"
    if profile.language == "javascript":
        pkg = _read_json(root / "package.json") or {}
        if "build" not in pkg.get("scripts", {}):
            return None
        return _JS_BUILD_COMMANDS[_js_runner(root)]
    if profile.language == "go" and (root / "go.mod").exists():
        return "go build ./..."
    if profile.language == "rust" and (root / "Cargo.toml").exists():
        return "cargo build --workspace"
    return None


def _detect_test_command(root: Path, profile: RepoProfile) -> str | None:
    if profile.language == "python":
        if not _has_python_tests(root):
            return None
        if (root / "uv.lock").exists():
            return "uv run pytest"
        if (root / "poetry.lock").exists():
            return "poetry run pytest"
        if (root / "Pipfile.lock").exists():
            return "pipenv run pytest"
        return "python -m pytest"
    if profile.language == "javascript":
        pkg = _read_json(root / "package.json") or {}
        if "test" not in pkg.get("scripts", {}):
            return None
        return _JS_TEST_COMMANDS[_js_runner(root)]
    if profile.language == "go" and (root / "go.mod").exists():
        return "go test ./..."
    if profile.language == "rust" and (root / "Cargo.toml").exists():
        return "cargo test --workspace"
    return None


def _has_python_tests(root: Path) -> bool:
    if any((root / directory).is_dir() for directory in ("tests", "test")):
        return True
    if any((root / name).exists() for name in ("pytest.ini", "tox.ini", "noxfile.py")):
        return True
    pyproject = _read_toml(root / "pyproject.toml") or {}
    if "pytest" in pyproject.get("tool", {}):
        return True
    return next(root.rglob("test_*.py"), None) is not None or next(root.rglob("*_test.py"), None) is not None


def _js_runner(root: Path) -> str:
    for lockfile, runner in _JS_RUNNERS:
        if (root / lockfile).exists():
            return runner
    return "npm"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_toml(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
