import shlex
import sys
from pathlib import Path

from prod_tracker import ledger, pipeline
from prod_tracker.llm import LLMConfig
from prod_tracker.profiler import build_profile
from prod_tracker.ruleset import load_ruleset

# Keeps the dynamic stage hermetic: these repos have a tests/ dir + uv.lock,
# which auto-detects "uv run pytest" — override with a fast no-op instead so
# these tests don't depend on uv/pytest actually being resolvable/fast here.
_NOOP_TEST_COMMAND = f"{shlex.quote(sys.executable)} -c \"import sys; sys.exit(0)\""


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_repo_with_secret(root: Path) -> None:
    write(root / "pyproject.toml", "[project]\nname = 'app'\nrequires-python = '>=3.12'")
    write(root / "uv.lock", "")
    write(root / ".python-version", "3.12\n")
    write(root / "tests" / "test_app.py", "def test_app(): pass")
    write(root / ".github" / "workflows" / "ci.yml", "on: push\njobs: {}\n")
    write(root / "app.py", 'API_KEY = "sk-1234567890abcdef1234567890abcdef"\n')


def run_no_llm(target: Path, ledger_path, sha="sha1", repo="test-repo"):
    ruleset = load_ruleset(Path("checks.yaml"))
    profile = build_profile(target)
    return pipeline.run(
        target,
        ruleset,
        profile,
        LLMConfig(enabled=False),
        test_command=_NOOP_TEST_COMMAND,
        ledger_path=ledger_path,
        repo=repo,
        sha=sha,
    )

    
def test_run_without_ledger_path_skips_persistence(tmp_path):
    write_repo_with_secret(tmp_path)

    report = run_no_llm(tmp_path, ledger_path=None)

    assert report.ledger == ledger.LedgerSummary()
    assert report.auto_comment == []
    assert report.backlog == []
    assert any(f.check_id == "config.secrets-in-code" for f in report.findings)


def test_secret_finding_is_new_then_still_open_and_routed_to_auto_comment(tmp_path):
    write_repo_with_secret(tmp_path)
    db_path = tmp_path / ".prod-tracker" / "ledger.db"

    first = run_no_llm(tmp_path, ledger_path=db_path, sha="sha1")
    assert first.ledger.new >= 1
    assert any(item.check_id == "config.secrets-in-code" for item in first.auto_comment)

    second = run_no_llm(tmp_path, ledger_path=db_path, sha="sha2")
    assert second.ledger.new == 0
    assert second.ledger.still_open >= 1


def test_fixing_the_secret_resolves_the_debt_item(tmp_path):
    write_repo_with_secret(tmp_path)
    db_path = tmp_path / ".prod-tracker" / "ledger.db"
    run_no_llm(tmp_path, ledger_path=db_path, sha="sha1")

    write(tmp_path / "app.py", "API_KEY = get_env('API_KEY')\n")
    report = run_no_llm(tmp_path, ledger_path=db_path, sha="sha2")

    assert report.ledger.resolved >= 1
    conn = ledger.init_db(db_path)
    try:
        items = ledger.list_items(conn, "test-repo", status=ledger.Status.resolved)
    finally:
        conn.close()
    assert any(item.check_id == "config.secrets-in-code" for item in items)


def test_low_confidence_finding_style_check_routes_to_backlog(tmp_path):
    # dependencies.floating-versions is S3 -> never auto-commented regardless of confidence.
    write(tmp_path / "pyproject.toml", "[project]\nname = 'app'\nrequires-python = '>=3.12'\ndependencies = ['requests>=2']")
    write(tmp_path / "uv.lock", "")
    write(tmp_path / ".python-version", "3.12\n")
    write(tmp_path / "tests" / "test_app.py", "def test_app(): pass")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "on: push\njobs: {}\n")
    db_path = tmp_path / ".prod-tracker" / "ledger.db"

    report = run_no_llm(tmp_path, ledger_path=db_path)

    assert any(item.check_id == "dependencies.floating-versions" for item in report.backlog)
    assert not any(item.check_id == "dependencies.floating-versions" for item in report.auto_comment)
