"""End to end through the CLI, with Zulip and the model faked out."""

import tomllib
from datetime import datetime

import pytest

from crier import cli
from crier.llm import LLM
from crier.zulip import ZulipClient
from tests.conftest import FIXTURE, NOW, SECRET_WORDS, FakeLLM, FakeZulip, raw_config


def toml_dump(raw: dict) -> str:
    lines = []
    for section, values in raw.items():
        lines.append(f"[{section}]")
        for k, v in values.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            elif isinstance(v, list):
                lines.append(f"{k} = [" + ", ".join(f'"{x}"' for x in v) + "]")
            else:
                lines.append(f"{k} = {v}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    rc = tmp_path / "zuliprc"
    rc.write_text("[api]\nemail=me@example.com\nkey=abc\n")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(toml_dump(raw_config(str(vault), str(rc))))
    tomllib.loads(cfg_path.read_text())  # sanity: our tiny dumper wrote valid TOML

    zulip, llm = FakeZulip(FIXTURE), FakeLLM()
    monkeypatch.setattr(cli, "zulip_client", lambda cfg: ZulipClient(
        cfg.zulip.site, "me@example.com", "abc", transport=zulip.transport))
    monkeypatch.setattr(cli, "make_llm", lambda cfg: LLM(cfg.llm, transport=llm.transport))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW

    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    return cfg_path, vault, zulip, llm


def test_run_writes_pages_and_never_prints_messages(setup, capsys):
    cfg_path, vault, _zulip, _llm = setup
    assert cli.main(["--config", str(cfg_path), "run"]) == 0
    out = capsys.readouterr().out
    for word in SECRET_WORDS:
        assert word not in out, word
    assert "fetched 6 new messages from 4 channels" in out

    digest = vault / "Areas/rc/digest"
    now_page = (digest / "zulip-now.md").read_text()
    day_page = (digest / "zulip-2026-11-07.md").read_text()
    assert now_page.startswith("---\ncreated: 2026-11-07\nmodified: 2026-11-07\ntype: reference\n")
    assert "kind: digest" in now_page
    assert "## Right now at the hub" in now_page and "## Coming up" in now_page
    assert "[[zulip-2026-11-07]]" in now_page
    for heading in ("## For you", "## Check-ins", "## Hub today"):
        assert heading in day_page
    assert "recurse.zulipchat.com/#narrow/channel/10-checkins/topic/Ada.20Lovelace/near/101" in day_page
    assert (vault / ".crier/crier.sqlite3").exists()
    assert not list(digest.glob(".*.tmp"))


def test_second_run_is_incremental_and_cached(setup, capsys):
    cfg_path, _vault, zulip, llm = setup
    cli.main(["--config", str(cfg_path), "run"])
    calls, reqs = len(llm.prompts), len(zulip.requests)
    capsys.readouterr()
    assert cli.main(["--config", str(cfg_path), "run"]) == 0
    assert "fetched 0 new messages" in capsys.readouterr().out
    assert len(llm.prompts) == calls  # nothing new: no model calls
    # Second fetch uses cursors: one request per channel, all anchored on an id.
    second = zulip.requests[reqs:]
    assert len(second) == 4 and all(r.url.params["anchor"] != "newest" for r in second)


def test_created_survives_rewrites(setup):
    cfg_path, vault, _zulip, _llm = setup
    page = vault / "Areas/rc/digest/zulip-now.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ncreated: 2026-11-01\nmodified: 2026-11-01\n---\nold\n")
    cli.main(["--config", str(cfg_path), "run"])
    text = page.read_text()
    assert "created: 2026-11-01\nmodified: 2026-11-07" in text and "old" not in text


def test_bad_channel_reported_and_others_still_fetched(setup, capsys, tmp_path):
    cfg_path, _vault, _zulip, _llm = setup
    cfg_path.write_text(cfg_path.read_text().replace('others = ["rust-corner"]', 'others = ["nope"]'))
    assert cli.main(["--config", str(cfg_path), "run"]) == 1
    out = capsys.readouterr().out
    assert "#nope: GET /messages: HTTP 400: Invalid channel name 'nope'" in out
    assert "fetched 5 new messages" in out


def test_check(setup, capsys):
    cfg_path, *_ = setup
    assert cli.main(["--config", str(cfg_path), "check"]) == 0
    out = capsys.readouterr().out
    assert "zulip: ok" in out and "llm: ok (GLM-5.3-Flash" in out
    assert "#checkins: ok" in out and "#rust-corner: ok" in out


def test_check_catches_bad_channel(setup, capsys):
    cfg_path, *_ = setup
    cfg_path.write_text(cfg_path.read_text().replace('others = ["rust-corner"]', 'others = ["nope"]'))
    assert cli.main(["--config", str(cfg_path), "check"]) == 1
    out = capsys.readouterr().out
    assert "#nope: GET /messages: HTTP 400" in out and "#checkins: ok" in out
    for word in SECRET_WORDS:
        assert word not in out


def test_missing_config(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "nope.toml"), "run"]) == 2
    assert "no config at" in capsys.readouterr().out


def test_active_hours(setup, capsys):
    cfg_path, *_ = setup
    cfg_path.write_text(cfg_path.read_text().replace(
        '[digest]\n', '[digest]\nactive_hours = [20, 23]\n'))
    assert cli.main(["--config", str(cfg_path), "run"]) == 0
    assert "outside active hours" in capsys.readouterr().out


def test_first_fetch_of_events_reaches_back_to_events_window(setup, capsys):
    from crier.config import load

    cfg_path, *_ = setup
    cfg = load(cfg_path)
    assert cli.first_lookback_hours(cfg, "events-board") == 72
    assert cli.first_lookback_hours(cfg, "EVENTS-board") == 72
    assert cli.first_lookback_hours(cfg, "hub-happenings") == 24


def test_backfill_ignores_cursors_without_duplicating(setup, capsys):
    import sqlite3

    cfg_path, vault, zulip, _llm = setup
    cli.main(["--config", str(cfg_path), "run"])
    reqs = len(zulip.requests)
    capsys.readouterr()
    assert cli.main(["--config", str(cfg_path), "run", "--backfill", "72"]) == 0
    assert "backfilled 72h: 6 new messages from 4 channels" in capsys.readouterr().out
    assert all(r.url.params["anchor"] == "newest" for r in zulip.requests[reqs:])
    db = sqlite3.connect(vault / ".crier/crier.sqlite3")
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 6
