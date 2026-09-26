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


def test_judge_and_verify_pipeline_integration(tmp_path):
    # Setup server repo with in-memory state
    write(tmp_path / "pyproject.toml", "[project]\nname = 'app'\nrequires-python = '>=3.12'")
    write(tmp_path / "uv.lock", "")
    write(tmp_path / ".python-version", "3.12\n")
    write(tmp_path / "tests" / "test_app.py", "def test_app(): pass")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "on: push\njobs: {}\n")
    write(tmp_path / "server.py", "GLOBAL_SESSIONS = {}\n")
    db_path = tmp_path / ".prod-tracker" / "ledger.db"

    ruleset = load_ruleset(Path("checks.yaml"))
    profile = build_profile(tmp_path)
    profile.archetype = "server"  # Explicitly server so stateless check applies

    # Mock client: judge finds the issue, verify confirms it
    from unittest.mock import MagicMock
    from prod_tracker.detectors import judge, verify
    mock_client = MagicMock()

    def mock_structured(config, system, user, response_model, client=None):
        if response_model is judge.JudgeOutput:
            return judge.JudgeOutput(
                findings=[
                    judge.CandidateFinding(
                        check_id="stateless.in-memory-shared-state",
                        file="server.py",
                        anchor="GLOBAL_SESSIONS",
                        evidence="GLOBAL_SESSIONS = {}",
                        confidence=0.9,
                    )
                ]
            )
        if response_model is verify.VerificationResult:
            return verify.VerificationResult(refuted=False, reason="Unmitigated in-memory state across replicas.")
        raise ValueError(f"Unexpected model: {response_model}")

    mock_client.generate_structured.side_effect = mock_structured

    report = pipeline.run(
        tmp_path,
        ruleset,
        profile,
        LLMConfig(enabled=True),
        test_command=_NOOP_TEST_COMMAND,
        ledger_path=db_path,
        client=mock_client,
        sha="sha1",
    )

    assert any(f.check_id == "stateless.in-memory-shared-state" for f in report.findings)
    assert report.ledger.new >= 1
    # stateless.in-memory-shared-state is S2 with confidence 0.9 -> auto_comment
    assert any(item.check_id == "stateless.in-memory-shared-state" for item in report.auto_comment)

    # Now verify that running with --no-llm does NOT resolve this judge debt item
    no_llm_report = pipeline.run(
        tmp_path,
        ruleset,
        profile,
        LLMConfig(enabled=False),
        test_command=_NOOP_TEST_COMMAND,
        ledger_path=db_path,
        sha="sha2",
    )
    assert no_llm_report.ledger.resolved == 0

    # Now verify that when the code is fixed and judge runs, it resolves the debt
    write(tmp_path / "server.py", "GLOBAL_SESSIONS = redis.from_url('redis://localhost')\n")
    mock_client_clean = MagicMock()
    mock_client_clean.generate_structured.return_value = judge.JudgeOutput(findings=[])

    clean_report = pipeline.run(
        tmp_path,
        ruleset,
        profile,
        LLMConfig(enabled=True),
        test_command=_NOOP_TEST_COMMAND,
        ledger_path=db_path,
        client=mock_client_clean,
        sha="sha3",
    )
    assert clean_report.ledger.resolved >= 1
