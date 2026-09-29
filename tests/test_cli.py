from pathlib import Path

from typer.testing import CliRunner

from prod_tracker.cli import app

runner = CliRunner()


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_rules_command_renders_dimensions_and_checks():
    result = runner.invoke(app, ["rules"])
    assert result.exit_code == 0
    assert "[codebase]" in result.stdout
    assert "[dependencies]" in result.stdout
    assert "[resilience]" in result.stdout
    assert "dependencies.missing-lockfile" in result.stdout
    assert "resilience.data-race-shared-mutable-state" in result.stdout


def test_scan_command_renders_profile_and_plan(tmp_path):
    write(tmp_path / "pyproject.toml", "[project]\nname = 'demo'\nrequires-python = '>=3.12'")
    write(tmp_path / "uv.lock", "")
    write(tmp_path / ".python-version", "3.12\n")
    write(tmp_path / "tests" / "test_demo.py", "def test(): pass")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\non: push\njobs: {}\n")

    result = runner.invoke(app, ["scan", str(tmp_path), "--no-llm", "--no-ledger"])
    assert result.exit_code == 0
    assert "language   : python" in result.stdout
    assert "ci         : True" in result.stdout
    assert "checks     :" in result.stdout
    assert "plan       :" in result.stdout


def test_scan_command_with_ledger_persists_and_renders_routing(tmp_path):
    write(tmp_path / "pyproject.toml", "[project]\nname = 'demo'\nrequires-python = '>=3.12'\ndependencies = ['requests>=2']")
    write(tmp_path / "uv.lock", "")
    write(tmp_path / ".python-version", "3.12\n")
    write(tmp_path / "tests" / "test_demo.py", "def test(): pass")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\non: push\njobs: {}\n")
    db_path = tmp_path / "ledger.db"

    result = runner.invoke(
        app,
        ["scan", str(tmp_path), "--no-llm", "--ledger", str(db_path), "--test-command", "python3 -c 'import sys; sys.exit(0)'"],
    )
    assert result.exit_code == 0
    assert "ledger     :" in result.stdout
    assert "backlog (" in result.stdout
    assert "dependencies.floating-versions" in result.stdout
    assert db_path.is_file()


def test_scan_command_clean_repo_prints_no_findings(tmp_path):
    # A completely clean repo with zero violations
    write(tmp_path / "pyproject.toml", "[project]\nname = 'demo'\nrequires-python = '==3.12'\ndependencies = ['requests==2.31.0']")
    write(tmp_path / "uv.lock", "")
    write(tmp_path / ".python-version", "3.12\n")
    write(tmp_path / "tests" / "test_demo.py", "def test(): pass")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\non: push\njobs: {}\n")

    result = runner.invoke(
        app,
        ["scan", str(tmp_path), "--no-llm", "--no-ledger", "--test-command", "python3 -c 'import sys; sys.exit(0)'"],
    )
    assert result.exit_code == 0
    assert "findings   : 0" in result.stdout
    assert "No findings detected." in result.stdout
