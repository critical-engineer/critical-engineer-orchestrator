"""Matrix channel, over the client-server HTTP API (D11, the POC default).

The bot is an ordinary Matrix account of its own. It talks to the human in
one private, **unencrypted** room: the POC bot has no crypto library (C2), so
the homeserver operator can read messages. Before every send the bot checks
that the room holds exactly the bot and the human and is not encrypted, so a
misconfigured room fails loudly instead of leaking or sending unreadable text.

Configuration (environment):
  CEO_MATRIX_HOMESERVER    client API base URL, e.g. https://matrix-client.matrix.org
  CEO_MATRIX_ACCESS_TOKEN  the bot's access token (secret; never logged)
  CEO_MATRIX_ROOM_ID       the room shared with the human, e.g. !abc123:matrix.org
  CEO_MATRIX_HUMAN         the human's Matrix ID (the only other member), e.g. @me:matrix.org

Setup helpers (run as the orchestrator user, with the variables above set):
  python -m ceo.channels.matrix join    accept the room invite as the bot
  python -m ceo.channels.matrix check   verify the room and send a test message
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote

from ceo.notify import ChannelError, check_transport, open_url

_USER_ID = re.compile(r"@[^:\s]+:\S+")
_ROOM_ID = re.compile(r"![^\s/]+")

_API = "/_matrix/client/v3"


@dataclass
class MatrixChannel:
    homeserver: str
    access_token: str = field(repr=False)
    room_id: str
    human_id: str
    timeout: float = 30
    name = "matrix"

    def __post_init__(self):
        self.homeserver = self.homeserver.rstrip("/")
        check_transport(self.homeserver, "CEO_MATRIX_HOMESERVER")
        if not _USER_ID.fullmatch(self.human_id):
            raise ChannelError("CEO_MATRIX_HUMAN must be a Matrix ID, e.g. @me:matrix.org")
        if not _ROOM_ID.fullmatch(self.room_id):
            raise ChannelError("CEO_MATRIX_ROOM_ID must be a room ID starting with '!'")
        if not self.access_token:
            raise ChannelError("CEO_MATRIX_ACCESS_TOKEN is empty")

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "MatrixChannel":
        keys = ("CEO_MATRIX_HOMESERVER", "CEO_MATRIX_ACCESS_TOKEN", "CEO_MATRIX_ROOM_ID", "CEO_MATRIX_HUMAN")
        missing = [k for k in keys if not env.get(k)]
        if missing:
            raise ChannelError(f"not set: {', '.join(missing)}")
        return cls(*(env[k] for k in keys))

    @property
    def _room(self) -> str:
        return quote(self.room_id, safe="")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            self.homeserver + _API + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            raw = open_url(request, self.timeout)
        except urllib.error.HTTPError as exc:
            try:
                errcode = json.loads(exc.read()).get("errcode", "")
            except (OSError, ValueError, AttributeError):
                errcode = ""
            raise MatrixHTTPError(method, path, exc.code, errcode) from exc
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ChannelError(f"{method} {path}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise ChannelError(f"{method} {path}: unexpected response")
        return data

    def _field(self, data: dict, key: str, kind: type):
        value = data.get(key)
        if not isinstance(value, kind):
            raise ChannelError(f"unexpected response: missing {key!r}")
        return value

    def bot_id(self) -> str:
        return self._field(self._request("GET", "/account/whoami"), "user_id", str)

    def verify_room(self) -> None:
        """Refuse to send unless the room is private to the bot and the human.

        Exactly two members; invite-only; history readable only by members
        from when they were invited (so a later joiner, or the world, can't
        read earlier notifications); and not encrypted (the POC bot can't).
        """
        bot = self.bot_id()
        joined = self._request("GET", f"/rooms/{self._room}/joined_members")
        members = set(self._field(joined, "joined", dict))
        if members != {bot, self.human_id}:
            raise ChannelError(
                f"room must contain only {bot} and {self.human_id}; found {', '.join(sorted(members))}"
            )
        join_rule = self._state("m.room.join_rules", "join_rule")
        if join_rule != "invite":
            raise ChannelError(f"room must be invite-only; join rule is {join_rule!r}")
        history = self._state("m.room.history_visibility", "history_visibility")
        if history not in ("invited", "joined"):
            raise ChannelError(
                f"room history must be visible to members only ('invited' or 'joined'); it is {history!r}"
            )
        try:
            self._request("GET", f"/rooms/{self._room}/state/m.room.encryption")
        except MatrixHTTPError as exc:
            if exc.status == 404:
                return
            raise
        raise ChannelError("room is end-to-end encrypted; the POC bot can't encrypt (D11)")

    def _state(self, event_type: str, key: str) -> str | None:
        """A room state value; None if the event is absent (Matrix defaults are too open)."""
        try:
            return self._request("GET", f"/rooms/{self._room}/state/{event_type}").get(key)
        except MatrixHTTPError as exc:
            if exc.status == 404:
                return None
            raise

    def send(self, message: str) -> None:
        self.verify_room()
        self._request(
            "PUT",
            f"/rooms/{self._room}/send/m.room.message/{uuid.uuid4().hex}",
            {"msgtype": "m.text", "body": message},
        )

    def join(self) -> None:
        self._request("POST", f"/join/{self._room}", {})


class MatrixHTTPError(ChannelError):
    def __init__(self, method: str, path: str, status: int, errcode: str):
        self.status = status
        super().__init__(f"{method} {path}: HTTP {status} {errcode}".rstrip())


def main(argv: list[str]) -> int:
    if argv not in (["join"], ["check"]):
        print("usage: python -m ceo.channels.matrix join|check", file=sys.stderr)
        return 2
    try:
        # Built directly: under `python -m` this module is __main__, so an
        # isinstance check against ceo.channels.matrix.MatrixChannel would fail.
        channel = MatrixChannel.from_env(os.environ)
        if argv == ["join"]:
            channel.join()
            print(f"joined {channel.room_id} as {channel.bot_id()}")
        else:
            channel.send("[CEO] channel check: if you can read this, notifications work.")
            print(f"ok: room verified and test message sent to {channel.human_id}")
    except ChannelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
