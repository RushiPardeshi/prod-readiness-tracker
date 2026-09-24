from datetime import datetime, timezone

from prod_tracker import ledger
from prod_tracker.models import Finding


def make_finding(**overrides) -> Finding:
    defaults = dict(
        check_id="logs.unstructured-logging",
        severity="S3",
        confidence=1.0,
        file="app.py",
        anchor="line 10",
        evidence='print("debug")',
    )
    defaults.update(overrides)
    return Finding(**defaults)


def test_init_db_creates_schema_and_is_idempotent(tmp_path):
    db_path = tmp_path / "ledger.db"

    conn = ledger.init_db(db_path)
    conn.close()
    conn = ledger.init_db(db_path)  # re-init on an existing file must not error

    tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert any(row["name"] == "debt_item" for row in tables)
    conn.close()


def test_fingerprint_ignores_raw_line_number_anchor():
    a = ledger.compute_fingerprint("check.id", "app.py", "line 10", 'print("x")')
    b = ledger.compute_fingerprint("check.id", "app.py", "line 99", 'print("x")')

    assert a == b


def test_fingerprint_differs_by_check_path_or_evidence():
    base = ledger.compute_fingerprint("check.id", "app.py", None, "evidence one")

    assert base != ledger.compute_fingerprint("other.id", "app.py", None, "evidence one")
    assert base != ledger.compute_fingerprint("check.id", "other.py", None, "evidence one")
    assert base != ledger.compute_fingerprint("check.id", "app.py", None, "evidence two")


def test_fingerprint_keeps_symbol_like_anchors():
    a = ledger.compute_fingerprint("dependencies.floating-versions", "package.json", "dependencies.express", "e")
    b = ledger.compute_fingerprint("dependencies.floating-versions", "package.json", "dependencies.lodash", "e")

    assert a != b


def test_upsert_new_finding_is_inserted_as_open(tmp_path):
    conn = ledger.init_db(tmp_path / "ledger.db")
    finding = make_finding()

    touched, summary = ledger.upsert(
        conn,
        [finding],
        repo="repo-a",
        evaluated_check_ids={finding.check_id},
        check_version=1,
        sha="sha1",
    )

    assert summary == ledger.LedgerSummary(new=1, still_open=0, reopened=0, resolved=0)
    assert len(touched) == 1
    assert touched[0].status is ledger.Status.open
    assert touched[0].first_seen_sha == "sha1"
    assert touched[0].last_seen_sha == "sha1"


def test_upsert_same_finding_again_is_still_open_not_duplicated(tmp_path):
    conn = ledger.init_db(tmp_path / "ledger.db")
    finding = make_finding()
    ledger.upsert(conn, [finding], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha1")

    touched, summary = ledger.upsert(
        conn, [finding], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha2"
    )

    rows = conn.execute("SELECT * FROM debt_item").fetchall()
    assert len(rows) == 1
    assert summary.new == 0
    assert summary.still_open == 1
    assert touched[0].last_seen_sha == "sha2"
    assert touched[0].first_seen_sha == "sha1"


def test_finding_missing_next_run_is_resolved_only_if_check_was_evaluated(tmp_path):
    conn = ledger.init_db(tmp_path / "ledger.db")
    finding = make_finding()
    ledger.upsert(conn, [finding], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha1")

    # the check ran again but no longer found anything -> the prior debt item resolves
    _, summary = ledger.upsert(conn, [], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha2")

    assert summary.resolved == 1
    items = ledger.list_items(conn, "repo-a")
    assert items[0].status is ledger.Status.resolved


def test_finding_missing_next_run_is_not_resolved_if_check_was_not_evaluated(tmp_path):
    conn = ledger.init_db(tmp_path / "ledger.db")
    finding = make_finding()
    ledger.upsert(conn, [finding], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha1")

    # simulate a check that wasn't actually re-run this time (e.g. a stub stage)
    _, summary = ledger.upsert(conn, [], repo="repo-a", evaluated_check_ids=set(), check_version=1, sha="sha2")

    assert summary.resolved == 0
    items = ledger.list_items(conn, "repo-a")
    assert items[0].status is ledger.Status.open


def test_resolved_finding_reappearing_is_reopened(tmp_path):
    conn = ledger.init_db(tmp_path / "ledger.db")
    finding = make_finding()
    ledger.upsert(conn, [finding], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha1")
    ledger.upsert(conn, [], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha2")

    touched, summary = ledger.upsert(
        conn, [finding], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha3"
    )

    assert summary.reopened == 1
    assert touched[0].status is ledger.Status.open


def test_fingerprint_uniqueness_is_scoped_per_repo(tmp_path):
    conn = ledger.init_db(tmp_path / "ledger.db")
    finding = make_finding()

    ledger.upsert(conn, [finding], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha1")
    ledger.upsert(conn, [finding], repo="repo-b", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha1")

    assert len(ledger.list_items(conn, "repo-a")) == 1
    assert len(ledger.list_items(conn, "repo-b")) == 1


def test_check_version_is_stamped_and_updated(tmp_path):
    conn = ledger.init_db(tmp_path / "ledger.db")
    finding = make_finding()
    ledger.upsert(conn, [finding], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha1")

    touched, _ = ledger.upsert(
        conn, [finding], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=2, sha="sha2"
    )

    assert touched[0].check_version == 2


def test_upsert_accepts_explicit_now(tmp_path):
    conn = ledger.init_db(tmp_path / "ledger.db")
    finding = make_finding()
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)

    touched, _ = ledger.upsert(
        conn, [finding], repo="repo-a", evaluated_check_ids={finding.check_id}, check_version=1, sha="sha1", now=fixed
    )

    assert touched[0].first_seen_at == fixed.isoformat()
