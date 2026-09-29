"""Stage 6 — the ledger: a persistent SQLite store of tracked debt items.

Findings become debt items keyed by a content-based fingerprint (NOT file:line),
so the same issue survives edits and yields a debt trend. See CLAUDE.md
'The ledger'.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from .models import Finding, Severity

DEFAULT_LEDGER_PATH = ".prod-tracker/ledger.db"

# fingerprint is UNIQUE per (repo, fingerprint), not globally: two repos can
# legitimately produce the same check_id + path + snippet without being the
# same tracked debt item.
SCHEMA = """
CREATE TABLE IF NOT EXISTS debt_item (
    id             INTEGER PRIMARY KEY,
    repo           TEXT NOT NULL,
    path           TEXT NOT NULL,
    anchor         TEXT,
    check_id       TEXT NOT NULL,
    severity       TEXT NOT NULL,
    confidence     REAL NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    evidence       TEXT,
    fingerprint    TEXT NOT NULL,
    check_version  INTEGER,
    first_seen_sha TEXT,
    first_seen_at  TEXT,
    last_seen_sha  TEXT,
    last_seen_at   TEXT,
    assignee       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_debt_item_repo_fingerprint
    ON debt_item (repo, fingerprint);
"""

# Anchors like "line 42" are position hints from regex-based detectors, not the
# stable enclosing-symbol CLAUDE.md describes (that needs tree-sitter, not yet
# implemented). Hashing them would violate "never key debt on file:line" the
# moment an unrelated earlier edit shifts every line number below it, so they
# are deliberately excluded from the fingerprint; only symbol-like anchors
# (e.g. "dependencies.express") contribute to identity.
_LINE_ANCHOR_RE = re.compile(r"^line\s+\d+$", re.IGNORECASE)


class Status(str, Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"
    wontfix = "wontfix"
    accepted = "accepted"


class DebtItem(BaseModel):
    id: int | None = None
    repo: str
    path: str
    anchor: str | None = None
    check_id: str
    severity: Severity
    confidence: float
    status: Status = Status.open
    evidence: str | None = None
    fingerprint: str
    check_version: int | None = None
    first_seen_sha: str | None = None
    first_seen_at: str | None = None
    last_seen_sha: str | None = None
    last_seen_at: str | None = None
    assignee: str | None = None


class LedgerSummary(BaseModel):
    new: int = 0
    still_open: int = 0
    reopened: int = 0
    resolved: int = 0


def init_db(path: str | Path = DEFAULT_LEDGER_PATH) -> sqlite3.Connection:
    """Connect to the ledger DB, applying the schema. Creates parent dirs."""
    db_path = Path(path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def compute_fingerprint(check_id: str, path: str, anchor: str | None, evidence: str) -> str:
    """Content + structural identity for a finding. See module docstring on anchors."""
    stable_anchor = "" if anchor is None or _LINE_ANCHOR_RE.match(anchor.strip()) else anchor.strip()
    normalized_evidence = " ".join(evidence.split())
    payload = "\x1f".join([check_id, path, stable_anchor, normalized_evidence])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upsert(
    conn: sqlite3.Connection,
    findings: list[Finding],
    *,
    repo: str,
    evaluated_check_ids: set[str],
    check_version: int,
    sha: str = "unknown",
    now: datetime | None = None,
) -> tuple[list[DebtItem], LedgerSummary]:
    """Dedup findings against the ledger by fingerprint and reconcile status.

    `evaluated_check_ids` must be the set of check_ids actually *executed* this
    run (not just archetype-applicable) — any prior open debt item for one of
    those check_ids that no longer matches is marked resolved. Passing a check
    id whose detector didn't really run (e.g. a stub) would falsely resolve
    real debt, so callers must be conservative here.
    """
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    summary = LedgerSummary()
    touched: list[DebtItem] = []
    seen_fingerprints: set[str] = set()

    for finding in findings:
        fingerprint = compute_fingerprint(finding.check_id, finding.file, finding.anchor, finding.evidence)
        seen_fingerprints.add(fingerprint)
        existing = conn.execute(
            "SELECT * FROM debt_item WHERE repo = ? AND fingerprint = ?",
            (repo, fingerprint),
        ).fetchone()

        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO debt_item (
                    repo, path, anchor, check_id, severity, confidence, status,
                    evidence, fingerprint, check_version,
                    first_seen_sha, first_seen_at, last_seen_sha, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo,
                    finding.file,
                    finding.anchor,
                    finding.check_id,
                    finding.severity.value,
                    finding.confidence,
                    Status.open.value,
                    finding.evidence,
                    fingerprint,
                    check_version,
                    sha,
                    timestamp,
                    sha,
                    timestamp,
                ),
            )
            summary.new += 1
            item_id = cursor.lastrowid
        else:
            status = Status(existing["status"])
            if status is Status.resolved:
                status = Status.open
                summary.reopened += 1
            else:
                summary.still_open += 1
            conn.execute(
                """
                UPDATE debt_item
                SET status = ?, severity = ?, confidence = ?, evidence = ?,
                    check_version = ?, last_seen_sha = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    finding.severity.value,
                    finding.confidence,
                    finding.evidence,
                    check_version,
                    sha,
                    timestamp,
                    existing["id"],
                ),
            )
            item_id = existing["id"]

        row = conn.execute("SELECT * FROM debt_item WHERE id = ?", (item_id,)).fetchone()
        touched.append(_debt_item_from_row(row))

    summary.resolved = _resolve_missing(conn, repo, evaluated_check_ids, seen_fingerprints)
    conn.commit()
    return touched, summary


def _resolve_missing(
    conn: sqlite3.Connection,
    repo: str,
    evaluated_check_ids: set[str],
    seen_fingerprints: set[str],
) -> int:
    if not evaluated_check_ids:
        return 0
    placeholders = ",".join("?" for _ in evaluated_check_ids)
    rows = conn.execute(
        f"""
        SELECT id, fingerprint FROM debt_item
        WHERE repo = ? AND check_id IN ({placeholders}) AND status != ?
        """,
        (repo, *evaluated_check_ids, Status.resolved.value),
    ).fetchall()
    stale_ids = [row["id"] for row in rows if row["fingerprint"] not in seen_fingerprints]
    for stale_id in stale_ids:
        conn.execute("UPDATE debt_item SET status = ? WHERE id = ?", (Status.resolved.value, stale_id))
    return len(stale_ids)


def list_items(conn: sqlite3.Connection, repo: str, *, status: Status | None = None) -> list[DebtItem]:
    if status is None:
        rows = conn.execute("SELECT * FROM debt_item WHERE repo = ? ORDER BY id", (repo,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM debt_item WHERE repo = ? AND status = ? ORDER BY id",
            (repo, status.value),
        ).fetchall()
    return [_debt_item_from_row(row) for row in rows]


def _debt_item_from_row(row: sqlite3.Row) -> DebtItem:
    return DebtItem(
        id=row["id"],
        repo=row["repo"],
        path=row["path"],
        anchor=row["anchor"],
        check_id=row["check_id"],
        severity=Severity(row["severity"]),
        confidence=row["confidence"],
        status=Status(row["status"]),
        evidence=row["evidence"],
        fingerprint=row["fingerprint"],
        check_version=row["check_version"],
        first_seen_sha=row["first_seen_sha"],
        first_seen_at=row["first_seen_at"],
        last_seen_sha=row["last_seen_sha"],
        last_seen_at=row["last_seen_at"],
        assignee=row["assignee"],
    )
