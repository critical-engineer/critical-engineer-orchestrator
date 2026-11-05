import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ceo.cli import load_env_file, skill_shell
from ceo.gates import open_gate
from ceo.ledger import open_ledger

ROOT = Path(__file__).parent.parent
SKILL_ID = "skill:dries-laptop"
ACTION = {"action": "merge", "pr": 12, "sha": "abc123"}


@pytest.fixture
def env(tmp_path):
    ledger = tmp_path / "ledger.db"
    conn = open_ledger(ledger)
    conn.execute("INSERT INTO plans (repo, change) VALUES ('r', 'media-box')")
    conn.execute("INSERT INTO tasks (plan_id, id, status) VALUES (1, '1.1', 'completed')")
    conn.execute("INSERT INTO tasks (plan_id, id) VALUES (1, '1.2')")
    conn.commit()
    conn.close()
    return {"CEO_LEDGER": str(ledger), "CEO_ENGINEER_SKILL": SKILL_ID}


def ledger(env):
    return open_ledger(env["CEO_LEDGER"])


def ask(env, text, gate_payload=None):
    conn = ledger(env)
    gate_id = open_gate(conn, 1, "elevated_action", gate_payload) if gate_payload else None
    with conn:
        qid = conn.execute(
            "INSERT INTO questions (plan_id, task_id, gate_id, text) VALUES (1, '1.2', ?, ?)",
            (gate_id, text),
        ).lastrowid
    conn.close()
    return qid, gate_id


def shell(env, command, identity=SKILL_ID):
    return skill_shell(command, identity, env)


# read commands


def test_status(env):
    ask(env, "Which port?")
    code, out = shell(env, "status")
    assert code == 0
    data = json.loads(out)
    assert data["plans"][0]["change"] == "media-box"
    assert data["plans"][0]["tasks"] == {"completed": 1, "pending": 1}
    assert (data["open_questions"], data["open_gates"]) == (1, 0)


def test_questions_include_the_exact_gate_payload(env):
    qid, gate_id = ask(env, "Merge?", ACTION)
    code, out = shell(env, "questions")
    [item] = json.loads(out)
    assert code == 0 and item["question"] == qid and item["plan"] == "media-box"
    assert item["gate"]["payload"] == ACTION and len(item["gate"]["sha256"]) == 64


# answering


def test_answer_records_route_and_identity_from_the_key(env):
    qid, _ = ask(env, "Which port?")
    code, out = shell(env, f"answer {qid} 8096 please")
    assert (code, out) == (0, f"Q{qid}: recorded (8096 please).")
    conn = ledger(env)
    assert conn.execute("SELECT answered_by FROM questions").fetchone()[0] == f"skill:{SKILL_ID}"
    conn.close()


def test_gate_approval_through_the_skill(env):
    qid, gate_id = ask(env, "Merge?", ACTION)
    assert shell(env, f"answer {qid} approve")[0] == 0
    conn = ledger(env)
    row = conn.execute("SELECT status, decided_route, decided_identity FROM gates").fetchone()
    assert tuple(row) == ("approved", "skill", SKILL_ID)
    conn.close()


def test_answer_text_is_kept_exactly_as_sent(env):
    # SSH joins arguments with spaces: `ssh ceo-box answer 7 "don't deploy"`
    # arrives as `answer 7 don't deploy`. Apostrophes and quotes are the
    # engineer's words, not transport syntax, and answers can't be edited.
    qid, _ = ask(env, "Merge?", ACTION)
    reason = "don't deploy; it \"touches\" the gate code 'today'"
    code, out = shell(env, f"answer {qid} deny {reason}")
    assert code == 0
    conn = ledger(env)
    assert conn.execute("SELECT reason FROM gates").fetchone()[0] == reason
    conn.close()


def test_unlisted_key_identity_cannot_answer(env):
    qid, gate_id = ask(env, "Merge?", ACTION)
    code, out = shell(env, f"answer {qid} approve", identity="skill:someone-else")
    assert code == 1 and "C10" in out
    conn = ledger(env)
    assert conn.execute("SELECT status FROM gates").fetchone()[0] == "open"
    kinds = [r[0] for r in conn.execute("SELECT kind FROM events")]
    assert "answer.refused" in kinds
    conn.close()


def test_no_skill_identity_configured_means_read_only(env):
    qid, _ = ask(env, "Which port?")
    env = {k: v for k, v in env.items() if k != "CEO_ENGINEER_SKILL"}
    assert shell(env, "status")[0] == 0
    assert shell(env, f"answer {qid} 8096")[0] == 1


# the forced-command boundary


@pytest.mark.parametrize(
    "command",
    [
        None,
        "",
        "rm -rf /",
        "bash",
        "sh -c status",
        "skill-shell skill:other",  # can't re-enter with another identity
        "status; rm -rf /",          # no shell: ';' is just part of a word
        "answer x yes",               # not a number
        "answer 1",                   # no answer text
        "status extra",               # read commands take no arguments
        "python -c 'import os'",
    ],
)
def test_only_allowlisted_commands_run(env, command):
    code, out = shell(env, command)
    assert code == 2
    conn = ledger(env)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0  # nothing happened
    conn.close()


def test_command_cannot_claim_another_identity(env):
    qid, _ = ask(env, "Merge?", ACTION)
    # Extra words are just answer text; there is no --identity option to inject.
    code, out = shell(env, f"answer {qid} approve --identity skill:dries-laptop", identity="skill:intruder")
    assert code == 1
    conn = ledger(env)
    assert conn.execute("SELECT status FROM gates").fetchone()[0] == "open"
    conn.close()


def test_bad_arguments_are_usage_errors(env):
    assert shell(env, "answer notanumber yes")[0] == 2
    assert shell(env, "answer 1")[0] == 2


# env file and process-level entry point


def test_load_env_file(tmp_path):
    f = tmp_path / "env"
    f.write_text("# comment\n\nCEO_LEDGER=/x/l.db\nCEO_ENGINEER_SKILL = skill:a\nnot a setting\n")
    assert load_env_file(f) == {"CEO_LEDGER": "/x/l.db", "CEO_ENGINEER_SKILL": "skill:a"}


def test_forced_command_end_to_end(env, tmp_path):
    """Runs the entry point the way sshd would: SSH_ORIGINAL_COMMAND + forced args."""
    qid, _ = ask(env, "Which port?")
    env_file = tmp_path / "ceo.env"
    env_file.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
    base = {k: v for k, v in os.environ.items() if not k.startswith("CEO_")}

    def run(original):
        return subprocess.run(
            [sys.executable, "-m", "ceo.cli", "skill-shell", "--env-file", str(env_file), SKILL_ID],
            env=base | {"SSH_ORIGINAL_COMMAND": original},
            capture_output=True, text=True, timeout=30, cwd=ROOT,
        )

    ok = run(f"answer {qid} 8096, don't use \"8080\"")
    assert ok.returncode == 0 and f"Q{qid}: recorded (8096, don't use \"8080\")." in ok.stdout
    refused = run("rm -rf /")
    assert refused.returncode == 2 and "not allowed" in refused.stderr


# the skill file


def test_skill_file_has_frontmatter():
    text = (ROOT / "skills" / "ceo" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front = text.split("---\n")[1]
    assert "name: ceo\n" in front and "description: " in front
    assert "never decide" in text.lower()  # the skill points; the engineer decides


def test_answer_text_may_contain_dashes(env):
    qid, _ = ask(env, "Merge?", ACTION)
    code, out = shell(env, f"answer {qid} deny --force push would lose history")
    assert code == 0
    conn = ledger(env)
    assert conn.execute("SELECT reason FROM gates").fetchone()[0] == "--force push would lose history"
    conn.close()
