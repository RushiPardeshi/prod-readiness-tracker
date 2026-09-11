"""Stage 1 — deterministic checks.

These checks intentionally favor precision over recall. Broad semantic cases
stay in judge/verify; this module handles file-presence checks, manifest
inspection, and narrow source patterns with concrete evidence.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, Callable, Iterable

from ..models import Check, Finding, Priority, RepoProfile


Detector = Callable[[Path, Check, RepoProfile], list[Finding]]

_LOCKFILES_BY_MANIFEST = {
    "package.json": ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb"),
    "pyproject.toml": ("poetry.lock", "uv.lock", "pdm.lock"),
    "requirements.txt": ("requirements.lock",),
    "go.mod": ("go.sum",),
    "Cargo.toml": ("Cargo.lock",),
    "Gemfile": ("Gemfile.lock",),
}

_RUNTIME_PIN_FILES = (
    ".python-version",
    ".nvmrc",
    ".node-version",
    ".tool-versions",
    "runtime.txt",
    "rust-toolchain",
    "rust-toolchain.toml",
)

_TEST_FILES = (
    "pytest.ini",
    "tox.ini",
    "noxfile.py",
    "jest.config.js",
    "jest.config.ts",
    "vitest.config.js",
    "vitest.config.ts",
)

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"][^'\"\n]{16,}['\"]"),
    re.compile(r"(?i)\b(?:postgres|mysql|mongodb|redis)://[^/\s:@]+:[^@\s]+@"),
)

_PRINT_LOG_PATTERNS = (
    re.compile(r"\bprint\s*\("),
    re.compile(r"\bconsole\.(?:log|debug|info|warn|error)\s*\("),
    re.compile(r"\bfmt\.Print(?:f|ln)?\s*\("),
)

_TIMEOUT_PATTERNS = (
    re.compile(r"\brequests\.(?:get|post|put|patch|delete|head)\s*\((?P<args>[^)]*)\)", re.DOTALL),
    re.compile(r"\bhttpx\.(?:get|post|put|patch|delete|head)\s*\((?P<args>[^)]*)\)", re.DOTALL),
    re.compile(r"\baxios\.(?:get|post|put|patch|delete)\s*\((?P<args>[^)]*)\)", re.DOTALL),
    re.compile(r"\bfetch\s*\((?P<args>[^)]*)\)", re.DOTALL),
)

_TEXT_SUFFIXES = {".py", ".js", ".ts", ".go", ".rs", ".rb", ".java", ".env", ".yaml", ".yml", ".toml", ".json"}

_DETECTORS: dict[str, Detector] = {}


def run(target: Path, checks: list[Check], profile: RepoProfile) -> list[Finding]:
    root = target.resolve()
    findings: list[Finding] = []
    for check in checks:
        if check.priority is not Priority.v1:
            continue
        detector = _DETECTORS.get(check.id)
        if detector is None:
            continue
        findings.extend(detector(root, check, profile))
    return findings


def _finding(check: Check, *, file: str, evidence: str, anchor: str | None = None) -> Finding:
    return Finding(
        check_id=check.id,
        severity=check.severity,
        confidence=1.0,
        file=file,
        anchor=anchor,
        evidence=evidence,
        rationale=check.rationale,
    )


def _detect_missing_lockfile(root: Path, check: Check, profile: RepoProfile) -> list[Finding]:
    _ = profile
    findings: list[Finding] = []
    for manifest, lockfiles in _LOCKFILES_BY_MANIFEST.items():
        if not (root / manifest).exists():
            continue
        if any((root / lockfile).exists() for lockfile in lockfiles):
            continue
        findings.append(
            _finding(
                check,
                file=manifest,
                evidence=f"{manifest} exists but none of {', '.join(lockfiles)} are committed",
            )
        )
    return findings


def _detect_floating_versions(root: Path, check: Check, profile: RepoProfile) -> list[Finding]:
    _ = profile
    findings: list[Finding] = []
    findings.extend(_floating_package_json(root, check))
    findings.extend(_floating_pyproject(root, check))
    findings.extend(_floating_requirements(root, check))
    return findings


def _floating_package_json(root: Path, check: Check) -> list[Finding]:
    data = _read_json(root / "package.json")
    if not data:
        return []
    findings: list[Finding] = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = data.get(section, {})
        if not isinstance(deps, dict):
            continue
        for name, version in sorted(deps.items()):
            if _is_floating_js_version(str(version)):
                findings.append(
                    _finding(
                        check,
                        file="package.json",
                        anchor=f"{section}.{name}",
                        evidence=f"{section}.{name} uses floating version {version!r}",
                    )
                )
    return findings


def _floating_pyproject(root: Path, check: Check) -> list[Finding]:
    data = _read_toml(root / "pyproject.toml")
    if not data:
        return []
    findings: list[Finding] = []
    for dep in _pyproject_dependency_entries(data):
        if _is_floating_python_dep(dep):
            findings.append(
                _finding(
                    check,
                    file="pyproject.toml",
                    anchor=_dependency_name(dep),
                    evidence=f"dependency uses floating version {dep!r}",
                )
            )
    return findings


def _floating_requirements(root: Path, check: Check) -> list[Finding]:
    path = root / "requirements.txt"
    if not path.exists():
        return []
    findings: list[Finding] = []
    for line in _read_text(path).splitlines():
        dep = line.strip()
        if not dep or dep.startswith("#") or dep.startswith(("-r ", "--")):
            continue
        if _is_floating_python_dep(dep):
            findings.append(
                _finding(
                    check,
                    file="requirements.txt",
                    anchor=_dependency_name(dep),
                    evidence=f"dependency uses floating version {dep!r}",
                )
            )
    return findings


def _detect_secrets(root: Path, check: Check, profile: RepoProfile) -> list[Finding]:
    _ = profile
    findings: list[Finding] = []
    for path in _iter_text_files(root):
        relative = _relative(path, root)
        for line_no, line in enumerate(_read_text(path).splitlines(), start=1):
            if any(pattern.search(line) for pattern in _SECRET_PATTERNS):
                findings.append(
                    _finding(
                        check,
                        file=relative,
                        anchor=f"line {line_no}",
                        evidence=_redact_secret_line(line),
                    )
                )
                break
    return findings


def _detect_no_ci(root: Path, check: Check, profile: RepoProfile) -> list[Finding]:
    _ = root
    if profile.has_ci:
        return []
    return [
        _finding(
            check,
            file=".",
            evidence="searched common CI config paths and found none",
        )
    ]


def _detect_missing_runtime_pin(root: Path, check: Check, profile: RepoProfile) -> list[Finding]:
    if any((root / pin).exists() for pin in _RUNTIME_PIN_FILES):
        return []
    if _manifest_runtime_pinned(root, profile.language):
        return []
    return [
        _finding(
            check,
            file=".",
            evidence="searched runtime pin files and manifest runtime declarations; found none",
        )
    ]


def _detect_unstructured_logging(root: Path, check: Check, profile: RepoProfile) -> list[Finding]:
    _ = profile
    findings: list[Finding] = []
    for path in _iter_source_files(root):
        relative = _relative(path, root)
        for line_no, line in enumerate(_read_text(path).splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//")):
                continue
            if any(pattern.search(stripped) for pattern in _PRINT_LOG_PATTERNS):
                findings.append(
                    _finding(
                        check,
                        file=relative,
                        anchor=f"line {line_no}",
                        evidence=stripped[:240],
                    )
                )
                break
    return findings


def _detect_no_test_suite(root: Path, check: Check, profile: RepoProfile) -> list[Finding]:
    _ = profile
    if _has_test_suite(root):
        return []
    return [
        _finding(
            check,
            file=".",
            evidence="searched test directories, test file patterns, and common test runner config; found none",
        )
    ]


def _detect_missing_io_timeouts(root: Path, check: Check, profile: RepoProfile) -> list[Finding]:
    _ = profile
    findings: list[Finding] = []
    for path in _iter_source_files(root):
        text = _read_text(path)
        relative = _relative(path, root)
        for pattern in _TIMEOUT_PATTERNS:
            for match in pattern.finditer(text):
                args = match.group("args")
                if _has_timeout_arg(args):
                    continue
                line_no = text[: match.start()].count("\n") + 1
                findings.append(
                    _finding(
                        check,
                        file=relative,
                        anchor=f"line {line_no}",
                        evidence=_single_line(match.group(0))[:240],
                    )
                )
    return findings


def _has_test_suite(root: Path) -> bool:
    if any((root / test_dir).is_dir() for test_dir in ("test", "tests", "__tests__")):
        return True
    if any((root / config).exists() for config in _TEST_FILES):
        return True
    patterns = ("test_*.py", "*_test.py", "*.test.js", "*.spec.js", "*.test.ts", "*.spec.ts", "*_test.go", "*_test.rs")
    return any(next(root.rglob(pattern), None) is not None for pattern in patterns)


def _manifest_runtime_pinned(root: Path, language: str) -> bool:
    if language == "javascript":
        data = _read_json(root / "package.json") or {}
        engines = data.get("engines", {})
        return isinstance(engines, dict) and any(engines.get(runtime) for runtime in ("node", "npm", "pnpm", "yarn"))
    if language == "python":
        data = _read_toml(root / "pyproject.toml") or {}
        return bool(data.get("project", {}).get("requires-python"))
    if language == "go":
        return "go " in _read_text(root / "go.mod")
    if language == "rust":
        return (root / "rust-toolchain").exists() or (root / "rust-toolchain.toml").exists()
    return False


def _pyproject_dependency_entries(data: dict[str, Any]) -> list[str]:
    entries = [str(dep) for dep in data.get("project", {}).get("dependencies", [])]
    optional = data.get("project", {}).get("optional-dependencies", {})
    if isinstance(optional, dict):
        for group in optional.values():
            entries.extend(str(dep) for dep in group)
    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    if isinstance(poetry_deps, dict):
        entries.extend(f"{name}{version}" for name, version in poetry_deps.items() if name.lower() != "python")
    return entries


def _is_floating_js_version(version: str) -> bool:
    value = version.strip()
    return (
        not value
        or value in {"*", "latest"}
        or value.startswith(("^", "~", ">", ">=", "<", "<="))
        or "x" in value.lower()
    )


def _is_floating_python_dep(dep: str) -> bool:
    value = dep.strip()
    if "@" in value:
        return False
    if "==" in value or "===" in value:
        return False
    return bool(value) and any(op in value for op in (">=", ">", "<=", "<", "~=", "*")) or not any(
        op in value for op in ("=", ">", "<", "~")
    )


def _dependency_name(dep: str) -> str:
    name = re.split(r"\s*(?:==|===|~=|>=|<=|>|<|@|\[)", dep.strip(), maxsplit=1)[0]
    return name or dep


def _has_timeout_arg(args: str) -> bool:
    return any(token in args for token in ("timeout", "signal", "AbortSignal", "context.WithTimeout", "context.WithDeadline"))


def _iter_source_files(root: Path) -> Iterable[Path]:
    return _iter_text_files(root, suffixes={".py", ".js", ".ts", ".go", ".rs", ".rb", ".java"})


def _iter_text_files(root: Path, suffixes: set[str] = _TEXT_SUFFIXES) -> Iterable[Path]:
    ignored_dirs = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".pytest_cache"}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if any(part in ignored_dirs for part in path.parts):
            continue
        yield path


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


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _single_line(text: str) -> str:
    return " ".join(text.split())


def _redact_secret_line(line: str) -> str:
    redacted = re.sub(r"(['\"])[^'\"]{8,}(['\"])", r"\1<redacted>\2", line.strip())
    redacted = re.sub(r"://([^/\s:@]+):([^@\s]+)@", r"://<user>:<redacted>@", redacted)
    return redacted[:240]


_DETECTORS = {
    "dependencies.missing-lockfile": _detect_missing_lockfile,
    "dependencies.floating-versions": _detect_floating_versions,
    "config.secrets-in-code": _detect_secrets,
    "build_release_run.no-ci": _detect_no_ci,
    "dev_prod_parity.missing-runtime-pin": _detect_missing_runtime_pin,
    "logs.unstructured-logging": _detect_unstructured_logging,
    "admin_processes.no-test-suite": _detect_no_test_suite,
    "resilience.missing-io-timeouts": _detect_missing_io_timeouts,
}
