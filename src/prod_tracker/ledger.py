"""Stage 6 — the ledger: a persistent SQLite store of tracked debt items.

Findings become debt items keyed by a content-based fingerprint (NOT file:line),
so the same issue survives edits and yields a debt trend. Upsert/dedup and the
status lifecycle are TODO. See CLAUDE.md 'The ledger'.
"""

from __future__ import annotations

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
    fingerprint    TEXT NOT NULL UNIQUE,
    check_version  INTEGER,
    first_seen_sha TEXT,
    first_seen_at  TEXT,
    last_seen_sha  TEXT,
    last_seen_at   TEXT,
    assignee       TEXT
);
"""


def init_db(path: str = ".prod-tracker/ledger.db"):
    # TODO: connect, apply SCHEMA, return a handle; fingerprint dedup in upsert().
    raise NotImplementedError("ledger persistence not yet implemented")
