import json

import pytest

from ceo.channels.matrix import MatrixChannel
from ceo.channels.signal_api import SignalChannel
from ceo.ledger import open_ledger
from ceo.notify import (
    MAX_LENGTH,
    ChannelError,
    channel_from_env,
    check_transport,
    format_notification,
    notify,
)


class FakeChannel:
    name = "fake"

    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def send(self, message):
        if self.error:
            raise ChannelError(self.error)
        self.sent.append(message)


@pytest.fixture
def conn(tmp_path):
    c = open_ledger(tmp_path / "ledger.db")
    yield c
    c.close()


def events(conn):
    return [
        (r["kind"], r["task_id"], json.loads(r["detail"]))
        for r in conn.execute("SELECT * FROM events ORDER BY id")
    ]


# channel selection and transport


def test_matrix_is_the_default_channel():
    env = {
        "CEO_MATRIX_HOMESERVER": "https://matrix.example",
        "CEO_MATRIX_ACCESS_TOKEN": "secret",
        "CEO_MATRIX_ROOM_ID": "!room:matrix.example",
        "CEO_MATRIX_HUMAN": "@me:matrix.example",
    }
    assert isinstance(channel_from_env(env), MatrixChannel)


def test_signal_channel_can_be_selected():
    env = {
        "CEO_CHANNEL": "signal",
        "CEO_SIGNAL_BOT_NUMBER": "+447700900001",
        "CEO_SIGNAL_HUMAN_NUMBER": "+447700900002",
    }
    assert isinstance(channel_from_env(env), SignalChannel)


def test_unknown_channel():
    with pytest.raises(ChannelError, match="unknown CEO_CHANNEL"):
        channel_from_env({"CEO_CHANNEL": "telegram"})


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:9922", "http://localhost:8008", "http://[::1]:8008", "https://matrix.example"]
)
def test_transport_allows_https_or_loopback(url):
    check_transport(url, "X")


@pytest.mark.parametrize("url", ["http://192.168.1.10:8008", "http://media-box.local", "ftp://127.0.0.1"])
def test_transport_rejects_plain_http_off_the_box(url):
    with pytest.raises(ChannelError, match="X must use https"):
        check_transport(url, "X")


# format


def test_format_notification():
    assert format_notification("blocked", " check failed ", plan="bootstrap", task_id="1.2") == (
        "[CEO] blocked · bootstrap · task 1.2\n\ncheck failed"
    )


def test_question_has_reply_instructions():
    message = format_notification("question", "Which port?", question_id=7)
    assert message.endswith("\n\nReply: Q7 <answer>")


@pytest.mark.parametrize(
    "kwargs",
    [{"kind": "question"}, {"kind": "done", "question_id": 3}, {"kind": "nope"}],
)
def test_format_rejects_bad_arguments(kwargs):
    with pytest.raises(ValueError):
        format_notification(summary="x", **kwargs)


def test_long_header_fields_are_bounded():
    message = format_notification("blocked", "x" * 5000, plan="p" * 5000, task_id="t" * 5000)
    assert len(message) <= MAX_LENGTH


def test_long_messages_are_truncated_but_keep_the_reply_line():
    message = format_notification("question", "x" * 10_000, question_id=12)
    assert len(message) <= MAX_LENGTH
    assert "…" in message
    assert message.endswith("Reply: Q12 <answer>")


# notify


def test_notify_sends_and_records(conn):
    channel = FakeChannel()
    message = notify(conn, channel, "done", "All checks passed.", task_id="1.1")
    assert channel.sent == [message]
    assert events(conn) == [
        ("notify.done", "1.1", {"channel": "fake", "message": message, "summary": "All checks passed."})
    ]


def test_full_summary_is_kept_in_the_ledger_when_truncated(conn):
    summary = "y" * 5000
    message = notify(conn, FakeChannel(), "done", summary)
    [(_, _, detail)] = events(conn)
    assert len(message) < len(summary) and detail["summary"] == summary


def test_failed_notify_is_recorded_and_raised(conn):
    with pytest.raises(ChannelError):
        notify(conn, FakeChannel(error="HTTP 500"), "blocked", "stuck", task_id="1.2")
    [(kind, task_id, detail)] = events(conn)
    assert (kind, task_id, detail["kind"], detail["error"], detail["channel"]) == (
        "notify.failed", "1.2", "blocked", "HTTP 500", "fake",
    )
