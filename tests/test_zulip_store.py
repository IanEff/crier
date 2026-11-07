import pytest

from crier.store import Store
from crier.zulip import Message, ZulipClient, ZulipError, read_zuliprc
from tests.conftest import FIXTURE, T0, FakeZulip, zmsg


def client(fake: FakeZulip, page_size: int = 500, sleeps: list | None = None) -> ZulipClient:
    return ZulipClient(
        "https://recurse.zulipchat.com", "me@example.com", "k",
        page_size=page_size, transport=fake.transport,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


def many(n: int) -> list[dict]:
    return [zmsg(1000 + i, "hub-happenings", "t", "Ada Lovelace", T0 + i * 60, f"m{i}") for i in range(n)]


def test_messages_since_pages_back_and_stops(fake_zulip):
    fake = FakeZulip(many(7))
    got = client(fake, page_size=2).messages_since("hub-happenings", T0 + 3 * 60)
    assert [m.id for m in got] == [1003, 1004, 1005, 1006]
    assert len(fake.requests) == 3  # 1005-1006, 1003-1004, 1001-1002 (crosses since_ts, stop)


def test_messages_after_pages_forward():
    fake = FakeZulip(many(7))
    got = client(fake, page_size=3).messages_after("hub-happenings", 1001)
    assert [m.id for m in got] == [1002, 1003, 1004, 1005, 1006]


def test_messages_after_nothing_new():
    fake = FakeZulip(many(3))
    assert client(fake).messages_after("hub-happenings", 1002) == []


def test_rate_limit_retries():
    fake = FakeZulip(many(2))
    fake.rate_limit_next = 2
    sleeps: list[float] = []
    got = client(fake, sleeps=sleeps).messages_since("hub-happenings", 0)
    assert len(got) == 2
    assert len(sleeps) == 2


def test_bad_channel_is_an_error(fake_zulip):
    with pytest.raises(ZulipError, match="Invalid channel name"):
        client(fake_zulip).messages_since("no-such-channel", 0)


def test_auth_is_basic(fake_zulip):
    client(fake_zulip).me()
    assert fake_zulip.requests[0].headers["authorization"].startswith("Basic ")


def test_read_zuliprc(tmp_path):
    rc = tmp_path / "zuliprc"
    rc.write_text("[api]\nemail=me@example.com\nkey=abc\nsite=https://recurse.zulipchat.com\n")
    assert read_zuliprc(rc) == ("me@example.com", "abc")
    with pytest.raises(ZulipError):
        read_zuliprc(tmp_path / "missing")


def test_store_roundtrip(tmp_path):
    s = Store(tmp_path / "s.sqlite3")
    msgs = [Message.from_api(m) for m in FIXTURE]
    assert s.add(msgs) == len(msgs)
    assert s.add(msgs[:1]) == 1  # re-adding replaces, doesn't duplicate
    hub = s.window(["HUB-happenings"], 0)
    assert [m.id for m in hub] == [103, 105]
    assert [m.id for m in s.window(["checkins"], T0 + 90)] == [102]
    assert s.window([], 0) == []


def test_store_cursor_only_moves_forward(tmp_path):
    s = Store(tmp_path / "s.sqlite3")
    assert s.cursor("checkins") is None
    s.set_cursor("Checkins", 50)
    s.set_cursor("checkins", 40)
    assert s.cursor("checkins") == 50


def test_store_prune_and_cache(tmp_path):
    s = Store(tmp_path / "s.sqlite3")
    s.add(Message.from_api(m) for m in FIXTURE)
    assert s.prune(T0 + 150) == 2  # the two check-ins
    assert s.cached("k", "h") is None
    s.remember("k", "h", "out")
    assert s.cached("k", "h") == "out"
    assert s.cached("k", "other") is None


def test_unicode_channel_names(tmp_path):
    """Emoji (with a zero-width joiner) and non-ASCII capitals survive fetch, store and window."""
    import json
    import tomllib

    from tests.conftest import STREAM_IDS

    name = tomllib.loads('n = "\\U0001F9D1\\u200D\\U0001F4BB current batches"')["n"]
    assert name == "🧑‍💻 current batches" and "‍" in name
    STREAM_IDS[name] = 50
    STREAM_IDS["Écrire"] = 60
    try:
        fake = FakeZulip([
            zmsg(1, name, "hello", "Ada Lovelace", T0, "hi"),
            zmsg(2, "Écrire", "t", "Ada Lovelace", T0, "salut"),
        ])
        c = client(fake)
        got = c.messages_since(name, 0)
        assert [m.id for m in got] == [1]
        sent = json.loads(fake.requests[0].url.params["narrow"])[0]["operand"]
        assert sent == name
        s = Store(tmp_path / "s.sqlite3")
        s.add(got + c.messages_since("Écrire", 0))
        assert [m.id for m in s.window([name.upper()], 0)] == [1]
        assert [m.id for m in s.window(["écrire"], 0)] == [2]
    finally:
        del STREAM_IDS[name], STREAM_IDS["Écrire"]


def test_unicode_channel_link():
    from crier.links import message_url

    m = Message.from_api({**zmsg(1, "checkins", "t", "A B", T0, "x"), "display_recipient": "🧑‍💻 current batches"})
    assert message_url("https://z", m).startswith("https://z/#narrow/channel/10-.F0.9F.A7.91.E2.80.8D.F0.9F.92.BB.20current.20batches/")
