"""The CEO command line: what the CEO skill calls (T13, D13).

Read commands print JSON, so any coding assistant (or a later MCP wrapper) can
use them. Decisions go through the same code as chat answers, with route
``skill`` and the identity bound to the caller's SSH key (C10).

  python -m ceo.cli status                  plans, task counts, open gates and questions
  python -m ceo.cli questions               open questions, with gate details
  python -m ceo.cli answer <n> <text...>    answer Q<n> ('approve' / 'deny <reason>' for a gate)

Over SSH, the box pins the engineer's key to the entry point below, so the
identity comes from authorized_keys, never from the client (see docs/skill.md):

  command="<venv>/bin/python -m ceo.cli skill-shell --env-file <env> <identity>",restrict ssh-ed25519 AAAA...

``skill-shell`` reads the requested command from SSH_ORIGINAL_COMMAND, runs it
only if it's one of the commands above, and passes an answer through exactly
as sent. No shell is involved.
"""

import argparse
import json
import os
import sqlite3
import sys

from ceo.gates import Engineer
from ceo.ledger import ledger_path, open_ledger
from ceo.questions import answer_question

ROUTE = "skill"
SKILL_COMMANDS = ("status", "questions", "answer")


def status(conn: sqlite3.Connection) -> dict:
    plans = []
    for plan in conn.execute("SELECT * FROM plans ORDER BY id"):
        counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, count(*) AS n FROM tasks"
                " WHERE plan_id = ? AND tombstoned = 0 GROUP BY status",
                (plan["id"],),
            )
        }
        plans.append(
            {
                "id": plan["id"],
                "repo": plan["repo"],
                "change": plan["change"],
                "approved_sha": plan["approved_sha"],
                "tasks": counts,
            }
        )
    open_gates = conn.execute("SELECT count(*) FROM gates WHERE status = 'open'").fetchone()[0]
    open_questions = conn.execute("SELECT count(*) FROM questions WHERE answer IS NULL").fetchone()[0]
    return {"plans": plans, "open_gates": open_gates, "open_questions": open_questions}


def open_questions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT q.id, q.plan_id, p.change, q.task_id, q.text, q.asked_at,"
        " g.id AS gate_id, g.kind AS gate_kind, g.payload AS gate_payload,"
        " g.payload_sha256 AS gate_sha256"
        " FROM questions q JOIN plans p ON p.id = q.plan_id"
        " LEFT JOIN gates g ON g.id = q.gate_id"
        " WHERE q.answer IS NULL ORDER BY q.id"
    )
    result = []
    for r in rows:
        item = {
            "question": r["id"],
            "plan": r["change"],
            "task": r["task_id"],
            "text": r["text"],
            "asked_at": r["asked_at"],
        }
        if r["gate_id"] is not None:
            item["gate"] = {
                "id": r["gate_id"],
                "kind": r["gate_kind"],
                "payload": json.loads(r["gate_payload"]),
                "sha256": r["gate_sha256"],
            }
        result.append(item)
    return result


def run(argv: list[str], *, identity: str, conn: sqlite3.Connection, engineer: Engineer) -> tuple[int, str]:
    """Run one command. Returns (exit code, output)."""
    parser = argparse.ArgumentParser(prog="ceo", exit_on_error=False, add_help=False)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("questions")
    answer = sub.add_parser("answer")
    answer.add_argument("question", type=int)
    # Everything after the number is the answer, even words starting with '-'.
    answer.add_argument("text", nargs=argparse.REMAINDER)
    usage = f"usage: {' | '.join(SKILL_COMMANDS)} (answer <n> <text...>)"
    try:
        args = parser.parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        return 2, usage
    if args.command == "answer" and not " ".join(args.text).strip():
        return 2, usage
    if args.command == "status":
        return 0, json.dumps(status(conn), indent=1)
    if args.command == "questions":
        return 0, json.dumps(open_questions(conn), indent=1)
    reply = answer_question(
        conn,
        engineer,
        route=ROUTE,
        identity=identity,
        question_id=args.question,
        answer=" ".join(args.text).strip(),
    )
    ok = reply.startswith(f"Q{args.question}: recorded")
    return (0 if ok else 1), reply


def load_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE lines; '#' comments and blank lines ignored. No shell expansion."""
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def skill_shell(original_command: str | None, identity: str, env: dict[str, str]) -> tuple[int, str]:
    """The SSH forced-command entry: allowlisted commands only, identity from the key."""
    if not original_command:
        return 2, f"no command; use one of: {', '.join(SKILL_COMMANDS)}"
    # SSH joins the client's arguments with spaces, so the command arrives as
    # one string. Split off only the command and, for 'answer', the question
    # number; the rest is the engineer's answer, kept exactly as sent (quotes
    # and apostrophes included). No shell ever sees it.
    argv = original_command.split(maxsplit=2)
    if not argv or argv[0] not in SKILL_COMMANDS:
        return 2, f"not allowed: {argv[0] if argv else ''!r}; use one of: {', '.join(SKILL_COMMANDS)}"
    if argv[0] == "answer":
        if len(argv) != 3 or not argv[1].isascii() or not argv[1].isdigit():
            return 2, "usage: answer <n> <text...>"
    elif len(argv) != 1:
        return 2, f"usage: {argv[0]} takes no arguments"
    conn = open_ledger(ledger_path(env))
    try:
        return run(argv, identity=identity, conn=conn, engineer=Engineer.from_env(env))
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if argv[:1] == ["skill-shell"]:
        parser = argparse.ArgumentParser(prog="ceo skill-shell")
        parser.add_argument("--env-file")
        parser.add_argument("identity")
        args = parser.parse_args(argv[1:])
        env = dict(os.environ)
        if args.env_file:
            env.update(load_env_file(args.env_file))
        code, out = skill_shell(os.environ.get("SSH_ORIGINAL_COMMAND"), args.identity, env)
    else:
        # Run directly on the box: the identity is the local OS user, which
        # must be listed in CEO_ENGINEER_SKILL to decide anything.
        identity = f"local:{os.environ.get('USER', 'unknown')}"
        conn = open_ledger(ledger_path())
        try:
            code, out = run(argv, identity=identity, conn=conn, engineer=Engineer.from_env())
        finally:
            conn.close()
    print(out, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
