from pathlib import Path

from prod_tracker.detectors import deterministic
from prod_tracker.models import Check, RepoProfile
from prod_tracker.profiler import build_profile
from prod_tracker.ruleset import load_ruleset


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def checks(*ids: str) -> list[Check]:
    by_id = {check.id: check for check in load_ruleset(Path("checks.yaml")).all_checks()}
    return [by_id[id_] for id_ in ids]


def run_ids(root: Path, *ids: str, profile: RepoProfile | None = None) -> list[str]:
    profile = profile or build_profile(root)
    return [finding.check_id for finding in deterministic.run(root, checks(*ids), profile)]


def test_missing_lockfile_for_package_manifest(tmp_path):
    write(tmp_path / "package.json", '{"dependencies": {"express": "5.0.0"}}')

    findings = deterministic.run(tmp_path, checks("dependencies.missing-lockfile"), build_profile(tmp_path))

    assert len(findings) == 1
    assert findings[0].file == "package.json"
    assert findings[0].confidence == 1.0


def test_lockfile_satisfies_package_manifest(tmp_path):
    write(tmp_path / "package.json", '{"dependencies": {"express": "5.0.0"}}')
    write(tmp_path / "package-lock.json", "{}")

    assert run_ids(tmp_path, "dependencies.missing-lockfile") == []


def test_floating_versions_from_package_json_and_pyproject(tmp_path):
    write(tmp_path / "package.json", '{"dependencies": {"express": "^5.0.0"}}')
    write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "mixed"
dependencies = ["requests>=2"]
""".strip(),
    )

    findings = deterministic.run(tmp_path, checks("dependencies.floating-versions"), build_profile(tmp_path))

    assert [finding.anchor for finding in findings] == ["dependencies.express", "requests"]


def test_exact_versions_do_not_report_floating_versions(tmp_path):
    write(tmp_path / "package.json", '{"dependencies": {"express": "5.0.0"}}')
    write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "pinned"
dependencies = ["requests==2.32.0"]
""".strip(),
    )

    assert run_ids(tmp_path, "dependencies.floating-versions") == []


def test_detects_known_secret_patterns(tmp_path):
    secret = "sk_live_" + "1234567890abcdef"
    write(tmp_path / "app.py", f'API_KEY = "{secret}"')

    findings = deterministic.run(tmp_path, checks("config.secrets-in-code"), build_profile(tmp_path))

    assert len(findings) == 1
    assert findings[0].file == "app.py"
    assert "<redacted>" in findings[0].evidence


def test_no_ci_uses_profile_flag(tmp_path):
    write(tmp_path / "pyproject.toml", "[project]\nname = 'app'")

    assert run_ids(tmp_path, "build_release_run.no-ci") == ["build_release_run.no-ci"]

    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci")
    assert run_ids(tmp_path, "build_release_run.no-ci") == []


def test_missing_runtime_pin_detects_absence_and_manifest_pin(tmp_path):
    write(tmp_path / "package.json", '{"dependencies": {"express": "5.0.0"}}')

    assert run_ids(tmp_path, "dev_prod_parity.missing-runtime-pin") == [
        "dev_prod_parity.missing-runtime-pin"
    ]

    write(tmp_path / "package.json", '{"engines": {"node": "22.x"}, "dependencies": {"express": "5.0.0"}}')
    assert run_ids(tmp_path, "dev_prod_parity.missing-runtime-pin") == []


def test_unstructured_logging_detects_print_style_logging(tmp_path):
    write(tmp_path / "package.json", '{"dependencies": {"express": "5.0.0"}}')
    write(tmp_path / "server.js", "console.log('started')")
    profile = build_profile(tmp_path)

    findings = deterministic.run(tmp_path, checks("logs.unstructured-logging"), profile)

    assert len(findings) == 1
    assert findings[0].file == "server.js"


def test_no_test_suite_detects_absence_and_test_directory(tmp_path):
    write(tmp_path / "pyproject.toml", "[project]\nname = 'app'")

    assert run_ids(tmp_path, "admin_processes.no-test-suite") == ["admin_processes.no-test-suite"]

    write(tmp_path / "tests" / "test_app.py", "def test_app(): pass")
    assert run_ids(tmp_path, "admin_processes.no-test-suite") == []


def test_missing_io_timeout_detects_precise_http_calls(tmp_path):
    write(tmp_path / "package.json", '{"dependencies": {"express": "5.0.0"}}')
    write(tmp_path / "server.py", "import requests\nrequests.get('https://example.com')\n")
    profile = build_profile(tmp_path)

    findings = deterministic.run(tmp_path, checks("resilience.missing-io-timeouts"), profile)

    assert len(findings) == 1
    assert findings[0].file == "server.py"


def test_io_timeout_argument_suppresses_timeout_finding(tmp_path):
    write(tmp_path / "package.json", '{"dependencies": {"express": "5.0.0"}}')
    write(tmp_path / "server.py", "import requests\nrequests.get('https://example.com', timeout=5)\n")
    profile = build_profile(tmp_path)

    assert deterministic.run(tmp_path, checks("resilience.missing-io-timeouts"), profile) == []


def test_phantom_deps_detects_undeclared_python_import(tmp_path):
    write(tmp_path / "pyproject.toml", "[project]\nname = 'demo'\ndependencies = ['requests>=2.0']")
    write(
        tmp_path / "app.py",
        "import os\nimport requests\nfrom httpx import AsyncClient\n",
    )
    profile = build_profile(tmp_path)

    findings = deterministic.run(tmp_path, checks("dependencies.phantom-deps"), profile)
    assert len(findings) == 1
    assert findings[0].file == "app.py"
    assert findings[0].anchor == "httpx"
    assert "not declared" in findings[0].evidence


def test_phantom_deps_ignores_stdlib_and_local_modules(tmp_path):
    write(tmp_path / "pyproject.toml", "[project]\nname = 'demo'\ndependencies = ['requests>=2.0']")
    write(tmp_path / "app.py", "import os\nimport sys\nimport json\nimport requests\n")
    write(tmp_path / "src" / "worker.py", "import app\n")
    profile = build_profile(tmp_path)

    findings = deterministic.run(tmp_path, checks("dependencies.phantom-deps"), profile)
    assert findings == []


def test_phantom_deps_detects_undeclared_js_import(tmp_path):
    write(tmp_path / "package.json", '{"dependencies": {"express": "5.0.0"}}')
    write(
        tmp_path / "server.js",
        'const fs = require("fs");\nconst express = require("express");\nconst axios = require("axios");\n',
    )
    profile = build_profile(tmp_path)

    findings = deterministic.run(tmp_path, checks("dependencies.phantom-deps"), profile)
    assert len(findings) == 1
    assert findings[0].file == "server.js"
    assert findings[0].anchor == "axios"
    assert "not declared" in findings[0].evidence
