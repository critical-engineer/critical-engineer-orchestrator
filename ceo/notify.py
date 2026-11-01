"""Notifications to the human (T6), over a pluggable chat channel (D11).

The channel is Matrix for the POC (``ceo.channels.matrix``); Signal is kept
as an option (``ceo.channels.signal_api``). Whatever the channel, messages
only ever reach the one configured human, the bot has its own account so it
can't read the human's personal messages (F7), and every notification is
recorded in the ledger, including failed sends.

Configuration (environment):
  CEO_CHANNEL   "matrix" (default) or "signal"; see each channel for its settings
"""

import http.client
import ipaddress
import os
import sqlite3
import urllib.error
import urllib.request
from typing import Protocol
from urllib.parse import urlsplit

from ceo.ledger import record_event

NOTIFICATION_KINDS = ("critic-ready", "blocked", "question", "done")

# Keep messages readable on a phone; full detail lives in the ledger and on GitHub.
MAX_LENGTH = 2000
_MAX_LABEL = 80  # plan name / task ID in the header


class ChannelError(Exception):
    pass


class Channel(Protocol):
    name: str

    def send(self, message: str) -> None: ...


def channel_from_env(env: dict[str, str] | None = None) -> Channel:
    env = os.environ if env is None else env
    kind = env.get("CEO_CHANNEL", "matrix")
    if kind == "matrix":
        from ceo.channels.matrix import MatrixChannel

        return MatrixChannel.from_env(env)
    if kind == "signal":
        from ceo.channels.signal_api import SignalChannel

        return SignalChannel.from_env(env)
    raise ChannelError(f"unknown CEO_CHANNEL {kind!r} (expected matrix or signal)")


def check_transport(url: str, setting: str) -> None:
    """Messages and identifiers must not cross the network unencrypted."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and _is_loopback(parts.hostname):
        return
    raise ChannelError(f"{setting} must use https unless it is loopback: {url}")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a token or message text would follow them to a
    host that check_transport() never saw. A redirect becomes an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def open_url(request: urllib.request.Request, timeout: float) -> bytes:
    """Send ``request`` without following redirects and return the body.

    A non-2xx response (including 3xx) raises ``urllib.error.HTTPError``; any
    other transport failure, including a dropped connection, raises
    ``ChannelError``.
    """
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise ChannelError(f"{request.get_method()} {urlsplit(request.full_url).path}: {exc}") from exc


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def format_notification(
    kind: str,
    summary: str,
    *,
    plan: str | None = None,
    task_id: str | None = None,
    question_id: int | None = None,
) -> str:
    if kind not in NOTIFICATION_KINDS:
        raise ValueError(f"unknown notification kind {kind!r}")
    if (kind == "question") != (question_id is not None):
        raise ValueError("question_id is required for, and only for, questions")
    header = f"[CEO] {kind}"
    if plan:
        header += f" · {_clip(plan, _MAX_LABEL)}"
    if task_id:
        header += f" · task {_clip(task_id, _MAX_LABEL)}"
    footer = f"\n\nReply: Q{question_id} <answer>" if question_id is not None else ""
    body = summary.strip()
    room = MAX_LENGTH - len(header) - len(footer) - 2
    return f"{header}\n\n{_clip(body, room)}{footer}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def notify(
    conn: sqlite3.Connection,
    channel: Channel,
    kind: str,
    summary: str,
    *,
    plan_id: int | None = None,
    plan: str | None = None,
    task_id: str | None = None,
    question_id: int | None = None,
) -> str:
    """Send a notification and record it in the ledger. Returns the message sent."""
    message = format_notification(
        kind, summary, plan=plan, task_id=task_id, question_id=question_id
    )
    detail = {"channel": channel.name, "message": message, "summary": summary}
    try:
        channel.send(message)
    except ChannelError as exc:
        record_event(
            conn,
            "notify.failed",
            plan_id=plan_id,
            task_id=task_id,
            detail={**detail, "kind": kind, "error": str(exc)},
        )
        raise
    record_event(conn, f"notify.{kind}", plan_id=plan_id, task_id=task_id, detail=detail)
    return message
