import shlex
import sys
from pathlib import Path

from prod_tracker.detectors import dynamic
from prod_tracker.models import Check, RepoProfile
from prod_tracker.profiler import build_profile
from prod_tracker.ruleset import load_ruleset


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def checks(*ids: str) -> list[Check]:
    by_id = {check.id: check for check in load_ruleset(Path("checks.yaml")).all_checks()}
    return [by_id[id_] for id_ in ids]


def python_exit(code: int) -> str:
    return f"{shlex.quote(sys.executable)} -c \"import sys; sys.exit({code})\""


def test_passing_override_command_produces_no_finding(tmp_path):
    profile = RepoProfile(path=str(tmp_path))

    findings = dynamic.run(
        tmp_path,
        checks("admin_processes.tests-failing"),
        profile,
        test_command=python_exit(0),
    )

    assert findings == []


def test_failing_override_command_reports_exit_code_and_stderr(tmp_path):
    profile = RepoProfile(path=str(tmp_path))
    command = f"{shlex.quote(sys.executable)} -c \"import sys; sys.stderr.write('boom'); sys.exit(1)\""

    findings = dynamic.run(
        tmp_path,
        checks("admin_processes.tests-failing"),
        profile,
        test_command=command,
    )

    assert len(findings) == 1
    assert findings[0].check_id == "admin_processes.tests-failing"
    assert findings[0].confidence == 1.0
    assert "exited 1" in findings[0].evidence
    assert "boom" in findings[0].evidence


def test_timeout_is_reported_as_a_failure(tmp_path):
    profile = RepoProfile(path=str(tmp_path))
    command = f'{shlex.quote(sys.executable)} -c "import time; time.sleep(5)"'

    findings = dynamic.run(
        tmp_path,
        checks("build_release_run.build-fails"),
        profile,
        build_command=command,
        timeout=1,
    )

    assert len(findings) == 1
    assert "timed out after 1s" in findings[0].evidence


def test_missing_executable_is_skipped_not_reported(tmp_path):
    profile = RepoProfile(path=str(tmp_path))

    findings = dynamic.run(
        tmp_path,
        checks("build_release_run.build-fails"),
        profile,
        build_command="definitely-not-a-real-command-xyz",
    )

    assert findings == []


def test_no_command_detected_is_skipped(tmp_path):
    write(tmp_path / "pyproject.toml", "[project]\nname = 'app'")
    profile = build_profile(tmp_path)

    assert dynamic.run(tmp_path, checks("build_release_run.build-fails"), profile) == []
    assert dynamic.run(tmp_path, checks("admin_processes.tests-failing"), profile) == []


def test_auto_detects_python_test_command_via_lockfile(tmp_path):
    write(tmp_path / "pyproject.toml", "[project]\nname = 'app'")
    write(tmp_path / "tests" / "test_app.py", "def test_app(): pass")
    write(tmp_path / "uv.lock", "")
    profile = build_profile(tmp_path)

    assert dynamic._detect_test_command(tmp_path, profile) == "uv run pytest"


def test_auto_detects_python_build_command_from_build_system(tmp_path):
    write(
        tmp_path / "pyproject.toml",
        "[project]\nname = 'app'\n\n[build-system]\nrequires = ['hatchling']\nbuild-backend = 'hatchling.build'",
    )
    write(tmp_path / "uv.lock", "")
    profile = build_profile(tmp_path)

    assert dynamic._detect_build_command(tmp_path, profile) == "uv build"


def test_auto_detects_javascript_commands_by_runner(tmp_path):
    write(
        tmp_path / "package.json",
        '{"scripts": {"build": "webpack", "test": "jest"}, "dependencies": {"express": "5.0.0"}}',
    )
    write(tmp_path / "yarn.lock", "")
    profile = RepoProfile(path=str(tmp_path), language="javascript")

    assert dynamic._detect_build_command(tmp_path, profile) == "yarn build"
    assert dynamic._detect_test_command(tmp_path, profile) == "yarn test"


def test_javascript_without_scripts_is_not_detected(tmp_path):
    write(tmp_path / "package.json", '{"dependencies": {"express": "5.0.0"}}')
    profile = RepoProfile(path=str(tmp_path), language="javascript")

    assert dynamic._detect_build_command(tmp_path, profile) is None
    assert dynamic._detect_test_command(tmp_path, profile) is None
