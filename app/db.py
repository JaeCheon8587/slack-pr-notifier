"""Persistent core: SQLite schema for the MR review pipeline middleware.

Ground truth for schema/columns is docs/mr-review-pipeline.html §④ (SQLite
스키마). Single-writer assumption (uvicorn --workers 1) + WAL mode, per the
document's schema-note.

This module intentionally uses only the stdlib ``sqlite3`` — no ORM.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger("uvicorn.error")

# Default DB path, sourced from Settings.db_path (app/config.py) so the
# location can be overridden via the DB_PATH env var / .env.
DEFAULT_DB_PATH = Path(get_settings().db_path)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_session (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT    NOT NULL,
    mr_iid          INTEGER NOT NULL,
    mr_sha          TEXT,
    round           INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'reviewing',
    revising_since  TEXT,
    merging_since   TEXT,
    revise_attempts INTEGER NOT NULL DEFAULT 0,
    repo_slug       TEXT,
    slack_channel   TEXT,
    slack_ts        TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (project_id, mr_iid),
    CHECK (status IN ('reviewing', 'merging', 'revising', 'manual', 'merged'))
);

CREATE TABLE IF NOT EXISTS opinion (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES review_session(id),
    slack_user      TEXT    NOT NULL,
    question_refs   TEXT,
    body            TEXT    NOT NULL,
    body_hash       TEXT    NOT NULL,
    applied_round   INTEGER,
    last_verdict    TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (session_id, slack_user, body_hash)
);

CREATE INDEX IF NOT EXISTS idx_opinion_unapplied_queue
    ON opinion (session_id, applied_round, created_at);

CREATE TABLE IF NOT EXISTS event_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES review_session(id),
    kind            TEXT    NOT NULL,
    detail          TEXT,
    send_state      TEXT    NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (send_state IN ('pending', 'sent', 'failed')),
    UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_event_log_session ON event_log (session_id);
"""


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection with row access by column name.

    Single-writer design (uvicorn --workers 1): synchronous stdlib sqlite3,
    no async wrapper needed. Several independent components each open their
    own short-lived connection to the same DB file close together at process
    startup (app.main's lifespan, app.sweeper's background thread,
    app.notify_queue's worker/requeue) — the ordering and idempotency below
    exist to make that safe instead of racing into
    ``sqlite3.OperationalError: database is locked``:

    - ``timeout=30.0`` (stdlib default is 5s) plus an explicit
      ``PRAGMA busy_timeout=30000`` executed as the very first statement on
      the connection: both make SQLite block and retry internally on a
      transient lock (e.g. another connection mid-write/checkpoint) for up
      to 30s instead of raising immediately.
    - ``journal_mode`` is *queried* first, and ``PRAGMA journal_mode=WAL`` is
      only *issued* if the file is not already reporting ``wal``. Journal
      mode is a persistent, file-level property (not a per-connection one) —
      once any one connection has ever switched the file to WAL, every later
      connection already observes ``wal`` via a plain read-only query, no
      write needed. Re-issuing the ``WAL`` switch against an already-WAL
      database still takes a brief exclusive lock to (re)confirm it, and two
      connections doing that within milliseconds of each other (exactly the
      concurrent-startup case above) can collide and raise
      ``sqlite3.OperationalError: database is locked`` — skipping the
      redundant set avoids that race entirely. On the rarer first-ever
      switch (a fresh/legacy-journal DB file) a failure is swallowed and
      logged as a warning rather than propagated: the connection remains
      perfectly usable in whichever journal mode it already has, so a
      WAL-switch failure alone must never fail startup or a request.
    - ``foreign_keys=ON`` stays a per-connection (not file-level) pragma, so
      it is always (re)applied here regardless of journal mode.
    """
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row

    current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(current_mode).lower() != "wal":
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            logger.warning(
                "db.get_connection: could not switch journal_mode to WAL "
                "(staying on %s) — DB remains usable, continuing",
                current_mode,
            )

    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the review_session / opinion / event_log tables (idempotent)."""
    conn.executescript(_SCHEMA)
    conn.commit()


def unapplied_opinions(conn: sqlite3.Connection, session_id: int, locked_at: str) -> list[sqlite3.Row]:
    """Return the opinion rows not yet applied to a revision, as of a CAS-lock time.

    Ground truth: docs/mr-review-pipeline.html §S4① step 2 — "대상 =
    applied_round IS NULL AND created_at <= 잠금(CAS) 시각". Opinions inserted
    after the lock (i.e. they lost the reviewing->revising CAS race) are
    excluded here and fall through to the *next* round automatically, since
    they remain ``applied_round IS NULL``.
    """

    return conn.execute(
        "SELECT * FROM opinion WHERE session_id = ? AND applied_round IS NULL "
        "AND created_at <= ? ORDER BY created_at",
        (session_id, locked_at),
    ).fetchall()
