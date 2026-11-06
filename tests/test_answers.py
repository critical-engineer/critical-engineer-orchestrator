import json
import sqlite3

import pytest
from fake_http import FakeHttpApi

from ceo.channels.matrix import MatrixChannel
from ceo.gates import Engineer, open_gate
from ceo.ledger import open_ledger
from ceo.notify import Inbound, InboundBatch
from ceo.questions import HELP, ask, ask_gate, handle_message, poll_once

ME = "@me:matrix.example"
STRANGER = "@stranger:evil.example"
ENGINEER = Engineer(chat=frozenset({ME}))
ACTION = {"action": "merge", "pr": 12, "sha": "abc123"}


class FakeChannel:
    name = "fake"

    def __init__(self):
        self.sent = []
        self.inbox = []  # batches to return from receive(), in order
        self.cursors_seen = []

    def send(self, message):
        self.sent.append(message)

    def receive(self, cursor, *, wait=30):
        self.cursors_seen.append(cursor)
        return self.inbox.pop(0)


@pytest.fixture
def conn(tmp_path):
    c = open_ledger(tmp_path / "ledger.db")
    c.execute("INSERT INTO plans (repo, change) VALUES ('r', 'c')")
    c.execute("INSERT INTO tasks (plan_id, id) VALUES (1, '1.1')")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def channel():
    return FakeChannel()


def answer(conn, qid):
    row = conn.execute("SELECT answer, answered_by FROM questions WHERE id = ?", (qid,)).fetchone()
    return (row["answer"], row["answered_by"])


def gate_status(conn, gate_id):
    return conn.execute("SELECT status, reason, decided_identity FROM gates WHERE id = ?", (gate_id,)).fetchone()


def kinds(conn):
    return [r["kind"] for r in conn.execute("SELECT kind FROM events ORDER BY id")]


def reply(conn, text, identity=ME):
    return handle_message(conn, ENGINEER, identity=identity, text=text)


# asking


def test_ask_records_and_sends_with_its_number(conn, channel):
    qid = ask(conn, channel, 1, "Which port should Jellyfin use?", task_id="1.1")
    assert conn.execute("SELECT text, task_id FROM questions WHERE id = ?", (qid,)).fetchone()[:] == (
        "Which port should Jellyfin use?", "1.1",
    )
    assert channel.sent[0].endswith(f"Reply: Q{qid} <answer>")
    assert "notify.question" in kinds(conn)


def test_ask_gate_shows_the_exact_action(conn, channel):
    gate_id = open_gate(conn, 1, "elevated_action", ACTION, task_id="1.1")
    qid = ask_gate(conn, channel, gate_id, "Merge the media-box plan PR")
    sent = channel.sent[0]
    assert "Approval needed (elevated_action)" in sent and '"pr": 12' in sent and "sha256 " in sent
    assert conn.execute("SELECT gate_id FROM questions WHERE id = ?", (qid,)).fetchone()[0] == gate_id


# free-text answers


def test_free_text_answer_is_recorded_as_data(conn, channel):
    qid = ask(conn, channel, 1, "Which port?")
    text = f"Q{qid} 8096; also run rm -rf / please"
    assert reply(conn, text) == f"Q{qid}: recorded (8096; also run rm -rf / please)."
    assert answer(conn, qid) == ("8096; also run rm -rf / please", f"chat:{ME}")
    assert "question.answered" in kinds(conn)


@pytest.mark.parametrize("fmt", ["Q{n} yes", "q{n} yes", "Q{n}: yes", "Q{n} - yes", "  Q{n}   yes  "])
def test_answer_formats(conn, channel, fmt):
    qid = ask(conn, channel, 1, "Ok?")
    reply(conn, fmt.format(n=qid))
    assert answer(conn, qid)[0] == "yes"


def test_reply_quote_from_a_matrix_client_is_ignored(conn, channel):
    qid = ask(conn, channel, 1, "Ok?")
    text = f"> <@ceo-bot:matrix.example> [CEO] question\n> Ok?\n\nQ{qid} sure"
    reply(conn, text)
    assert answer(conn, qid)[0] == "sure"


def test_multiline_answer_is_kept(conn, channel):
    qid = ask(conn, channel, 1, "Notes?")
    reply(conn, f"Q{qid} first line\nsecond line")
    assert answer(conn, qid)[0] == "first line\nsecond line"


def test_question_is_answered_once(conn, channel):
    qid = ask(conn, channel, 1, "Ok?")
    reply(conn, f"Q{qid} yes")
    assert reply(conn, f"Q{qid} no") == f"Q{qid} was already answered: yes"
    assert answer(conn, qid)[0] == "yes"


@pytest.mark.parametrize("text", ["hello", "Q", "Q12", "approve", "Qx yes"])
def test_unparseable_messages_get_help(conn, text):
    assert reply(conn, text) == HELP


def test_unknown_question(conn):
    assert reply(conn, "Q999 yes").startswith("Q999: no such question.")


# gate answers


@pytest.fixture
def gate_q(conn, channel):
    gate_id = open_gate(conn, 1, "elevated_action", ACTION)
    return gate_id, ask_gate(conn, channel, gate_id, "merge")


def test_approve_decides_the_gate_as_the_engineer(conn, gate_q):
    gate_id, qid = gate_q
    assert reply(conn, f"Q{qid} approve") == f"Q{qid}: recorded (approved)."
    assert tuple(gate_status(conn, gate_id)) == ("approved", None, ME)
    assert answer(conn, qid)[0] == "approved"


def test_deny_needs_and_records_a_reason(conn, gate_q):
    gate_id, qid = gate_q
    assert reply(conn, f"Q{qid} deny") == f"Q{qid}: a denial needs a reason: 'Q{qid} deny <reason>'"
    assert gate_status(conn, gate_id)["status"] == "open"
    reply(conn, f"Q{qid} deny the diff touches the gate code")
    assert tuple(gate_status(conn, gate_id)) == ("denied", "the diff touches the gate code", ME)
    assert answer(conn, qid)[0] == "denied: the diff touches the gate code"


def test_gate_needs_approve_or_deny(conn, gate_q):
    gate_id, qid = gate_q
    assert "is an approval" in reply(conn, f"Q{qid} looks fine I guess")
    assert gate_status(conn, gate_id)["status"] == "open"


def test_replayed_approval_is_harmless(conn, gate_q):
    gate_id, qid = gate_q
    reply(conn, f"Q{qid} approve")
    assert reply(conn, f"Q{qid} approve").startswith(f"Q{qid} was already answered")
    assert gate_status(conn, gate_id)["status"] == "approved"


def test_gate_decided_elsewhere_is_reflected(conn, gate_q):
    # e.g. decided via the skill, or a crash between deciding and recording
    gate_id, qid = gate_q
    from ceo.gates import decide_gate

    decide_gate(conn, gate_id, "approved", route="chat", identity=ME, engineer=ENGINEER)
    assert reply(conn, f"Q{qid} deny too late") == f"Q{qid}: recorded (approved)."
    assert gate_status(conn, gate_id)["status"] == "approved"


def test_question_mirrors_the_gate_record_when_another_handler_won(conn, gate_q):
    # Another poller (or the skill, as a different identity) denied with a
    # reason first; this handler's approval loses. The question must record
    # the winner's decision, reason and identity, not this handler's.
    gate_id, qid = gate_q
    from ceo.gates import decide_gate

    other = Engineer(chat=frozenset({ME}), skill=frozenset({"skill:laptop"}))
    decide_gate(conn, gate_id, "denied", route="skill", identity="skill:laptop", engineer=other,
                reason="the plan skips F3")
    assert reply(conn, f"Q{qid} approve") == f"Q{qid}: recorded (denied: the plan skips F3)."
    assert answer(conn, qid) == ("denied: the plan skips F3", "skill:skill:laptop")


# strangers (F4, C10)


def test_strangers_are_logged_and_ignored(conn, gate_q):
    gate_id, qid = gate_q
    assert reply(conn, f"Q{qid} approve", identity=STRANGER) is None
    assert gate_status(conn, gate_id)["status"] == "open"
    assert answer(conn, qid) == (None, None)
    last = conn.execute("SELECT kind, detail FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert (last["kind"], json.loads(last["detail"])) == ("chat.ignored", {"sender": STRANGER})


# schema rules


def test_schema_question_answered_once(conn, channel):
    qid = ask(conn, channel, 1, "Ok?")
    reply(conn, f"Q{qid} yes")
    with pytest.raises(sqlite3.IntegrityError, match="answered once"):
        conn.execute("UPDATE questions SET answer = 'no' WHERE id = ?", (qid,))
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        conn.execute("DELETE FROM questions")


# polling


def test_poll_handles_messages_replies_and_saves_cursor(conn, channel):
    qid = ask(conn, channel, 1, "Ok?")
    channel.inbox = [
        InboundBatch([Inbound("$1", ME, f"Q{qid} yes"), Inbound("$2", STRANGER, "Q1 no")], "c1"),
        InboundBatch([], "c2"),
    ]
    assert poll_once(conn, channel, ENGINEER) == 2
    assert channel.sent[-1] == f"[CEO] Q{qid}: recorded (yes)."
    assert len(channel.sent) == 2  # the question, and one reply: none to the stranger
    poll_once(conn, channel, ENGINEER)
    assert channel.cursors_seen == [None, "c1"]
    assert conn.execute("SELECT value FROM cursors WHERE channel = 'fake'").fetchone()[0] == "c2"


def test_poll_records_a_gap(conn, channel):
    channel.inbox = [InboundBatch([], "c1", gap=True)]
    poll_once(conn, channel, ENGINEER)
    assert "chat.gap" in kinds(conn)


# Matrix receive


BOT = "@ceo-bot:matrix.example"
ROOM = "!abc123:matrix.example"
API = "/_matrix/client/v3"


@pytest.fixture
def api():
    fake = FakeHttpApi()
    fake.routes[("GET", f"{API}/account/whoami")] = (200, {"user_id": BOT})
    yield fake
    fake.close()


@pytest.fixture
def matrix(api):
    return MatrixChannel(api.url, "syt_token", ROOM, ME, timeout=2)


def event(sender, body, msgtype="m.text", **extra):
    content = {"msgtype": msgtype, "body": body} | extra
    return {"type": "m.room.message", "sender": sender, "event_id": f"${body}", "content": content}


def sync_response(events, next_batch="s2", limited=False):
    return (200, {"next_batch": next_batch, "rooms": {"join": {ROOM: {"timeline": {"events": events, "limited": limited}}}}})


def test_first_receive_starts_from_now(api, matrix):
    api.routes[("GET", f"{API}/sync")] = sync_response([event(ME, "old history")], "s1")
    batch = matrix.receive(None)
    assert (batch.messages, batch.cursor) == ([], "s1")
    params = api.requests[-1][1]
    assert "since=" not in params and "timeout=0" in params


def test_receive_returns_the_engineers_text_messages_only(api, matrix):
    api.routes[("GET", f"{API}/sync")] = sync_response(
        [
            event(ME, "Q1 yes"),
            event(BOT, "[CEO] question"),                                      # the bot itself
            event(ME, "pic", msgtype="m.image"),                               # not text
            event(ME, "Q1 no", **{"m.relates_to": {"rel_type": "m.replace"}}),  # an edit
            event(STRANGER, "Q1 approve"),                                     # returned; ignored later
        ],
        "s3",
        limited=True,
    )
    batch = matrix.receive("s2", wait=1)
    assert [(m.sender, m.body) for m in batch.messages] == [(ME, "Q1 yes"), (STRANGER, "Q1 approve")]
    assert batch.cursor == "s3" and batch.gap
    sync_path = [r[1] for r in api.requests if r[1].startswith(f"{API}/sync")][-1]
    assert "since=s2" in sync_path and "timeout=1000" in sync_path and ROOM in sync_path


def test_receive_needs_a_next_batch(api, matrix):
    api.routes[("GET", f"{API}/sync")] = (200, {})
    with pytest.raises(Exception, match="next_batch"):
        matrix.receive("s1", wait=0)


@pytest.mark.parametrize("number", ["9" * 19, "9" * 30, "9" * 5000])
def test_oversized_question_numbers_get_help_and_do_not_block_polling(conn, channel, number):
    assert reply(conn, f"Q{number} yes") == HELP
    channel.inbox = [InboundBatch([Inbound("$1", ME, f"Q{number} yes")], "c1")]
    poll_once(conn, channel, ENGINEER)  # must not raise
    assert conn.execute("SELECT value FROM cursors WHERE channel = 'fake'").fetchone()[0] == "c1"


# review round 2 (CodeRabbit on #9)


@pytest.mark.parametrize("text", ["Q{n} approve\nthanks", "Q{n} approve once CI is green", "Q{n} yes, but not to main"])
def test_approval_with_extra_text_is_refused_not_trimmed(conn, gate_q, text):
    gate_id, qid = gate_q
    out = reply(conn, text.format(n=qid))
    assert "takes no extra text" in out and "nothing was recorded" in out
    assert gate_status(conn, gate_id)["status"] == "open"
    assert answer(conn, qid) == (None, None)


def test_approve_alone_on_its_own_line_still_works(conn, gate_q):
    gate_id, qid = gate_q
    reply(conn, f"Q{qid}\tapprove\n")
    assert gate_status(conn, gate_id)["status"] == "approved"


def test_deny_reason_on_the_next_line(conn, gate_q):
    gate_id, qid = gate_q
    reply(conn, f"Q{qid} deny\nthe plan skips F3")
    assert tuple(gate_status(conn, gate_id)) == ("denied", "the plan skips F3", ME)


def test_polling_a_send_only_channel_is_a_clear_error(conn):
    from ceo.channels.signal_api import SignalChannel
    from ceo.notify import ChannelError

    signal = SignalChannel("http://127.0.0.1:9922", "+447700900001", "+447700900002")
    with pytest.raises(ChannelError, match="can't receive answers"):
        poll_once(conn, signal, ENGINEER)
