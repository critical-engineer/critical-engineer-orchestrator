"""SQLite ledger: the only store of execution status (C7).

The plan (an OpenSpec change) says what to do; the ledger records what
happened. Task IDs are the change's ``X.Y`` labels, never OpenSpec's
positional JSON ids (F9). Removed tasks are tombstoned, not deleted, so an
ID is never reused. ``events`` is an append-only audit log.
"""

import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

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

PRAGMA user_version = 1;
"""

# Each migration takes the schema from version N-1 to N. A new ledger gets the
# version 1 schema and then every migration, so there is one path to "current".
_MIGRATIONS = {
    2: [
        # Hash of a task's definition (description + ceo.yaml entry), used by
        # sync to spot tasks that changed after they ran (T4, F9).
        "ALTER TABLE tasks ADD COLUMN definition_hash TEXT",
    ],
}


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
    """Create or migrate the schema on ``conn`` if needed, atomically.

    A current ledger is detected without taking the write lock. Creation and
    migrations run inside a single write transaction, re-checking the version
    once the lock is held, so a failure leaves the file as it was and
    concurrent openers can't both apply DDL. ``executescript()`` is avoided
    because it commits any open transaction.
    """
    version = _version(conn)
    if version == SCHEMA_VERSION:
        return
    if version > SCHEMA_VERSION:
        raise LedgerError(
            f"ledger schema version {version}, this code expects {SCHEMA_VERSION}"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = _version(conn)
        if version == 0:
            for statement in _statements(_SCHEMA):
                conn.execute(statement)
            version = 1
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS[target]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target}")
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


def append_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    plan_id: int | None = None,
    task_id: str | None = None,
    detail: dict | None = None,
) -> int:
    """Add an event inside the caller's transaction (no commit)."""
    cur = conn.execute(
        "INSERT INTO events (kind, plan_id, task_id, detail) VALUES (?, ?, ?, ?)",
        (kind, plan_id, task_id, json.dumps(detail) if detail is not None else None),
    )
    return cur.lastrowid


def record_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    plan_id: int | None = None,
    task_id: str | None = None,
    detail: dict | None = None,
) -> int:
    """Add an event and commit."""
    event_id = append_event(conn, kind, plan_id=plan_id, task_id=task_id, detail=detail)
    conn.commit()
    return event_id
