"""Questions to the engineer and their answers over chat (T7).

CEO asks with ``ask()`` (a free-text question) or ``ask_gate()`` (a judgment
gate, T14). The engineer answers in chat with ``Q<n> <answer>``; a gate takes
``Q<n> approve`` or ``Q<n> deny <reason>``. ``poll_once()`` reads new messages
from the channel and records the answers.

Rules:
- Only the engineer's identity for the chat route counts (F4, C10). Anyone
  else is logged and ignored, with no reply.
- An answer is data, never an instruction: it's stored as given and nothing
  in it is executed.
- A question is answered once; replaying a message is harmless.

Run on the box (with the channel and engineer settings in the environment):
  python -m ceo.questions poll   read and record new answers (repeat to keep going)
  python -m ceo.questions check  ask a test question and wait for the answer
"""

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from ceo.gates import Engineer, GateError, decide_gate
from ceo.ledger import append_event, open_ledger, record_event
from ceo.notify import Channel, ChannelError, channel_from_env, notify

ROUTE = "chat"

HELP = "Reply with 'Q<n> <answer>'. For an approval: 'Q<n> approve' or 'Q<n> deny <reason>'."

# At most 18 digits: a longer number can't be a question id and would overflow
# SQLite's INTEGER, failing every poll on the same message.
_ANSWER = re.compile(r"\s*[Qq](\d{1,18})\s*[:.\-]?\s+(\S.*)", re.DOTALL)
_APPROVE = {"approve", "approved", "yes"}
_DENY = {"deny", "denied", "reject", "no"}


def _strip_reply_quote(text: str) -> str:
    """Drop the '> quoted' lines Matrix clients put before a reply."""
    lines = text.splitlines()
    while lines and lines[0].startswith(">"):
        lines.pop(0)
    return "\n".join(lines).strip()


def ask(
    conn: sqlite3.Connection,
    channel: Channel,
    plan_id: int,
    text: str,
    *,
    task_id: str | None = None,
    gate_id: int | None = None,
    plan: str | None = None,
) -> int:
    """Record a question and send it. Returns its number (Q<n>)."""
    with conn:
        question_id = conn.execute(
            "INSERT INTO questions (plan_id, task_id, gate_id, text) VALUES (?, ?, ?, ?)",
            (plan_id, task_id, gate_id, text),
        ).lastrowid
    notify(
        conn,
        channel,
        "question",
        text,
        plan_id=plan_id,
        plan=plan,
        task_id=task_id,
        question_id=question_id,
    )
    return question_id


def ask_gate(
    conn: sqlite3.Connection,
    channel: Channel,
    gate_id: int,
    summary: str,
    *,
    plan: str | None = None,
) -> int:
    """Ask the engineer to decide a gate, showing exactly what it covers."""
    gate = conn.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()
    if gate is None:
        raise GateError(f"no gate {gate_id}")
    payload = json.dumps(json.loads(gate["payload"]), indent=1, ensure_ascii=False)
    text = (
        f"Approval needed ({gate['kind']}): {summary.strip()}\n\n"
        f"{payload}\n"
        f"sha256 {gate['payload_sha256'][:12]}\n\n"
        "Reply 'Q<n> approve' or 'Q<n> deny <reason>'."
    )
    return ask(
        conn, channel, gate["plan_id"], text, task_id=gate["task_id"], gate_id=gate_id, plan=plan
    )


def handle_message(
    conn: sqlite3.Connection, engineer: Engineer, *, identity: str, text: str
) -> str | None:
    """Record an answer from chat. Returns the reply to send, or None for no reply."""
    if not engineer.allows(ROUTE, identity):
        record_event(conn, "chat.ignored", detail={"sender": identity})
        return None
    match = _ANSWER.fullmatch(_strip_reply_quote(text))
    if not match:
        return HELP
    question_id, answer = int(match.group(1)), match.group(2).strip()
    question = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if question is None:
        return f"Q{question_id}: no such question. {HELP}"
    if question["answer"] is not None:
        return f"Q{question_id} was already answered: {question['answer']}"

    if question["gate_id"] is not None:
        word, _, rest = answer.partition(" ")
        word = word.lower().rstrip(".!,:")
        if word in _APPROVE:
            decision, reason = "approved", None
        elif word in _DENY:
            decision, reason = "denied", rest.strip()
            if not reason:
                return f"Q{question_id}: a denial needs a reason: 'Q{question_id} deny <reason>'"
        else:
            return f"Q{question_id} is an approval: reply 'Q{question_id} approve' or 'Q{question_id} deny <reason>'"
        try:
            decide_gate(
                conn,
                question["gate_id"],
                decision,
                route=ROUTE,
                identity=identity,
                engineer=engineer,
                reason=reason,
            )
        except GateError as exc:
            gate = conn.execute(
                "SELECT status FROM gates WHERE id = ?", (question["gate_id"],)
            ).fetchone()
            if gate is None or gate["status"] == "open":
                return f"Q{question_id}: {exc}"
            # Decided already: by a replayed message, another poller or the skill.
        # The question mirrors the gate's own record, whoever wrote the decision:
        # if two handlers race, both copy the same decision, reason and decider,
        # so the question can never contradict the gate's audit trail.
        gate = conn.execute(
            "SELECT status, reason, decided_route, decided_identity FROM gates WHERE id = ?",
            (question["gate_id"],),
        ).fetchone()
        answer = gate["status"] if gate["reason"] is None else f"{gate['status']}: {gate['reason']}"
        answered_by = f"{gate['decided_route']}:{gate['decided_identity']}"
        identity = gate["decided_identity"]
    else:
        answered_by = f"{ROUTE}:{identity}"

    with conn:
        updated = conn.execute(
            "UPDATE questions SET answer = ?, answered_by = ?,"
            " answered_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE id = ? AND answer IS NULL",
            (answer, answered_by, question_id),
        ).rowcount
        if updated:
            append_event(
                conn,
                "question.answered",
                plan_id=question["plan_id"],
                task_id=question["task_id"],
                detail={"question": question_id, "identity": identity, "answer": answer},
            )
    return f"Q{question_id}: recorded ({answer})."


def _get_cursor(conn: sqlite3.Connection, channel: str) -> str | None:
    row = conn.execute("SELECT value FROM cursors WHERE channel = ?", (channel,)).fetchone()
    return row["value"] if row else None


def _set_cursor(conn: sqlite3.Connection, channel: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO cursors (channel, value) VALUES (?, ?)"
            " ON CONFLICT (channel) DO UPDATE SET value = excluded.value,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (channel, value),
        )


def poll_once(
    conn: sqlite3.Connection, channel: Channel, engineer: Engineer, *, wait: float = 30
) -> int:
    """Read new messages, record answers, reply. Returns the number of messages handled."""
    batch = channel.receive(_get_cursor(conn, channel.name), wait=wait)
    if batch.gap:
        record_event(conn, "chat.gap", detail={"channel": channel.name})
    for message in batch.messages:
        reply = handle_message(conn, engineer, identity=message.sender, text=message.body)
        if reply:
            channel.send(f"[CEO] {reply}")
    _set_cursor(conn, channel.name, batch.cursor)
    return len(batch.messages)


def _ledger_path() -> Path:
    path = Path(os.environ.get("CEO_LEDGER", "~/.local/share/ceo/ledger.db")).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main(argv: list[str]) -> int:
    if argv not in (["poll"], ["check"]):
        print("usage: python -m ceo.questions poll|check", file=sys.stderr)
        return 2
    try:
        channel = channel_from_env()
        engineer = Engineer.from_env()
        conn = open_ledger(_ledger_path())
        if argv == ["poll"]:
            print(f"handled {poll_once(conn, channel, engineer)} message(s)")
            return 0
        # check: a real round trip through the phone
        conn.execute(
            "INSERT INTO plans (repo, change) VALUES ('local', 'chat-check')"
            " ON CONFLICT (repo, change) DO NOTHING"
        )
        conn.commit()
        plan_id = conn.execute(
            "SELECT id FROM plans WHERE repo = 'local' AND change = 'chat-check'"
        ).fetchone()[0]
        if _get_cursor(conn, channel.name) is None:
            poll_once(conn, channel, engineer, wait=0)  # start reading from now
        question_id = ask(conn, channel, plan_id, "Chat check: reply with any answer.", plan="chat-check")
        print(f"asked Q{question_id}; waiting up to 5 minutes for the answer on your phone")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            poll_once(conn, channel, engineer)
            row = conn.execute("SELECT answer FROM questions WHERE id = ?", (question_id,)).fetchone()
            if row["answer"] is not None:
                print(f"ok: Q{question_id} answered: {row['answer']}")
                return 0
        print(f"no answer to Q{question_id} within 5 minutes", file=sys.stderr)
        return 1
    except (ChannelError, GateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
