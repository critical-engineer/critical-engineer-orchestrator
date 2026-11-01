"""Signal channel, via signal-cli-rest-api. Kept as an option (D11).

Needs a phone number registered as the bot's own Signal account (OQ1).

Configuration (environment):
  CEO_SIGNAL_API_URL       default http://127.0.0.1:9922; must be https unless loopback
  CEO_SIGNAL_BOT_NUMBER    the bot's registered number, e.g. +447700900000
  CEO_SIGNAL_HUMAN_NUMBER  the human's number (the only recipient)
"""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from ceo.notify import ChannelError, check_transport, open_url

_PHONE_NUMBER = re.compile(r"\+[1-9][0-9]{6,14}")  # E.164, ASCII digits; use fullmatch


@dataclass(frozen=True)
class SignalChannel:
    api_url: str
    bot_number: str
    human_number: str
    timeout: float = 30
    name = "signal"

    def __post_init__(self):
        for field in ("bot_number", "human_number"):
            if not _PHONE_NUMBER.fullmatch(getattr(self, field)):
                raise ChannelError(f"{field} must be in E.164 format, e.g. +447700900000")
        if self.bot_number == self.human_number:
            raise ChannelError("the bot needs its own number, not the human's (OQ1)")

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "SignalChannel":
        missing = [
            k for k in ("CEO_SIGNAL_BOT_NUMBER", "CEO_SIGNAL_HUMAN_NUMBER") if not env.get(k)
        ]
        if missing:
            raise ChannelError(f"not set: {', '.join(missing)}")
        api_url = env.get("CEO_SIGNAL_API_URL", "http://127.0.0.1:9922").rstrip("/")
        check_transport(api_url, "CEO_SIGNAL_API_URL")
        return cls(api_url, env["CEO_SIGNAL_BOT_NUMBER"], env["CEO_SIGNAL_HUMAN_NUMBER"])

    def send(self, message: str) -> None:
        payload = {
            "message": message,
            "number": self.bot_number,
            "recipients": [self.human_number],
        }
        request = urllib.request.Request(
            f"{self.api_url}/v2/send",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            open_url(request, self.timeout)
        except urllib.error.HTTPError as exc:
            raise ChannelError(f"send failed: HTTP {exc.code}") from exc
        except ChannelError as exc:
            raise ChannelError(f"send failed: {exc}") from exc
