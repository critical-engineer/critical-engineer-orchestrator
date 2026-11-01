import pytest
from fake_http import FakeHttpApi

from ceo.channels.signal_api import SignalChannel
from ceo.notify import ChannelError

BOT = "+447700900001"
HUMAN = "+447700900002"


@pytest.fixture
def api():
    fake = FakeHttpApi()
    fake.routes[("POST", "/v2/send")] = (201, {})
    yield fake
    fake.close()


@pytest.fixture
def channel(api):
    return SignalChannel(api_url=api.url, bot_number=BOT, human_number=HUMAN, timeout=2)


def test_from_env():
    env = {"CEO_SIGNAL_BOT_NUMBER": BOT, "CEO_SIGNAL_HUMAN_NUMBER": HUMAN}
    assert SignalChannel.from_env(env) == SignalChannel("http://127.0.0.1:9922", BOT, HUMAN)


def test_from_env_requires_numbers():
    with pytest.raises(ChannelError, match="CEO_SIGNAL_HUMAN_NUMBER"):
        SignalChannel.from_env({"CEO_SIGNAL_BOT_NUMBER": BOT})


def test_from_env_checks_transport():
    env = {
        "CEO_SIGNAL_API_URL": "http://192.168.1.10:9922",
        "CEO_SIGNAL_BOT_NUMBER": BOT,
        "CEO_SIGNAL_HUMAN_NUMBER": HUMAN,
    }
    with pytest.raises(ChannelError, match="https"):
        SignalChannel.from_env(env)


@pytest.mark.parametrize(
    "number",
    ["07700900002", "+44 7700 900002", "+0123456789", "", "+447700900002\n", "+４４7700900002"],
)
def test_rejects_non_e164(number):
    with pytest.raises(ChannelError, match="E.164"):
        SignalChannel("http://127.0.0.1", BOT, number)


def test_bot_needs_its_own_number():
    with pytest.raises(ChannelError, match="own number"):
        SignalChannel("http://127.0.0.1", HUMAN, HUMAN)


def test_send_posts_to_the_configured_human_only(api, channel):
    channel.send("hello")
    [(method, path, _, body)] = api.requests
    assert (method, path, body) == (
        "POST", "/v2/send", {"message": "hello", "number": BOT, "recipients": [HUMAN]}
    )


def test_send_raises_on_http_error(api, channel):
    api.routes[("POST", "/v2/send")] = (400, {})
    with pytest.raises(ChannelError, match="HTTP 400"):
        channel.send("hello")


def test_send_raises_when_api_unreachable(api, channel):
    api.close()
    with pytest.raises(ChannelError, match="send failed"):
        channel.send("hello")
