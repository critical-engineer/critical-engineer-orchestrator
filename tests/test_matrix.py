import pytest
from fake_http import DROP, FakeHttpApi

from ceo.channels import matrix
from ceo.channels.matrix import MatrixChannel
from ceo.notify import ChannelError

BOT = "@ceo-bot:matrix.example"
HUMAN = "@me:matrix.example"
ROOM = "!abc123:matrix.example"
TOKEN = "syt_secret_token"
API = "/_matrix/client/v3"


@pytest.fixture
def api():
    fake = FakeHttpApi()
    fake.routes.update(
        {
            ("GET", f"{API}/account/whoami"): (200, {"user_id": BOT}),
            ("GET", f"{API}/rooms/{ROOM}/joined_members"): (
                200, {"joined": {BOT: {}, HUMAN: {}}},
            ),
            ("GET", f"{API}/rooms/{ROOM}/state/m.room.encryption"): (
                404, {"errcode": "M_NOT_FOUND"},
            ),
            ("PUT", f"{API}/rooms/{ROOM}/send/m.room.message/"): (200, {"event_id": "$e"}),
            ("POST", f"{API}/join/{ROOM}"): (200, {"room_id": ROOM}),
        }
    )
    yield fake
    fake.close()


@pytest.fixture
def channel(api):
    return MatrixChannel(api.url, TOKEN, ROOM, HUMAN, timeout=2)


def sends(api):
    return [r for r in api.requests if r[0] == "PUT"]


# configuration


def env_for(url="https://matrix.example", **overrides):
    env = {
        "CEO_MATRIX_HOMESERVER": url,
        "CEO_MATRIX_ACCESS_TOKEN": TOKEN,
        "CEO_MATRIX_ROOM_ID": ROOM,
        "CEO_MATRIX_HUMAN": HUMAN,
    }
    env.update(overrides)
    return env


def test_from_env():
    ch = MatrixChannel.from_env(env_for(url="https://matrix.example/"))
    assert (ch.homeserver, ch.room_id, ch.human_id) == ("https://matrix.example", ROOM, HUMAN)


def test_from_env_reports_missing_settings():
    with pytest.raises(ChannelError, match="CEO_MATRIX_ACCESS_TOKEN"):
        MatrixChannel.from_env(env_for(CEO_MATRIX_ACCESS_TOKEN=""))


def test_token_is_not_in_repr():
    assert TOKEN not in repr(MatrixChannel.from_env(env_for()))


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"CEO_MATRIX_HOMESERVER": "http://matrix.example"}, "https"),
        ({"CEO_MATRIX_HUMAN": "me@matrix.example"}, "Matrix ID"),
        ({"CEO_MATRIX_HUMAN": "@me:matrix.example\n"}, "Matrix ID"),
        ({"CEO_MATRIX_ROOM_ID": "#room:matrix.example"}, "room ID"),
    ],
)
def test_from_env_validates(overrides, message):
    with pytest.raises(ChannelError, match=message):
        MatrixChannel.from_env(env_for(**overrides))


# sending


def test_send_posts_plain_text_to_the_room(api, channel):
    channel.send("hello")
    [(method, path, headers, body)] = sends(api)
    assert path.startswith(f"{API}/rooms/{ROOM}/send/m.room.message/")
    assert body == {"msgtype": "m.text", "body": "hello"}
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_each_send_has_its_own_transaction_id(api, channel):
    channel.send("a")
    channel.send("b")
    first, second = (r[1] for r in sends(api))
    assert first != second


def test_room_ids_are_url_encoded(api, channel):
    channel.send("hello")
    room_paths = [p for p in api.raw_paths if "/rooms/" in p]
    assert room_paths and all("/rooms/%21abc123%3Amatrix.example/" in p for p in room_paths)


@pytest.mark.parametrize(
    "joined",
    [
        {BOT: {}, HUMAN: {}, "@stranger:evil.example": {}},  # someone else in the room
        {BOT: {}},  # human not joined
        {BOT: {}, "@other:matrix.example": {}},  # wrong human
    ],
)
def test_refuses_room_with_wrong_members(api, channel, joined):
    api.routes[("GET", f"{API}/rooms/{ROOM}/joined_members")] = (200, {"joined": joined})
    with pytest.raises(ChannelError, match="room must contain only"):
        channel.send("hello")
    assert sends(api) == []


def test_refuses_encrypted_room(api, channel):
    api.routes[("GET", f"{API}/rooms/{ROOM}/state/m.room.encryption")] = (
        200, {"algorithm": "m.megolm.v1.aes-sha2"},
    )
    with pytest.raises(ChannelError, match="encrypted"):
        channel.send("hello")
    assert sends(api) == []


def test_other_errors_checking_encryption_are_raised(api, channel):
    api.routes[("GET", f"{API}/rooms/{ROOM}/state/m.room.encryption")] = (
        403, {"errcode": "M_FORBIDDEN"},
    )
    with pytest.raises(ChannelError, match="HTTP 403 M_FORBIDDEN"):
        channel.send("hello")
    assert sends(api) == []


def test_bad_token_is_reported_without_the_token(api, channel):
    api.routes[("GET", f"{API}/account/whoami")] = (401, {"errcode": "M_UNKNOWN_TOKEN"})
    with pytest.raises(ChannelError) as exc:
        channel.send("hello")
    assert "M_UNKNOWN_TOKEN" in str(exc.value) and TOKEN not in str(exc.value)


def test_unreachable_homeserver(api, channel):
    api.close()
    with pytest.raises(ChannelError):
        channel.send("hello")


# setup helpers


def test_join(api, channel):
    channel.join()
    assert ("POST", f"{API}/join/{ROOM}") in [(r[0], r[1]) for r in api.requests]


def test_cli_check_sends_a_test_message(api, monkeypatch, capsys):
    for k, v in env_for(url=api.url).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CEO_CHANNEL", raising=False)
    assert matrix.main(["check"]) == 0
    assert "channel check" in sends(api)[0][3]["body"]
    assert "ok: room verified" in capsys.readouterr().out


def test_cli_reports_errors(api, monkeypatch, capsys):
    for k, v in env_for(url=api.url).items():
        monkeypatch.setenv(k, v)
    api.routes[("GET", f"{API}/rooms/{ROOM}/joined_members")] = (200, {"joined": {BOT: {}}})
    assert matrix.main(["check"]) == 1
    assert "room must contain only" in capsys.readouterr().err
    assert matrix.main(["nope"]) == 2


# transport hardening (CodeRabbit review on #5)


@pytest.fixture
def elsewhere():
    fake = FakeHttpApi()
    fake.routes[("GET", "/")] = (200, {"user_id": BOT})
    yield fake
    fake.close()


def test_redirects_are_not_followed_and_the_token_stays_put(api, channel, elsewhere):
    api.routes[("GET", f"{API}/account/whoami")] = (
        302, {}, {"Location": f"{elsewhere.url}/steal"},
    )
    with pytest.raises(ChannelError, match="HTTP 302"):
        channel.send("hello")
    assert elsewhere.requests == []
    assert sends(api) == []


def test_non_json_response_is_a_channel_error(api, channel):
    api.routes[("GET", f"{API}/account/whoami")] = (200, b"<html>captive portal</html>")
    with pytest.raises(ChannelError, match="not JSON"):
        channel.send("hello")


def test_dropped_connection_is_a_channel_error(api, channel):
    api.routes[("GET", f"{API}/account/whoami")] = DROP
    with pytest.raises(ChannelError):
        channel.send("hello")


@pytest.mark.parametrize(
    "route, body",
    [
        (f"{API}/account/whoami", {}),
        (f"{API}/account/whoami", []),
        (f"{API}/rooms/{ROOM}/joined_members", {"joined": ["not", "a", "dict"]}),
    ],
)
def test_unexpected_response_shape_is_a_channel_error(api, channel, route, body):
    api.routes[("GET", route)] = (200, body)
    with pytest.raises(ChannelError, match="unexpected response"):
        channel.send("hello")


def test_transport_failure_is_recorded_by_notify(api, channel, tmp_path):
    from ceo.ledger import open_ledger
    from ceo.notify import notify

    api.routes[("GET", f"{API}/account/whoami")] = DROP
    conn = open_ledger(tmp_path / "ledger.db")
    with pytest.raises(ChannelError):
        notify(conn, channel, "done", "x")
    assert [r[0] for r in conn.execute("SELECT kind FROM events")] == ["notify.failed"]
    conn.close()
