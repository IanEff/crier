import json

import httpx
import pytest

from crier import cli
from crier.config import ConfigError, parse
from crier.digest import Digester
from crier.guard import GuardError, check_push_url
from crier.llm import LLM
from crier.push import Pusher, PushError, clean, headlines
from crier.store import Store
from crier.zulip import Message
from tests.conftest import FIXTURE, NOW, SECRET_WORDS, T0, FakeLLM, raw_config, zmsg
from tests.test_cli import setup  # noqa: F401  (fixture)

PUSH = {"server": "http://127.0.0.1:2586", "topic": "crier-test"}


class FakeNtfy:
    def __init__(self):
        self.sent: list[dict] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/"
        self.sent.append(json.loads(request.content))
        return httpx.Response(self.status, json={"id": "x"})

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


def push_cfg(tmp_path, **extra):
    raw = raw_config(str(tmp_path / "vault"))
    raw["push"] = {**PUSH, **extra}
    return parse(raw)


def results_for(cfg, store, fake_llm, msgs):
    store.add(msgs)
    d = Digester(cfg, store, LLM(cfg.llm, transport=fake_llm.transport), NOW)
    return d.now_sections() + d.day_sections(NOW.date())


@pytest.fixture
def world(tmp_path):
    cfg = push_cfg(tmp_path)
    store = Store(cfg.output.state_path / "crier.sqlite3")
    ntfy = FakeNtfy()
    pusher = Pusher(cfg, store, transport=ntfy.transport)
    return cfg, store, ntfy, pusher, FakeLLM()


def test_push_only_to_localhost():
    check_push_url("http://127.0.0.1:2586")
    check_push_url("http://localhost:2586")
    for bad in ("https://ntfy.sh", "http://mac.tail1234.ts.net:2586", "http://100.101.102.103:2586"):
        with pytest.raises(GuardError):
            check_push_url(bad)


def test_push_config(tmp_path):
    assert parse(raw_config(str(tmp_path))).push is None
    cfg = push_cfg(tmp_path)
    assert cfg.push.sections == ("hub-now", "coming-up", "for-you") and cfg.push.max_per_run == 5
    with pytest.raises(GuardError):
        push_cfg(tmp_path, server="https://ntfy.sh")
    with pytest.raises(ConfigError, match="unknown"):
        push_cfg(tmp_path, sections=["checkins"])
    assert push_cfg(tmp_path, enabled=False).push is None


def test_clean():
    assert clean("- **Fri 4pm**: a talk (main space) [m104]") == "Fri 4pm: a talk (main space)"
    assert clean("  * dumplings at 12:30 [m103, m105] ") == "dumplings at 12:30"
    assert clean("- lunch ![](https://evil.example/p) [here](https://evil.example) [m103]") == "lunch here"
    assert clean("- talk https://evil.example/x at 4 [m104]") == "talk (link) at 4"


def test_headlines_skip_unsourced_and_checkins(world):
    cfg, store, _ntfy, _pusher, llm = world
    results = results_for(cfg, store, llm, [Message.from_api(m) for m in FIXTURE])
    hs = headlines(results, cfg.push, cfg.zulip.site)
    assert {h.section for h in hs} == {"hub-now", "coming-up", "for-you"}
    assert all(h.ids and 999999 not in h.ids for h in hs)  # the invented citation never pushes
    assert not any(101 in h.ids or 102 in h.ids for h in hs)  # check-ins don't push
    assert hs[0].url.startswith("https://recurse.zulipchat.com/#narrow/")


def test_first_run_primes_then_only_new_messages_push(world):
    cfg, store, ntfy, pusher, llm = world
    results = results_for(cfg, store, llm, [Message.from_api(m) for m in FIXTURE])
    assert pusher.run(results) == (0, 0)  # primed: today's backlog stays quiet
    assert ntfy.sent == []
    assert pusher.run(results) == (0, 0)  # nothing new

    new = Message.from_api(zmsg(107, "hub-happenings", "pizza", "Ada Lovelace", T0 + 5 * 3600 + 60,
                                "Pizza on 4 now."))
    results = results_for(cfg, store, llm, [new])
    sent, covered = pusher.run(results)
    assert (sent, covered) == (1, 1)  # 107 is in hub-now, for-you and hub-today, but pushes once
    n = ntfy.sent[0]
    assert n["topic"] == "crier-test" and n["title"] == "At the hub now" and n["priority"] == 4
    assert n["message"] == "item from message 107"
    assert n["click"].endswith("/topic/pizza/near/107")
    assert pusher.run(results) == (0, 0)


def test_overflow_folds_into_one(tmp_path):
    cfg = push_cfg(tmp_path, max_per_run=2)
    store = Store(cfg.output.state_path / "crier.sqlite3")
    ntfy = FakeNtfy()
    pusher = Pusher(cfg, store, transport=ntfy.transport)
    llm = FakeLLM()
    pusher.run(results_for(cfg, store, llm, []))  # prime on an empty day
    results = results_for(cfg, store, llm, [Message.from_api(m) for m in FIXTURE])
    sent, covered = pusher.run(results)
    assert sent == 2 and covered == 4  # 103, 105 (hub), 104 (events), 106 (for-you)
    assert ntfy.sent[-1]["message"] == "…and 3 more in the digest."
    assert pusher.run(results) == (0, 0)


def test_failed_push_retries_next_run(world):
    cfg, store, ntfy, pusher, llm = world
    pusher.run(results_for(cfg, store, llm, []))
    results = results_for(cfg, store, llm, [Message.from_api(m) for m in FIXTURE])
    ntfy.status = 500
    with pytest.raises(PushError, match="HTTP 500"):
        pusher.run(results)
    ntfy.status = 200
    ntfy.sent.clear()
    assert pusher.run(results)[1] == 4


def test_obsidian_action(tmp_path):
    cfg = push_cfg(tmp_path, obsidian_vault="recurse vault")
    pusher = Pusher(cfg, Store(cfg.output.state_path / "s.sqlite3"))
    assert pusher.digest_url() == (
        "obsidian://open?vault=recurse%20vault&file=Areas/rc/digest/zulip-now"
    )


def test_cli_run_pushes_and_prints_counts_only(setup, capsys, monkeypatch):  # noqa: F811
    cfg_path, _vault, zulip, _llm = setup
    cfg_path.write_text(cfg_path.read_text() + '[push]\nserver = "http://127.0.0.1:2586"\ntopic = "t"\n')
    ntfy = FakeNtfy()
    real = cli.Pusher
    monkeypatch.setattr(cli, "Pusher", lambda cfg, store: real(cfg, store, transport=ntfy.transport))

    assert cli.main(["--config", str(cfg_path), "run"]) == 0
    assert "push: 0 notifications (0 headlines)" in capsys.readouterr().out

    zulip.messages.append(zmsg(107, "hub-happenings", "pizza", "Ada Lovelace", T0 + 5 * 3600 + 60,
                               "Pizza on 4 now."))
    assert cli.main(["--config", str(cfg_path), "run"]) == 0
    out = capsys.readouterr().out
    assert "push: 1 notifications (1 headlines)" in out
    for word in [*SECRET_WORDS, "Pizza", "item from message"]:
        assert word not in out
    assert len(ntfy.sent) == 1

    assert cli.main(["--config", str(cfg_path), "push-test"]) == 0
    assert ntfy.sent[-1]["title"] == "crier"
