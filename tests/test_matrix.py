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
            ("GET", f"{API}/rooms/{ROOM}/state/m.room.join_rules"): (
                200, {"join_rule": "invite"},
            ),
            ("GET", f"{API}/rooms/{ROOM}/state/m.room.history_visibility"): (
                200, {"history_visibility": "invited"},
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


def test_cli_runs_as_a_module(api):
    # Regression: `python -m` loads this module as __main__ as well as
    # ceo.channels.matrix; the CLI must work in that setting.
    import os
    import subprocess
    import sys

    env = {**os.environ, **env_for(url=api.url)}
    result = subprocess.run(
        [sys.executable, "-m", "ceo.channels.matrix", "check"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "ok: room verified" in result.stdout


# room privacy (CodeRabbit review on #5)


@pytest.mark.parametrize("visibility", ["invited", "joined"])
def test_member_only_history_is_accepted(api, channel, visibility):
    api.routes[("GET", f"{API}/rooms/{ROOM}/state/m.room.history_visibility")] = (
        200, {"history_visibility": visibility},
    )
    channel.send("hello")
    assert len(sends(api)) == 1


@pytest.mark.parametrize(
    "response, message",
    [
        ((200, {"history_visibility": "shared"}), "'shared'"),
        ((200, {"history_visibility": "world_readable"}), "'world_readable'"),
        ((404, {"errcode": "M_NOT_FOUND"}), "None"),  # absent means 'shared'
    ],
)
def test_history_readable_beyond_members_is_refused(api, channel, response, message):
    api.routes[("GET", f"{API}/rooms/{ROOM}/state/m.room.history_visibility")] = response
    with pytest.raises(ChannelError, match=f"members only.*{message}"):
        channel.send("hello")
    assert sends(api) == []


@pytest.mark.parametrize(
    "response", [(200, {"join_rule": "public"}), (200, {"join_rule": "knock"}), (404, {})]
)
def test_room_that_is_not_invite_only_is_refused(api, channel, response):
    api.routes[("GET", f"{API}/rooms/{ROOM}/state/m.room.join_rules")] = response
    with pytest.raises(ChannelError, match="invite-only"):
        channel.send("hello")
    assert sends(api) == []


# proxies (CodeRabbit review on #5)


def test_loopback_requests_bypass_an_environment_proxy(api, channel, monkeypatch):
    proxy = FakeHttpApi()
    try:
        monkeypatch.setenv("http_proxy", proxy.url)
        monkeypatch.setenv("HTTP_PROXY", proxy.url)
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        channel.send("hello")
        assert proxy.requests == []
        assert len(sends(api)) == 1
    finally:
        proxy.close()


# password login and the settings file


def test_password_login_sends_credentials_and_returns_the_session(api):
    api.routes[("POST", f"{API}/login")] = (200, {"access_token": "syt_new", "device_id": "D1", "user_id": BOT})
    session = matrix.password_login(api.url, BOT, "hunter2")
    assert session["access_token"] == "syt_new"
    [(_, path, headers, body)] = [r for r in api.requests if r[0] == "POST"]
    assert path == f"{API}/login" and "Authorization" not in headers
    assert body["identifier"] == {"type": "m.id.user", "user": BOT}
    assert body["password"] == "hunter2" and "refresh_token" not in body


def test_password_login_errors(api):
    api.routes[("POST", f"{API}/login")] = (403, {"errcode": "M_FORBIDDEN"})
    with pytest.raises(ChannelError, match="HTTP 403 M_FORBIDDEN"):
        matrix.password_login(api.url, BOT, "wrong")
    with pytest.raises(ChannelError, match="https"):
        matrix.password_login("http://matrix.example", BOT, "x")
    with pytest.raises(ChannelError, match="Matrix ID"):
        matrix.password_login(api.url, "ceo-bot", "x")


def test_write_env_value_replaces_keeps_others_and_stays_private(tmp_path):
    import os
    import stat

    env = tmp_path / "env"
    env.write_text("# comment\nCEO_MATRIX_ACCESS_TOKEN=old\nCEO_MATRIX_HUMAN=@me:x\n")
    env.chmod(0o644)
    matrix.write_env_value(env, "CEO_MATRIX_ACCESS_TOKEN", "syt_new")
    assert env.read_text() == "# comment\nCEO_MATRIX_ACCESS_TOKEN=syt_new\nCEO_MATRIX_HUMAN=@me:x\n"
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    matrix.write_env_value(env, "CEO_NEW", "1")
    assert env.read_text().endswith("CEO_NEW=1\n")
    assert [p.name for p in tmp_path.iterdir()] == ["env"]  # no temp files left


def test_write_env_value_rejects_line_breaks(tmp_path):
    with pytest.raises(ChannelError, match="line break"):
        matrix.write_env_value(tmp_path / "env", "K", "a\nCEO_EVIL=1")


def test_cli_login_writes_the_token_without_printing_it(api, monkeypatch, tmp_path, capsys):
    api.routes[("POST", f"{API}/login")] = (200, {"access_token": "syt_secret_new", "device_id": "D1", "user_id": BOT})
    env = tmp_path / "env"
    env.write_text("CEO_MATRIX_ACCESS_TOKEN=old\n")
    monkeypatch.setenv("CEO_MATRIX_HOMESERVER", api.url)
    monkeypatch.setenv("CEO_ENV_FILE", str(env))
    monkeypatch.setattr(matrix.getpass, "getpass", lambda prompt: "hunter2")
    assert matrix.main(["login", BOT]) == 0
    out = capsys.readouterr().out
    assert "syt_secret_new" not in out and "hunter2" not in out
    assert "token expiry: none (long-lived)" in out
    assert env.read_text() == "CEO_MATRIX_ACCESS_TOKEN=syt_secret_new\n"


def test_cli_login_creates_a_missing_settings_directory_privately(api, monkeypatch, tmp_path):
    import os
    import stat

    api.routes[("POST", f"{API}/login")] = (200, {"access_token": "syt_new", "device_id": "D1"})
    env = tmp_path / "fresh" / "ceo" / "env"
    monkeypatch.setenv("CEO_MATRIX_HOMESERVER", api.url)
    monkeypatch.setenv("CEO_ENV_FILE", str(env))
    monkeypatch.setattr(matrix.getpass, "getpass", lambda prompt: "hunter2")
    assert matrix.main(["login", BOT]) == 0
    assert env.read_text() == "CEO_MATRIX_ACCESS_TOKEN=syt_new\n"
    assert stat.S_IMODE(os.stat(env.parent).st_mode) == 0o700


def test_cli_login_checks_the_settings_file_can_be_written_before_logging_in(api, monkeypatch, tmp_path, capsys):
    # The access check is stubbed rather than using a read-only directory:
    # root can write anywhere, so a real permission test depends on who runs it.
    monkeypatch.setenv("CEO_MATRIX_HOMESERVER", api.url)
    monkeypatch.setenv("CEO_ENV_FILE", str(tmp_path / "ceo" / "env"))
    monkeypatch.setattr(matrix.os, "access", lambda path, mode: False)
    monkeypatch.setattr(matrix.getpass, "getpass", lambda prompt: pytest.fail("asked for the password"))
    assert matrix.main(["login", BOT]) == 1
    assert "can't write" in capsys.readouterr().err
    assert not [r for r in api.requests if r[0] == "POST"]  # never logged in


def test_cli_login_prints_nothing_from_the_server_response(api, monkeypatch, tmp_path, capsys):
    api.routes[("POST", f"{API}/login")] = (
        200, {"access_token": "syt_x", "device_id": "DEVICE_FROM_SERVER", "user_id": "@spoofed:x", "expires_in_ms": 300000},
    )
    monkeypatch.setenv("CEO_MATRIX_HOMESERVER", api.url)
    monkeypatch.setenv("CEO_ENV_FILE", str(tmp_path / "env"))
    monkeypatch.setattr(matrix.getpass, "getpass", lambda prompt: "hunter2")
    assert matrix.main(["login", BOT]) == 0
    out = capsys.readouterr().out
    assert "DEVICE_FROM_SERVER" not in out and "@spoofed" not in out and "300000" not in out
    assert f"logged in as {BOT}" in out and "it expires and will need refreshing" in out
