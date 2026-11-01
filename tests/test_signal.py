import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ceo.ledger import open_ledger
from ceo.notify import (
    MAX_LENGTH,
    SignalConfig,
    SignalError,
    format_notification,
    notify,
    send,
)

BOT = "+447700900001"
HUMAN = "+447700900002"


class FakeSignalApi:
    """A local stand-in for signal-cli-rest-api that records requests."""

    def __init__(self):
        self.requests = []
        self.status = 201
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                api.requests.append((self.path, json.loads(body)))
                self.send_response(api.status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api():
    fake = FakeSignalApi()
    yield fake
    fake.close()


@pytest.fixture
def config(api):
    return SignalConfig(api_url=api.url, bot_number=BOT, human_number=HUMAN)


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


# config


def test_config_from_env():
    cfg = SignalConfig.from_env(
        {"CEO_SIGNAL_BOT_NUMBER": BOT, "CEO_SIGNAL_HUMAN_NUMBER": HUMAN}
    )
    assert cfg == SignalConfig("http://127.0.0.1:9922", BOT, HUMAN)


def test_config_requires_numbers():
    with pytest.raises(SignalError, match="CEO_SIGNAL_HUMAN_NUMBER"):
        SignalConfig.from_env({"CEO_SIGNAL_BOT_NUMBER": BOT})


@pytest.mark.parametrize(
    "number",
    ["07700900002", "+44 7700 900002", "+0123456789", "", "+447700900002\n", "+４４7700900002"],
)
def test_config_rejects_non_e164(number):
    with pytest.raises(SignalError, match="E.164"):
        SignalConfig("http://x", BOT, number)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:9922", "http://localhost:9922", "http://[::1]:9922", "https://signal.example"],
)
def test_config_accepts_loopback_or_https(url):
    env = {"CEO_SIGNAL_API_URL": url, "CEO_SIGNAL_BOT_NUMBER": BOT, "CEO_SIGNAL_HUMAN_NUMBER": HUMAN}
    assert SignalConfig.from_env(env).api_url == url


@pytest.mark.parametrize("url", ["http://192.168.1.10:9922", "http://media-box.local:9922", "ftp://127.0.0.1"])
def test_config_rejects_plain_http_off_the_box(url):
    env = {"CEO_SIGNAL_API_URL": url, "CEO_SIGNAL_BOT_NUMBER": BOT, "CEO_SIGNAL_HUMAN_NUMBER": HUMAN}
    with pytest.raises(SignalError, match="https"):
        SignalConfig.from_env(env)


def test_bot_needs_its_own_number():
    with pytest.raises(SignalError, match="own number"):
        SignalConfig("http://x", HUMAN, HUMAN)


# send


def test_send_posts_to_the_configured_human_only(api, config):
    send(config, "hello")
    assert api.requests == [
        ("/v2/send", {"message": "hello", "number": BOT, "recipients": [HUMAN]})
    ]


def test_send_raises_on_http_error(api, config):
    api.status = 400
    with pytest.raises(SignalError, match="HTTP 400"):
        send(config, "hello")


def test_send_raises_when_api_unreachable(api, config):
    api.close()
    with pytest.raises(SignalError, match="send failed"):
        send(config, "hello", timeout=2)


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


def test_notify_sends_and_records(api, conn, config):
    message = notify(conn, config, "done", "All checks passed.", task_id="1.1")
    assert api.requests[0][1]["message"] == message
    assert events(conn) == [
        ("notify.done", "1.1", {"message": message, "summary": "All checks passed."})
    ]


def test_full_summary_is_kept_in_the_ledger_when_truncated(api, conn, config):
    summary = "y" * 5000
    message = notify(conn, config, "done", summary)
    [(_, _, detail)] = events(conn)
    assert len(message) < len(summary) and detail["summary"] == summary


def test_failed_notify_is_recorded_and_raised(api, conn, config):
    api.status = 500
    with pytest.raises(SignalError):
        notify(conn, config, "blocked", "stuck", task_id="1.2")
    [(kind, task_id, detail)] = events(conn)
    assert (kind, task_id, detail["kind"], detail["error"]) == (
        "notify.failed",
        "1.2",
        "blocked",
        "send failed: HTTP 500",
    )
