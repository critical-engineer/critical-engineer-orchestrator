"""Judgment gates (T14): where CEO stops and only the Critical Engineer decides.

A gate records the exact action or evidence shown (canonical JSON plus its
SHA-256 digest) and, once decided, which route and identity decided it. The
ledger schema enforces the hard rules: the payload never changes, a gate is
decided once, a denial has a reason, gates are never deleted (C10).

This module adds what the schema can't: a decision is accepted only from the
engineer's identity on the route it arrived by, and code about to act on an
approval must show it is acting on exactly what was approved.

Configuration (environment) for the engineer's identities, one per route:
  CEO_ENGINEER_CHAT     Matrix ID; defaults to CEO_MATRIX_HUMAN
  CEO_ENGINEER_SKILL    the identity the skill's transport authenticates (OQ12)
  CEO_ENGINEER_GITHUB   comma-separated GitHub logins allowed to approve
  CEO_GITHUB_BOT        the bot's GitHub login; it may never approve
A route with no identity configured can't clear gates.
"""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass

from ceo.ledger import GATE_KINDS, GATE_ROUTES, append_event, record_event

DECISIONS = ("approved", "denied")


class GateError(Exception):
    pass


def canonical(payload: dict) -> str:
    """One stable text form per payload, so its digest is reproducible."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(payload: dict) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Engineer:
    """The Critical Engineer's identity on each route that can clear a gate."""

    chat: frozenset[str] = frozenset()
    skill: frozenset[str] = frozenset()
    github: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Engineer":
        env = os.environ if env is None else env

        def ids(value: str | None) -> frozenset[str]:
            return frozenset(v.strip() for v in (value or "").split(",") if v.strip())

        engineer = cls(
            chat=ids(env.get("CEO_ENGINEER_CHAT") or env.get("CEO_MATRIX_HUMAN")),
            skill=ids(env.get("CEO_ENGINEER_SKILL")),
            github=ids(env.get("CEO_ENGINEER_GITHUB")),
        )
        bot = env.get("CEO_GITHUB_BOT", "").strip()
        if bot and bot.lower() in {g.lower() for g in engineer.github}:
            raise GateError("the GitHub bot can't be one of the engineer's identities (C10)")
        return engineer

    def allows(self, route: str, identity: str) -> bool:
        if route not in GATE_ROUTES:
            return False
        allowed = getattr(self, route)
        if route == "github":  # GitHub logins are case-insensitive
            return identity.lower() in {a.lower() for a in allowed}
        return identity in allowed


def open_gate(
    conn: sqlite3.Connection,
    plan_id: int,
    kind: str,
    payload: dict,
    *,
    task_id: str | None = None,
) -> int:
    if kind not in GATE_KINDS:
        raise ValueError(f"unknown gate kind {kind!r}")
    sha = digest(payload)
    with conn:
        cur = conn.execute(
            "INSERT INTO gates (plan_id, task_id, kind, payload, payload_sha256)"
            " VALUES (?, ?, ?, ?, ?)",
            (plan_id, task_id, kind, canonical(payload), sha),
        )
        gate_id = cur.lastrowid
        append_event(
            conn,
            "gate.opened",
            plan_id=plan_id,
            task_id=task_id,
            detail={"gate": gate_id, "kind": kind, "payload_sha256": sha},
        )
    return gate_id


def decide_gate(
    conn: sqlite3.Connection,
    gate_id: int,
    decision: str,
    *,
    route: str,
    identity: str,
    engineer: Engineer,
    reason: str | None = None,
) -> None:
    """Record the engineer's decision. Anyone else is refused, and the attempt logged."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if decision == "denied" and not (reason or "").strip():
        raise GateError("a denial needs a reason; it goes back to the worker")
    row = conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
    if row is None:
        raise GateError(f"no gate {gate_id}")
    if not engineer.allows(route, identity):
        record_event(
            conn,
            "gate.decision_refused",
            plan_id=row["plan_id"],
            task_id=row["task_id"],
            detail={"gate": gate_id, "route": route, "identity": identity, "decision": decision},
        )
        raise GateError(f"{identity!r} can't decide gates via {route} (C10)")
    with conn:
        cur = conn.execute(
            "UPDATE gates SET status = ?, decided_route = ?, decided_identity = ?,"
            " decided_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), reason = ?"
            " WHERE id = ? AND status = 'open'",
            (decision, route, identity, reason, gate_id),
        )
        if cur.rowcount == 0:
            raise GateError(f"gate {gate_id} was already decided")
        append_event(
            conn,
            "gate.decided",
            plan_id=row["plan_id"],
            task_id=row["task_id"],
            detail={
                "gate": gate_id,
                "decision": decision,
                "route": route,
                "identity": identity,
                "reason": reason,
            },
        )


def require_approved(conn: sqlite3.Connection, gate_id: int, payload: dict) -> None:
    """Call just before acting: the gate must be approved for exactly this payload."""
    row = conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
    if row is None:
        raise GateError(f"no gate {gate_id}")
    if row["status"] != "approved":
        raise GateError(f"gate {gate_id} is {row['status']}, not approved")
    if digest(payload) != row["payload_sha256"]:
        raise GateError(f"gate {gate_id}: the action changed since it was approved")
