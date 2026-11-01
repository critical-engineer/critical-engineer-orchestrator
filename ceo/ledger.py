"""SQLite ledger: the only store of execution status (C7).

The plan (an OpenSpec change) says what to do; the ledger records what
happened. Task IDs are the change's ``X.Y`` labels, never OpenSpec's
positional JSON ids (F9). Removed tasks are tombstoned, not deleted, so an
ID is never reused. ``events`` is an append-only audit log.
"""

import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

TASK_STATUSES = ("pending", "running", "completed", "blocked")

_SCHEMA = f"""
CREATE TABLE plans (
    id           INTEGER PRIMARY KEY,
    repo         TEXT NOT NULL,
    change       TEXT NOT NULL,            -- OpenSpec change name
    approved_sha TEXT,                     -- NULL until approved on main (T5)
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (repo, change)
);

CREATE TABLE tasks (
    plan_id    INTEGER NOT NULL REFERENCES plans (id),
    id         TEXT NOT NULL,              -- "X.Y" label from tasks.md
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ({", ".join(f"'{s}'" for s in TASK_STATUSES)})),
    attempts   INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    tombstoned INTEGER NOT NULL DEFAULT 0 CHECK (tombstoned IN (0, 1)),
    PRIMARY KEY (plan_id, id)
);

CREATE TABLE runs (
    id           INTEGER PRIMARY KEY,
    plan_id      INTEGER NOT NULL,
    task_id      TEXT NOT NULL,
    worker       TEXT NOT NULL,            -- e.g. "opencode", "claude"
    started_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at  TEXT,
    commit_sha   TEXT,
    check_exit   INTEGER,                  -- exit code of the orchestrator-run check
    check_output TEXT,
    cost_usd     REAL CHECK (cost_usd IS NULL OR cost_usd >= 0),
    FOREIGN KEY (plan_id, task_id) REFERENCES tasks (plan_id, id)
);

CREATE TABLE questions (
    id          INTEGER PRIMARY KEY,       -- shown to the human as Q<id>
    plan_id     INTEGER NOT NULL REFERENCES plans (id),
    task_id     TEXT,
    text        TEXT NOT NULL,
    asked_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    answer      TEXT,
    answered_at TEXT,
    answered_by TEXT,
    FOREIGN KEY (plan_id, task_id) REFERENCES tasks (plan_id, id),
    CHECK ((answer IS NULL) = (answered_at IS NULL))
);

CREATE TABLE events (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    kind    TEXT NOT NULL,
    plan_id INTEGER REFERENCES plans (id),
    task_id TEXT,
    detail  TEXT                           -- JSON
);

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

PRAGMA user_version = {SCHEMA_VERSION};
"""


class LedgerError(Exception):
    pass


def _statements(script: str) -> list[str]:
    """Split a SQL script into statements (trigger bodies contain ';')."""
    statements, current = [], ""
    for part in script.split(";"):
        current += part + ";"
        if sqlite3.complete_statement(current):
            statements.append(current.strip())
            current = ""
    return [s for s in statements if s != ";"]


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _initialise(conn: sqlite3.Connection) -> None:
    """Create the schema on ``conn`` if needed, atomically.

    A current ledger is detected without taking the write lock. A new one is
    created inside a single write transaction, re-checking the version once
    the lock is held, so a failure leaves an empty file and concurrent openers
    can't both create tables. ``executescript()`` is avoided because it
    commits any open transaction.
    """
    version = _version(conn)
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise LedgerError(
            f"ledger schema version {version}, this code expects {SCHEMA_VERSION}"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _version(conn) == 0:
            for statement in _statements(_SCHEMA):
                conn.execute(statement)
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass  # keep the original error; open_ledger() closes the connection
        raise


def open_ledger(path: str | Path, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Open the ledger at ``path``, creating the schema if the file is new."""
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        _initialise(conn)
    except BaseException:
        conn.close()
        raise
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def record_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    plan_id: int | None = None,
    task_id: str | None = None,
    detail: dict | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO events (kind, plan_id, task_id, detail) VALUES (?, ?, ?, ?)",
        (kind, plan_id, task_id, json.dumps(detail) if detail is not None else None),
    )
    conn.commit()
    return cur.lastrowid
