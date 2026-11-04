import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ceo.gates import (
    Engineer,
    GateError,
    canonical,
    decide_gate,
    digest,
    open_gate,
    require_approved,
)
from ceo.ledger import open_ledger

ME = "@me:matrix.example"
ENGINEER = Engineer(chat=frozenset({ME}), skill=frozenset({"me@laptop"}), github=frozenset({"DriesSamyn"}))
ACTION = {"action": "merge", "repo": "critical-engineer/x", "pr": 12, "sha": "abc123"}


@pytest.fixture
def conn(tmp_path):
    c = open_ledger(tmp_path / "ledger.db")
    c.execute("INSERT INTO plans (repo, change) VALUES ('r', 'c')")
    c.execute("INSERT INTO tasks (plan_id, id) VALUES (1, '1.1')")
    c.commit()
    yield c
    c.close()


def gate(conn, gate_id):
    return conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()


def events(conn):
    return [(r["kind"], json.loads(r["detail"])) for r in conn.execute("SELECT * FROM events ORDER BY id")]


def approve(conn, gate_id, **kw):
    args = {"route": "chat", "identity": ME, "engineer": ENGINEER} | kw
    decide_gate(conn, gate_id, "approved", **args)


# payload and digest


def test_digest_is_independent_of_key_order():
    assert digest({"a": 1, "b": [1, 2]}) == digest({"b": [1, 2], "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})
    assert canonical({"b": 1, "a": "é"}) == '{"a":"é","b":1}'


def test_open_gate_records_payload_digest_and_event(conn):
    gate_id = open_gate(conn, 1, "elevated_action", ACTION, task_id="1.1")
    row = gate(conn, gate_id)
    assert (row["status"], row["kind"], row["task_id"]) == ("open", "elevated_action", "1.1")
    assert json.loads(row["payload"]) == ACTION and row["payload_sha256"] == digest(ACTION)
    assert events(conn) == [
        ("gate.opened", {"gate": gate_id, "kind": "elevated_action", "payload_sha256": digest(ACTION)})
    ]


def test_unknown_kind(conn):
    with pytest.raises(ValueError):
        open_gate(conn, 1, "vibes", ACTION)


# deciding


def test_engineer_approves_via_chat(conn):
    gate_id = open_gate(conn, 1, "elevated_action", ACTION)
    approve(conn, gate_id)
    row = gate(conn, gate_id)
    assert (row["status"], row["decided_route"], row["decided_identity"]) == ("approved", "chat", ME)
    assert row["decided_at"] is not None
    assert events(conn)[-1] == (
        "gate.decided",
        {"gate": gate_id, "decision": "approved", "route": "chat", "identity": ME, "reason": None},
    )


def test_denial_needs_a_reason(conn):
    gate_id = open_gate(conn, 1, "high_radius", {"evidence": "x"})
    with pytest.raises(GateError, match="reason"):
        decide_gate(conn, gate_id, "denied", route="chat", identity=ME, engineer=ENGINEER, reason="  ")
    decide_gate(conn, gate_id, "denied", route="chat", identity=ME, engineer=ENGINEER, reason="race on retry")
    assert gate(conn, gate_id)["reason"] == "race on retry"


@pytest.mark.parametrize(
    "route, identity",
    [
        ("chat", "@stranger:evil.example"),   # someone else in chat
        ("github", ME),                        # right person, wrong route's identity
        ("skill", "someone@else"),
        ("email", ME),                         # unknown route
    ],
)
def test_only_the_engineer_on_that_route_can_decide(conn, route, identity):
    gate_id = open_gate(conn, 1, "elevated_action", ACTION)
    with pytest.raises(GateError, match="C10"):
        approve(conn, gate_id, route=route, identity=identity)
    assert gate(conn, gate_id)["status"] == "open"
    kind, detail = events(conn)[-1]
    assert kind == "gate.decision_refused" and detail["identity"] == identity


def test_github_logins_are_case_insensitive(conn):
    gate_id = open_gate(conn, 1, "plan_approval", {"sha": "abc"})
    approve(conn, gate_id, route="github", identity="driessamyn")
    assert gate(conn, gate_id)["decided_identity"] == "driessamyn"


def test_a_gate_is_decided_once(conn):
    gate_id = open_gate(conn, 1, "elevated_action", ACTION)
    approve(conn, gate_id)
    with pytest.raises(GateError, match="already decided"):
        decide_gate(conn, gate_id, "denied", route="chat", identity=ME, engineer=ENGINEER, reason="no")
    assert gate(conn, gate_id)["status"] == "approved"


def test_concurrent_decisions_only_one_wins(tmp_path):
    path = tmp_path / "ledger.db"
    c = open_ledger(path)
    c.execute("INSERT INTO plans (repo, change) VALUES ('r', 'c')")
    c.commit()
    gate_id = open_gate(c, 1, "elevated_action", ACTION)
    c.close()

    def try_decide(decision):
        conn = open_ledger(path)
        try:
            decide_gate(conn, gate_id, decision, route="chat", identity=ME, engineer=ENGINEER,
                        reason="no" if decision == "denied" else None)
            return "won"
        except GateError:
            return "lost"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(try_decide, ["approved", "denied"] * 4))
    assert results.count("won") == 1


def test_no_such_gate(conn):
    with pytest.raises(GateError, match="no gate"):
        approve(conn, 999)


# acting on an approval


def test_require_approved_for_the_exact_action(conn):
    gate_id = open_gate(conn, 1, "elevated_action", ACTION)
    with pytest.raises(GateError, match="open, not approved"):
        require_approved(conn, gate_id, ACTION)
    approve(conn, gate_id)
    require_approved(conn, gate_id, dict(reversed(list(ACTION.items()))))  # same action, any key order
    with pytest.raises(GateError, match="changed since it was approved"):
        require_approved(conn, gate_id, ACTION | {"sha": "def456"})


def test_denied_gate_is_not_approved(conn):
    gate_id = open_gate(conn, 1, "elevated_action", ACTION)
    decide_gate(conn, gate_id, "denied", route="chat", identity=ME, engineer=ENGINEER, reason="no")
    with pytest.raises(GateError, match="denied"):
        require_approved(conn, gate_id, ACTION)


# rules enforced by the schema itself (no code path can bypass them)


def test_schema_payload_is_immutable(conn):
    gate_id = open_gate(conn, 1, "elevated_action", ACTION)
    for sql in (
        "UPDATE gates SET payload = '{}'",
        f"UPDATE gates SET payload_sha256 = '{'0' * 64}'",
        "UPDATE gates SET kind = 'escalation'",
        "UPDATE gates SET task_id = '1.1'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(sql)
    assert gate(conn, gate_id)["payload_sha256"] == digest(ACTION)


def test_schema_decided_once(conn):
    gate_id = open_gate(conn, 1, "elevated_action", ACTION)
    approve(conn, gate_id)
    with pytest.raises(sqlite3.IntegrityError, match="decided once"):
        conn.execute("UPDATE gates SET decided_identity = '@other:x' WHERE id = ?", (gate_id,))


def test_schema_decision_fields_go_together(conn):
    open_gate(conn, 1, "elevated_action", ACTION)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE gates SET status = 'approved'")  # no route, identity or time


def test_schema_denial_needs_reason(conn):
    open_gate(conn, 1, "elevated_action", ACTION)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE gates SET status = 'denied', decided_route = 'chat', decided_identity = 'x',"
            " decided_at = 'now', reason = ' '"
        )


def test_schema_cannot_insert_a_decided_gate(conn):
    with pytest.raises(sqlite3.IntegrityError, match="opened, not inserted decided"):
        conn.execute(
            "INSERT INTO gates (plan_id, kind, payload, payload_sha256, status, decided_route,"
            " decided_identity, decided_at) VALUES (1, 'elevated_action', '{}', ?, 'approved',"
            " 'chat', 'x', 'now')",
            ("0" * 64,),
        )


def test_schema_gates_are_never_deleted(conn):
    open_gate(conn, 1, "elevated_action", ACTION)
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        conn.execute("DELETE FROM gates")


def test_schema_rejects_unknown_route_and_kind(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO gates (plan_id, kind, payload, payload_sha256) VALUES (1, 'vibes', '{}', ?)",
            ("0" * 64,),
        )
    open_gate(conn, 1, "elevated_action", ACTION)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE gates SET status = 'approved', decided_route = 'email', decided_identity = 'x',"
            " decided_at = 'now'"
        )


# engineer configuration


def test_engineer_from_env():
    eng = Engineer.from_env(
        {"CEO_MATRIX_HUMAN": ME, "CEO_ENGINEER_GITHUB": "driessamyn, other", "CEO_GITHUB_BOT": "ceo-bot"}
    )
    assert eng.chat == {ME} and eng.github == {"driessamyn", "other"} and eng.skill == frozenset()
    assert not eng.allows("skill", "anyone")  # unconfigured route can't clear gates


def test_bot_can_never_be_an_approver():
    with pytest.raises(GateError, match="bot"):
        Engineer.from_env({"CEO_ENGINEER_GITHUB": "driessamyn,CEO-Bot", "CEO_GITHUB_BOT": "ceo-bot"})


@pytest.mark.parametrize(
    "decision, reason", [("denied", ""), ("denied", None), ("maybe", None)]
)
def test_malformed_attempts_by_others_are_still_logged(conn, decision, reason):
    gate_id = open_gate(conn, 1, "elevated_action", ACTION)
    with pytest.raises(GateError, match="C10"):
        decide_gate(conn, gate_id, decision, route="chat", identity="@stranger:evil.example",
                    engineer=ENGINEER, reason=reason)
    kind, detail = events(conn)[-1]
    assert kind == "gate.decision_refused" and detail["decision"] == decision
