from pathlib import Path

from prod_tracker import pipeline
from prod_tracker.llm import LLMConfig
from prod_tracker.models import Archetype
from prod_tracker.profiler import build_profile
from prod_tracker.ruleset import load_ruleset


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_python_server_fixture_archetype_and_gating(tmp_path):
    # Python FastAPI Server
    write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "api-service"
requires-python = "==3.12"
dependencies = ["fastapi==0.110.0", "uvicorn==0.28.0"]
""".strip(),
    )
    write(tmp_path / "uv.lock", "")
    write(tmp_path / ".python-version", "3.12\n")
    write(tmp_path / "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    write(tmp_path / "tests" / "test_api.py", "def test_api(): pass")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\non: push\njobs: {}\n")

    profile = build_profile(tmp_path)
    assert profile.language == "python"
    assert profile.archetype == "server"
    assert profile.has_ci is True

    ruleset = load_ruleset(Path("checks.yaml"))
    report = pipeline.run(
        tmp_path,
        ruleset,
        profile,
        LLMConfig(enabled=False),
        test_command="python3 -c 'import sys; sys.exit(0)'",
    )

    # Server checks apply
    server_checks = [c for c in ruleset.all_checks() if c.id in {"stateless.in-memory-shared-state", "disposability.no-sigterm-handler"}]
    for c in server_checks:
        assert c.applies(profile.archetype) is True


def test_python_worker_fixture_archetype_and_gating(tmp_path):
    # Python Celery Worker
    write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "task-worker"
requires-python = "==3.12"
dependencies = ["celery==5.3.6"]
""".strip(),
    )
    write(tmp_path / "uv.lock", "")
    write(tmp_path / ".python-version", "3.12\n")
    write(tmp_path / "worker.py", "from celery import Celery\napp = Celery('tasks')\n")
    write(tmp_path / "tests" / "test_tasks.py", "def test_task(): pass")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\non: push\njobs: {}\n")

    profile = build_profile(tmp_path)
    assert profile.language == "python"
    assert profile.archetype == "worker"

    ruleset = load_ruleset(Path("checks.yaml"))
    # Worker checks apply
    worker_check = next(c for c in ruleset.all_checks() if c.id == "stateless.in-memory-shared-state")
    assert worker_check.applies(profile.archetype) is True

    # Port binding check applies to server only, NOT worker
    port_check = next((c for c in ruleset.all_checks() if c.id == "port_binding.hardcoded-port"), None)
    if port_check:
        assert port_check.applies(profile.archetype) is False


def test_python_lib_fixture_gates_out_server_checks(tmp_path):
    # Pure Python Library
    write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "math-utils"
requires-python = "==3.12"
dependencies = []
""".strip(),
    )
    write(tmp_path / "uv.lock", "")
    write(tmp_path / ".python-version", "3.12\n")
    write(tmp_path / "src" / "math_utils" / "__init__.py", "def add(a, b): return a + b\n")
    write(tmp_path / "tests" / "test_math.py", "def test_add(): pass")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\non: push\njobs: {}\n")

    profile = build_profile(tmp_path)
    assert profile.language == "python"
    assert profile.archetype == "lib"

    ruleset = load_ruleset(Path("checks.yaml"))
    # Server / Worker absence and state checks must NOT apply to libraries
    sigterm_check = next(c for c in ruleset.all_checks() if c.id == "disposability.no-sigterm-handler")
    assert sigterm_check.applies(profile.archetype) is False

    stateless_check = next(c for c in ruleset.all_checks() if c.id == "stateless.in-memory-shared-state")
    assert stateless_check.applies(profile.archetype) is False


def test_javascript_server_fixture_archetype_and_detection(tmp_path):
    # JavaScript Express Server
    write(
        tmp_path / "package.json",
        """
{
  "name": "web-api",
  "version": "1.0.0",
  "main": "server.js",
  "dependencies": {
    "express": "4.19.2"
  },
  "devDependencies": {
    "jest": "29.7.0"
  },
  "engines": {
    "node": "20.x"
  }
}
""".strip(),
    )
    write(tmp_path / "package-lock.json", "{}")
    write(tmp_path / ".nvmrc", "20.10.0\n")
    write(tmp_path / "server.js", "const express = require('express');\nconst app = express();\n")
    write(tmp_path / "test" / "server.test.js", "test('dummy', () => {});\n")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\non: push\njobs: {}\n")

    profile = build_profile(tmp_path)
    assert profile.language == "javascript"
    assert profile.archetype == "server"
    assert profile.has_ci is True

    ruleset = load_ruleset(Path("checks.yaml"))
    report = pipeline.run(
        tmp_path,
        ruleset,
        profile,
        LLMConfig(enabled=False),
        test_command="node -e 'process.exit(0)'",
    )
    # Locked, exact versions, pinned runtime, CI present, no test failure
    assert not any(f.check_id == "dependencies.missing-lockfile" for f in report.findings)
    assert not any(f.check_id == "dependencies.floating-versions" for f in report.findings)
    assert not any(f.check_id == "dev_prod_parity.missing-runtime-pin" for f in report.findings)
    assert not any(f.check_id == "build_release_run.no-ci" for f in report.findings)


def test_go_server_fixture_archetype_and_detection(tmp_path):
    # Go HTTP Server
    write(tmp_path / "go.mod", "module example.com/myservice\n\ngo 1.22\n")
    write(tmp_path / "go.sum", "")
    write(
        tmp_path / "main.go",
        """
package main

import "net/http"

func main() {
    http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {})
    http.ListenAndServe(":8080", nil)
}
""".strip(),
    )
    write(tmp_path / "main_test.go", "package main\nimport \"testing\"\nfunc TestMain(t *testing.T) {}\n")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\non: push\njobs: {}\n")

    profile = build_profile(tmp_path)
    assert profile.language == "go"
    assert profile.archetype == "server"
    assert profile.has_ci is True


def test_rust_cli_fixture_archetype_and_detection(tmp_path):
    # Rust CLI Tool
    write(
        tmp_path / "Cargo.toml",
        """
[package]
name = "mycli"
version = "0.1.0"
edition = "2021"

[dependencies]
clap = "4.5.0"
""".strip(),
    )
    write(tmp_path / "Cargo.lock", "")
    write(tmp_path / "rust-toolchain.toml", "[toolchain]\nchannel = '1.77.0'\n")
    write(tmp_path / "src" / "main.rs", "fn main() { println!(\"CLI tool\"); }\n")
    write(tmp_path / "tests" / "cli_test.rs", "#[test] fn test_cli() {}\n")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\non: push\njobs: {}\n")

    profile = build_profile(tmp_path)
    assert profile.language == "rust"
    assert profile.archetype == "cli"

    ruleset = load_ruleset(Path("checks.yaml"))
    # Gating: server checks must not apply to Rust CLI
    server_check = next(c for c in ruleset.all_checks() if c.id == "disposability.no-sigterm-handler")
    assert server_check.applies(profile.archetype) is False
