"""Stage 0 — build a deterministic RepoProfile for the target.

The profiler intentionally uses conservative repository-level evidence. It is
used for archetype gates, so uncertain cases should remain `unknown` instead of
guessing a long-running service.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from .models import RepoProfile


_LANGUAGE_SIGNALS: dict[str, tuple[str, ...]] = {
    "python": (
        "pyproject.toml",
        "requirements.txt",
        "requirements.in",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "uv.lock",
    ),
    "javascript": (
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "tsconfig.json",
    ),
    "go": ("go.mod", "go.sum"),
    "rust": ("Cargo.toml", "Cargo.lock"),
    "ruby": ("Gemfile", "Gemfile.lock"),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts", "gradle.lockfile"),
}

_CI_FILES = (
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    "Jenkinsfile",
    "bitbucket-pipelines.yml",
    ".drone.yml",
    ".buildkite/pipeline.yml",
)

_CONTAINER_FILES = (
    "Dockerfile",
    "Containerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    ".devcontainer/devcontainer.json",
)

_ENTRYPOINT_FILES = (
    "main.py",
    "app.py",
    "asgi.py",
    "wsgi.py",
    "manage.py",
    "server.js",
    "server.ts",
    "index.js",
    "index.ts",
    "app.js",
    "app.ts",
    "main.go",
    "src/main.rs",
)

_SERVER_DEPENDENCIES = {
    "fastapi",
    "flask",
    "django",
    "uvicorn",
    "gunicorn",
    "express",
    "fastify",
    "koa",
    "hapi",
    "next",
    "@nestjs/core",
    "spring-boot",
    "axum",
    "actix-web",
    "rocket",
}

_WORKER_DEPENDENCIES = {
    "celery",
    "rq",
    "dramatiq",
    "huey",
    "bull",
    "bullmq",
    "sidekiq",
}

_CLI_HINT_NAMES = {"cli.py", "__main__.py"}
_SERVER_IMPORT_MARKERS = ("fastapi", "flask", "django", "uvicorn", "express", "fastify", "koa")
_WORKER_IMPORT_MARKERS = ("celery", "rq", "dramatiq", "huey", "sidekiq")


def build_profile(path: Path) -> RepoProfile:
    target = path.resolve()
    if target.is_file():
        target = target.parent

    evidence: list[str] = []
    language = _detect_language(target, evidence)
    has_dockerfile = _has_any(target, _CONTAINER_FILES)
    has_ci = _has_ci(target)
    entrypoints = _detect_entrypoints(target, evidence)
    archetype = _detect_archetype(target, entrypoints, evidence)

    if has_dockerfile:
        evidence.append("container config detected")
    if has_ci:
        evidence.append("CI config detected")

    return RepoProfile(
        path=str(target),
        language=language,
        archetype=archetype,
        has_dockerfile=has_dockerfile,
        has_ci=has_ci,
        entrypoints=entrypoints,
        evidence=evidence,
    )


def _detect_language(path: Path, evidence: list[str]) -> str:
    scores: Counter[str] = Counter()
    for language, signals in _LANGUAGE_SIGNALS.items():
        for signal in signals:
            if (path / signal).exists():
                scores[language] += 2 if _is_manifest(signal) else 1
                evidence.append(f"{language} signal: {signal}")

    if not scores:
        return "unknown"

    [(language, top_score), *rest] = scores.most_common()
    if rest and rest[0][1] == top_score:
        evidence.append("language tie; leaving language unknown")
        return "unknown"
    return language


def _detect_entrypoints(path: Path, evidence: list[str]) -> list[str]:
    entrypoints: list[str] = []
    for relative in _ENTRYPOINT_FILES:
        if (path / relative).exists():
            entrypoints.append(relative)

    if (path / "cmd").is_dir():
        for main_go in sorted((path / "cmd").glob("*/main.go")):
            entrypoints.append(_relative(main_go, path))

    package_json = _read_package_json(path / "package.json")
    if package_json:
        for script_name, command in sorted(package_json.get("scripts", {}).items()):
            if script_name in {"start", "dev", "serve", "server", "worker", "cli"}:
                entrypoints.append(f"package.json:scripts.{script_name}={command}")
        for bin_name in _package_bins(package_json):
            entrypoints.append(f"package.json:bin.{bin_name}")

    pyproject = _read_pyproject(path / "pyproject.toml")
    if pyproject:
        for script_name in sorted(pyproject.get("project", {}).get("scripts", {})):
            entrypoints.append(f"pyproject.toml:project.scripts.{script_name}")

    entrypoints = _dedupe(entrypoints)
    if entrypoints:
        evidence.append("entrypoints detected: " + ", ".join(entrypoints))
    return entrypoints


def _detect_archetype(path: Path, entrypoints: list[str], evidence: list[str]) -> str:
    package_json = _read_package_json(path / "package.json") or {}
    pyproject = _read_pyproject(path / "pyproject.toml") or {}

    dependencies = set(_package_dependencies(package_json))
    dependencies.update(_pyproject_dependencies(pyproject))

    if dependencies & _WORKER_DEPENDENCIES or _source_contains(path, _WORKER_IMPORT_MARKERS):
        evidence.append("worker signal from dependency/import marker")
        return "worker"

    if _has_cron_config(path):
        evidence.append("cron schedule config detected")
        return "cron"

    if dependencies & _SERVER_DEPENDENCIES or _source_contains(path, _SERVER_IMPORT_MARKERS):
        evidence.append("server signal from dependency/import marker")
        return "server"

    if _has_server_script(package_json):
        evidence.append("server signal from package.json start/dev script")
        return "server"

    if _has_cli_script(pyproject, package_json) or any(Path(ep).name in _CLI_HINT_NAMES for ep in entrypoints):
        evidence.append("CLI signal from script metadata or conventional file")
        return "cli"

    if _looks_like_library(path, pyproject, package_json):
        evidence.append("library signal from package metadata/source layout")
        return "lib"

    return "unknown"


def _has_ci(path: Path) -> bool:
    workflows = path / ".github" / "workflows"
    if workflows.is_dir() and any(p.is_file() for p in workflows.iterdir()):
        return True
    return _has_any(path, _CI_FILES)


def _has_any(path: Path, relatives: tuple[str, ...]) -> bool:
    return any((path / relative).exists() for relative in relatives)


def _has_cron_config(path: Path) -> bool:
    workflows = path / ".github" / "workflows"
    return (
        (path / "crontab").exists()
        or workflows.is_dir()
        and any("schedule:" in _read_text(p) for p in workflows.glob("*.y*ml"))
    )


def _has_server_script(package_json: dict[str, Any]) -> bool:
    scripts = package_json.get("scripts", {})
    start = " ".join(str(scripts.get(name, "")) for name in ("start", "dev", "serve", "server"))
    return any(marker in start for marker in ("next", "vite", "node", "uvicorn", "gunicorn"))


def _has_cli_script(pyproject: dict[str, Any], package_json: dict[str, Any]) -> bool:
    if pyproject.get("project", {}).get("scripts"):
        return True
    return bool(package_json.get("bin"))


def _looks_like_library(path: Path, pyproject: dict[str, Any], package_json: dict[str, Any]) -> bool:
    if pyproject.get("project", {}).get("scripts") or package_json.get("bin"):
        return False
    return any((path / relative).exists() for relative in ("src", "lib", "__init__.py"))


def _package_dependencies(package_json: dict[str, Any]) -> set[str]:
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = package_json.get(key, {})
        if isinstance(section, dict):
            deps.update(section.keys())
    return deps


def _package_bins(package_json: dict[str, Any]) -> list[str]:
    bins = package_json.get("bin", {})
    if isinstance(bins, str):
        return [package_json.get("name", "bin")]
    if isinstance(bins, dict):
        return sorted(str(name) for name in bins)
    return []


def _pyproject_dependencies(pyproject: dict[str, Any]) -> set[str]:
    deps = {_normalize_python_dep(dep) for dep in pyproject.get("project", {}).get("dependencies", [])}
    optional = pyproject.get("project", {}).get("optional-dependencies", {})
    if isinstance(optional, dict):
        for group in optional.values():
            deps.update(_normalize_python_dep(dep) for dep in group)
    return {dep for dep in deps if dep}


def _normalize_python_dep(dep: object) -> str:
    value = str(dep).strip().lower()
    for separator in ("[", "=", "<", ">", "~", "!"):
        value = value.split(separator, 1)[0]
    return value


def _source_contains(path: Path, markers: tuple[str, ...]) -> bool:
    for source in _source_files(path):
        text = _read_text(source).lower()
        if any(_contains_marker(text, marker) for marker in markers):
            return True
    return False


def _contains_marker(text: str, marker: str) -> bool:
    escaped = re.escape(marker)
    return any(
        re.search(pattern, text) is not None
        for pattern in (
            rf"\bfrom\s+{escaped}\b",
            rf"\bimport\s+{escaped}\b",
            rf"\bfrom\s+['\"]{escaped}['\"]",
            rf"\brequire\(\s*['\"]{escaped}['\"]\s*\)",
        )
    )


def _source_files(path: Path) -> list[Path]:
    patterns = ("*.py", "*.js", "*.ts", "*.go", "*.rs", "*.rb", "*.java")
    files: list[Path] = []
    for pattern in patterns:
        files.extend(path.glob(pattern))
        src = path / "src"
        if src.is_dir():
            files.extend(src.rglob(pattern))
    return files[:100]


def _read_package_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_pyproject(path: Path) -> dict[str, Any] | None:
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


def _is_manifest(path: str) -> bool:
    return path in {
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "Gemfile",
        "pom.xml",
        "build.gradle",
    }


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
