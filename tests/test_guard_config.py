import tomllib
from pathlib import Path

import httpx
import pytest

from crier.config import ConfigError, parse
from crier.guard import GuardError, check_llm_url, check_zulip_url
from crier.llm import LLM, LLMError
from crier.push import Pusher
from crier.store import Store
from crier.zulip import ZulipClient
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


# --- where the LLM key may go ---

def _keyed_request(base_url: str, api: str = "openai", reply=None) -> httpx.Request:
    seen = []

    def handler(request):
        seen.append(request)
        return reply or httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    cfg = parse({**raw_config("/tmp/v"), "llm": {"api": api, "base_url": base_url, "model": "m"}})
    llm = LLM(cfg.llm, api_key="sk-secret-123", transport=httpx.MockTransport(handler))
    try:
        llm.complete("sys", "user")
    except LLMError:
        pass
    return seen[0]


@pytest.mark.parametrize("api", ["openai", "anthropic"])
def test_key_goes_to_rc(api):
    r = _keyed_request("https://llm.rcdis.co/v1", api)
    assert "sk-secret-123" in (r.headers.get("authorization", "") + r.headers.get("x-api-key", ""))


@pytest.mark.parametrize("api", ["openai", "anthropic"])
@pytest.mark.parametrize("url", ["http://localhost:11434/v1", "https://127.0.0.1:8080"])
def test_key_stays_home_for_local_models(api, url):
    r = _keyed_request(url, api)
    assert "authorization" not in r.headers and "x-api-key" not in r.headers


def test_key_redacted_from_server_errors():
    reply = httpx.Response(401, json={"error": {"message": "bad key sk-secret-123, try again"}})
    cfg = parse(raw_config("/tmp/v"))
    llm = LLM(cfg.llm, api_key="sk-secret-123", transport=httpx.MockTransport(lambda r: reply))
    with pytest.raises(LLMError) as e:
        llm.complete("sys", "user")
    assert "sk-secret-123" not in str(e.value)
    assert str(e.value) == "HTTP 401: bad key [redacted], try again"


def test_clients_ignore_environment_proxies(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    raw = raw_config(str(tmp_path))
    raw["push"] = {"server": "http://127.0.0.1:2586", "topic": "t"}
    cfg = parse(raw)
    clients = [
        LLM(cfg.llm)._http,
        ZulipClient(cfg.zulip.site, "me@example.com", "k")._http,
        Pusher(cfg, Store(tmp_path / "s.sqlite3"))._http,
    ]
    for c in clients:
        assert c.trust_env is False
        assert not c._mounts  # no proxy transports were set up
