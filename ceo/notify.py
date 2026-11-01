"""Signal notifications to the human (T6), via signal-cli-rest-api.

The bot has its own Signal number (OQ1) so it can't read the human's personal
messages (F7). Messages only ever go to the one configured human: the
recipient comes from configuration, never from a caller. Every notification
is recorded in the ledger, including failed sends.

Configuration (environment):
  CEO_SIGNAL_API_URL       default http://127.0.0.1:9922; must be https unless loopback
  CEO_SIGNAL_BOT_NUMBER    the bot's registered number, e.g. +447700900000
  CEO_SIGNAL_HUMAN_NUMBER  the human's number (the only recipient)
"""

import ipaddress
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass

from ceo.ledger import record_event

NOTIFICATION_KINDS = ("critic-ready", "blocked", "question", "done")

# Keep messages readable on a phone; full detail lives in the ledger and on GitHub.
MAX_LENGTH = 2000
_MAX_LABEL = 80  # plan name / task ID in the header

_PHONE_NUMBER = re.compile(r"\+[1-9][0-9]{6,14}")  # E.164, ASCII digits; use fullmatch


class SignalError(Exception):
    pass


@dataclass(frozen=True)
class SignalConfig:
    api_url: str
    bot_number: str
    human_number: str

    def __post_init__(self):
        for name in ("bot_number", "human_number"):
            if not _PHONE_NUMBER.fullmatch(getattr(self, name)):
                raise SignalError(f"{name} must be in E.164 format, e.g. +447700900000")
        if self.bot_number == self.human_number:
            raise SignalError("the bot needs its own number, not the human's (OQ1)")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SignalConfig":
        env = os.environ if env is None else env
        missing = [
            k for k in ("CEO_SIGNAL_BOT_NUMBER", "CEO_SIGNAL_HUMAN_NUMBER") if not env.get(k)
        ]
        if missing:
            raise SignalError(f"not set: {', '.join(missing)}")
        api_url = env.get("CEO_SIGNAL_API_URL", "http://127.0.0.1:9922").rstrip("/")
        _check_transport(api_url)
        return cls(
            api_url=api_url,
            bot_number=env["CEO_SIGNAL_BOT_NUMBER"],
            human_number=env["CEO_SIGNAL_HUMAN_NUMBER"],
        )


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _check_transport(api_url: str) -> None:
    """Messages and phone numbers must not cross the network unencrypted."""
    parts = urlsplit(api_url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and _is_loopback(parts.hostname):
        return
    raise SignalError(f"CEO_SIGNAL_API_URL must use https unless it is loopback: {api_url}")


def send(config: SignalConfig, message: str, *, timeout: float = 30) -> None:
    payload = {
        "message": message,
        "number": config.bot_number,
        "recipients": [config.human_number],
    }
    request = urllib.request.Request(
        f"{config.api_url}/v2/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except urllib.error.HTTPError as exc:
        raise SignalError(f"send failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SignalError(f"send failed: {exc}") from exc


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
    config: SignalConfig,
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
    try:
        send(config, message)
    except SignalError as exc:
        record_event(
            conn,
            "notify.failed",
            plan_id=plan_id,
            task_id=task_id,
            detail={"kind": kind, "message": message, "summary": summary, "error": str(exc)},
        )
        raise
    record_event(
        conn,
        f"notify.{kind}",
        plan_id=plan_id,
        task_id=task_id,
        detail={"message": message, "summary": summary},
    )
    return message
