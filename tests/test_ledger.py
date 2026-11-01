import json
import sqlite3

import pytest

from ceo.ledger import SCHEMA_VERSION, LedgerError, open_ledger, record_event


@pytest.fixture
def conn(tmp_path):
    c = open_ledger(tmp_path / "ledger.db")
    yield c
    c.close()


@pytest.fixture
def plan_id(conn):
    cur = conn.execute(
        "INSERT INTO plans (repo, change) VALUES ('critical-engineer/x', 'bootstrap')"
    )
    return cur.lastrowid


def add_task(conn, plan_id, task_id="1.1"):
    conn.execute("INSERT INTO tasks (plan_id, id) VALUES (?, ?)", (plan_id, task_id))


def test_creates_schema_and_version(conn):
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"plans", "tasks", "runs", "questions", "events"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_reopen_keeps_data(tmp_path):
    path = tmp_path / "ledger.db"
    c = open_ledger(path)
    c.execute("INSERT INTO plans (repo, change) VALUES ('r', 'c')")
    c.commit()
    c.close()
    c = open_ledger(path)
    assert c.execute("SELECT count(*) FROM plans").fetchone()[0] == 1
    c.close()


def test_refuses_unknown_schema_version(tmp_path):
    path = tmp_path / "ledger.db"
    c = sqlite3.connect(path)
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    c.close()
    with pytest.raises(LedgerError):
        open_ledger(path)


def test_plan_change_is_unique_per_repo(conn, plan_id):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO plans (repo, change) VALUES ('critical-engineer/x', 'bootstrap')"
        )


def test_new_task_defaults(conn, plan_id):
    add_task(conn, plan_id)
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert (row["status"], row["attempts"], row["tombstoned"]) == ("pending", 0, 0)


def test_task_id_never_reused(conn, plan_id):
    add_task(conn, plan_id)
    conn.execute("UPDATE tasks SET tombstoned = 1")
    with pytest.raises(sqlite3.IntegrityError):
        add_task(conn, plan_id)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE tasks SET status = 'done'",
        "UPDATE tasks SET attempts = -1",
        "UPDATE tasks SET tombstoned = 2",
    ],
)
def test_task_constraints(conn, plan_id, sql):
    add_task(conn, plan_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_task_requires_existing_plan(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_task(conn, 999)


def test_run_requires_existing_task(conn, plan_id):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runs (plan_id, task_id, worker) VALUES (?, '9.9', 'opencode')",
            (plan_id,),
        )


def test_run_rejects_negative_cost(conn, plan_id):
    add_task(conn, plan_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runs (plan_id, task_id, worker, cost_usd)"
            " VALUES (?, '1.1', 'opencode', -0.01)",
            (plan_id,),
        )


def test_question_answer_needs_timestamp(conn, plan_id):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO questions (plan_id, text, answer) VALUES (?, 'q?', 'yes')",
            (plan_id,),
        )


def test_events_are_append_only(conn, plan_id):
    event_id = record_event(conn, "plan.created", plan_id=plan_id, detail={"a": 1})
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    assert row["kind"] == "plan.created"
    assert json.loads(row["detail"]) == {"a": 1}
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET kind = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")
