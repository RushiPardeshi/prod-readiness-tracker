from pathlib import Path

from prod_tracker import ledger, pipeline
from prod_tracker.detectors import deterministic
from prod_tracker.llm import LLMConfig
from prod_tracker.models import RepoProfile
from prod_tracker.profiler import build_profile
from prod_tracker.ruleset import load_ruleset


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_secrets_detector_ignores_placeholder_keys(tmp_path):
    # Common dummy/placeholder keys used in docs or examples must NOT fire
    write(
        tmp_path / "config.py",
        """
API_KEY = "your-api-key-here-please-change-me"
STRIPE_SECRET = "REDACTED_SECRET_KEY_12345"
TOKEN = "placeholder_token_value_xyz"
""".strip(),
    )
    ruleset = load_ruleset(Path("checks.yaml"))
    check = next(c for c in ruleset.all_checks() if c.id == "config.secrets-in-code")
    profile = RepoProfile(path=str(tmp_path))

    findings = deterministic.run(tmp_path, [check], profile)
    assert findings == []

    # But genuine/leaked tokens MUST fire
    write(tmp_path / "config.py", "GITHUB_TOKEN = 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890'\n")
    real_findings = deterministic.run(tmp_path, [check], profile)
    assert len(real_findings) == 1
    assert real_findings[0].check_id == "config.secrets-in-code"


def test_ignored_directories_are_skipped_by_scanners(tmp_path):
    # Files inside node_modules or .venv must not trigger findings
    write(tmp_path / ".venv" / "lib" / "site.py", "SECRET = 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890'\n")
    write(tmp_path / "node_modules" / "express" / "index.js", "console.log('in node modules')\n")
    write(tmp_path / "build" / "out.js", "console.log('in build')\n")
    write(tmp_path / "pyproject.toml", "[project]\nname = 'app'\nrequires-python = '>=3.12'")

    ruleset = load_ruleset(Path("checks.yaml"))
    checks = [
        c for c in ruleset.all_checks()
        if c.id in {"config.secrets-in-code", "logs.unstructured-logging"}
    ]
    profile = build_profile(tmp_path)

    findings = deterministic.run(tmp_path, checks, profile)
    assert findings == []


def test_unstructured_logging_ignores_test_files(tmp_path):
    # print() calls in test files are acceptable for test output/debugging
    write(tmp_path / "tests" / "test_main.py", "def test_ok():\n    print('test debug message')\n")
    write(tmp_path / "server.py", "print('production log message')\n")

    ruleset = load_ruleset(Path("checks.yaml"))
    check = next(c for c in ruleset.all_checks() if c.id == "logs.unstructured-logging")
    profile = build_profile(tmp_path)

    findings = deterministic.run(tmp_path, [check], profile)
    assert len(findings) == 1
    assert findings[0].file == "server.py"


def test_ledger_triaged_statuses_stay_out_of_surfacing(tmp_path):
    # Triaged statuses (acknowledged, wontfix, accepted) must not appear in auto_comment or backlog
    write(tmp_path / "pyproject.toml", "[project]\nname = 'app'\nrequires-python = '>=3.12'\ndependencies = ['requests>=2']")
    write(tmp_path / "uv.lock", "")
    write(tmp_path / ".python-version", "3.12\n")
    write(tmp_path / "tests" / "test_app.py", "def test_app(): pass")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\non: push\njobs: {}\n")
    db_path = tmp_path / "ledger.db"

    ruleset = load_ruleset(Path("checks.yaml"))
    profile = build_profile(tmp_path)

    # First run: open finding goes to backlog
    r1 = pipeline.run(
        tmp_path,
        ruleset,
        profile,
        LLMConfig(enabled=False),
        test_command="python3 -c 'import sys; sys.exit(0)'",
        ledger_path=db_path,
        sha="sha1",
    )
    assert len(r1.backlog) == 1
    item = r1.backlog[0]

    # Human triages the finding: mark as acknowledged
    conn = ledger.init_db(db_path)
    try:
        conn.execute("UPDATE debt_item SET status = 'acknowledged' WHERE id = ?", (item.id,))
        conn.commit()
    finally:
        conn.close()

    # Second run: the item is still open in code, but triaged as acknowledged
    r2 = pipeline.run(
        tmp_path,
        ruleset,
        profile,
        LLMConfig(enabled=False),
        test_command="python3 -c 'import sys; sys.exit(0)'",
        ledger_path=db_path,
        sha="sha2",
    )
    # Triaged item must NOT surface in auto_comment or backlog
    assert r2.auto_comment == []
    assert r2.backlog == []
