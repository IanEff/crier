import tomllib
from pathlib import Path

import pytest

from crier.config import ConfigError, parse
from crier.guard import GuardError, check_llm_url, check_zulip_url
from tests.conftest import raw_config

ROOT = Path(__file__).parent.parent


@pytest.mark.parametrize("url", [
    "https://llm.recurse.com/v1",
    "https://LLM.recurse.com",
    "https://llm.rcdis.co/v1",
    "http://localhost:11434/v1",
    "http://127.0.0.1:8080",
])
def test_llm_allowed(url):
    check_llm_url(url)


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1",
    "https://api.anthropic.com",
    "http://llm.recurse.com/v1",                 # plain http off-machine
    "https://llm.recurse.com.evil.example/v1",   # suffix trick
    "https://evil.rcdis.co/v1",                  # sibling host on the same domain
    "https://gateway.resilient-reaper-55635.ondis.co",  # the droplet's Disco name
    "https://llm.recurse.com@evil.example/v1",   # userinfo trick
    "https://openrouter.ai/api/v1",
    "",
])
def test_llm_refused(url):
    with pytest.raises(GuardError):
        check_llm_url(url)


def test_zulip_only_rc():
    check_zulip_url("https://recurse.zulipchat.com")
    for bad in ("http://recurse.zulipchat.com", "https://chat.zulip.org", "https://evil.example"):
        with pytest.raises(GuardError):
            check_zulip_url(bad)


def test_parse_minimal(tmp_path):
    cfg = parse(raw_config(str(tmp_path)))
    assert cfg.channels.all() == ("checkins", "hub-happenings", "events-board", "rust-corner")
    assert cfg.channels.not_checkins() == ("hub-happenings", "events-board", "rust-corner")
    assert cfg.output.state_path == tmp_path / ".crier"


def test_parse_refuses_hosted_llm(tmp_path):
    raw = raw_config(str(tmp_path))
    raw["llm"]["base_url"] = "https://api.openai.com/v1"
    with pytest.raises(GuardError):
        parse(raw)


def test_parse_errors(tmp_path):
    raw = raw_config(str(tmp_path))
    raw["llm"]["api"] = "gemini"
    with pytest.raises(ConfigError):
        parse(raw)
    raw = raw_config(str(tmp_path))
    raw["channels"] = {}
    with pytest.raises(ConfigError, match="no channels"):
        parse(raw)
    raw = raw_config(str(tmp_path))
    raw["digest"]["timezone"] = "Mars/Olympus"
    with pytest.raises(ConfigError, match="time zone"):
        parse(raw)


def test_example_config_parses():
    with (ROOT / "config.example.toml").open("rb") as f:
        cfg = parse(tomllib.load(f))
    assert cfg.llm.base_url == "https://llm.rcdis.co/v1"
    assert cfg.llm.model == "default"
    assert cfg.llm.reasoning_effort == "low"
    assert cfg.digest.active_hours == (8, 23)
    assert cfg.channels.checkins == ("checkins",)


def test_api_key_env(monkeypatch, tmp_path):
    raw = raw_config(str(tmp_path))
    raw["llm"]["api_key_env"] = "CRIER_TEST_KEY"
    cfg = parse(raw)
    monkeypatch.delenv("CRIER_TEST_KEY", raising=False)
    with pytest.raises(ConfigError, match="CRIER_TEST_KEY"):
        cfg.llm.api_key()
    monkeypatch.setenv("CRIER_TEST_KEY", " sk-local ")
    assert cfg.llm.api_key() == "sk-local"


def test_api_key_cmd(tmp_path):
    raw = raw_config(str(tmp_path))
    raw["llm"]["api_key_cmd"] = "echo from-keychain"
    assert parse(raw).llm.api_key() == "from-keychain"
    raw["llm"]["api_key_cmd"] = "false"
    with pytest.raises(ConfigError, match="exit 1"):
        parse(raw).llm.api_key()
