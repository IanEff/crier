"""Loading and checking ~/.config/crier/config.toml.

The config holds no secrets itself: the Zulip key lives in a zuliprc file and
the LLM key comes from an environment variable or a command (the macOS
keychain, say).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from crier.guard import check_llm_url, check_zulip_url

DEFAULT_CONFIG = Path("~/.config/crier/config.toml")


class ConfigError(Exception):
    """The config is missing, malformed, or points somewhere it mustn't."""


@dataclass(frozen=True)
class ZulipConfig:
    site: str
    zuliprc: Path
    lookback_hours: float = 24
    page_size: int = 500


@dataclass(frozen=True)
class LLMConfig:
    api: str  # "openai" or "anthropic"
    base_url: str
    model: str
    api_key_env: str | None = None
    api_key_cmd: str | None = None
    max_input_chars: int = 150_000
    max_output_tokens: int = 8_192
    temperature: float = 0.2
    reasoning_effort: str | None = "low"  # OpenAI-shape only; keeps reasoning out of the answer
    timeout: float = 300.0

    def api_key(self) -> str | None:
        """The key, from the command if set, else the env var, else None (a local model)."""
        if self.api_key_cmd:
            try:
                out = subprocess.run(
                    shlex.split(self.api_key_cmd), capture_output=True, text=True, check=False
                )
            except OSError as e:
                raise ConfigError(f"llm.api_key_cmd didn't run: {e}") from e
            if out.returncode != 0 or not out.stdout.strip():
                raise ConfigError(f"llm.api_key_cmd failed (exit {out.returncode})")
            return out.stdout.strip()
        if self.api_key_env:
            key = os.environ.get(self.api_key_env, "").strip()
            if not key:
                raise ConfigError(f"${self.api_key_env} is not set (llm.api_key_env)")
            return key
        return None


@dataclass(frozen=True)
class OutputConfig:
    vault: Path
    digest_dir: str = "Areas/rc/digest"
    state_dir: str = ".crier"

    @property
    def digest_path(self) -> Path:
        return self.vault / self.digest_dir

    @property
    def state_path(self) -> Path:
        return self.vault / self.state_dir


@dataclass(frozen=True)
class Channels:
    checkins: tuple[str, ...]
    hub: tuple[str, ...]
    events: tuple[str, ...]
    others: tuple[str, ...]

    def all(self) -> tuple[str, ...]:
        """Every watched channel, once each, in config order."""
        seen: dict[str, None] = {}
        for name in (*self.checkins, *self.hub, *self.events, *self.others):
            seen.setdefault(name, None)
        return tuple(seen)

    def not_checkins(self) -> tuple[str, ...]:
        skip = {c.lower() for c in self.checkins}
        return tuple(c for c in self.all() if c.lower() not in skip)


@dataclass(frozen=True)
class Me:
    profile: str
    interests: tuple[str, ...]


@dataclass(frozen=True)
class DigestConfig:
    timezone: str = "America/New_York"
    now_window_hours: float = 3
    events_lookback_hours: float = 72
    max_messages_per_section: int = 400
    max_message_chars: int = 1_500
    retention_days: int = 30
    active_hours: tuple[int, int] | None = None  # (start, end) local hours; None = always

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Config:
    zulip: ZulipConfig
    llm: LLMConfig
    output: OutputConfig
    channels: Channels
    me: Me
    digest: DigestConfig


def load(path: Path | None = None) -> Config:
    path = (path or DEFAULT_CONFIG).expanduser()
    if not path.exists():
        raise ConfigError(f"no config at {path}: copy config.example.toml there and fill it in")
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return parse(raw)


def _section(raw: dict, name: str) -> dict:
    sec = raw.get(name, {})
    if not isinstance(sec, dict):
        raise ConfigError(f"[{name}] must be a table")
    return sec


def _names(sec: dict, key: str) -> tuple[str, ...]:
    value = sec.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"channels.{key} must be a list of channel names")
    return tuple(v.strip().lstrip("#") for v in value if v.strip())


def _require(sec: dict, section: str, key: str) -> str:
    value = sec.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{section}.{key} is required")
    return value.strip()


def parse(raw: dict) -> Config:
    z = _section(raw, "zulip")
    site = z.get("site", "https://recurse.zulipchat.com").rstrip("/")
    zulip = ZulipConfig(
        site=site,
        zuliprc=Path(z.get("zuliprc", "~/.config/crier/zuliprc")).expanduser(),
        lookback_hours=float(z.get("lookback_hours", 24)),
        page_size=int(z.get("page_size", 500)),
    )

    l_ = _section(raw, "llm")
    api = l_.get("api", "openai")
    if api not in ("openai", "anthropic"):
        raise ConfigError('llm.api must be "openai" or "anthropic"')
    llm = LLMConfig(
        api=api,
        base_url=_require(l_, "llm", "base_url").rstrip("/"),
        model=_require(l_, "llm", "model"),
        api_key_env=l_.get("api_key_env") or None,
        api_key_cmd=l_.get("api_key_cmd") or None,
        max_input_chars=int(l_.get("max_input_chars", 150_000)),
        max_output_tokens=int(l_.get("max_output_tokens", 8_192)),
        reasoning_effort=l_.get("reasoning_effort", "low") or None,
        temperature=float(l_.get("temperature", 0.2)),
        timeout=float(l_.get("timeout", 300)),
    )

    o = _section(raw, "output")
    output = OutputConfig(
        vault=Path(_require(o, "output", "vault")).expanduser(),
        digest_dir=o.get("digest_dir", "Areas/rc/digest"),
        state_dir=o.get("state_dir", ".crier"),
    )

    c = _section(raw, "channels")
    channels = Channels(
        checkins=_names(c, "checkins"),
        hub=_names(c, "hub"),
        events=_names(c, "events"),
        others=_names(c, "others"),
    )
    if not channels.all():
        raise ConfigError("[channels] lists no channels to watch")

    m = _section(raw, "me")
    interests = m.get("interests", [])
    if not isinstance(interests, list):
        raise ConfigError("me.interests must be a list")
    me = Me(
        profile=str(m.get("profile", "")).strip(),
        interests=tuple(str(i).strip() for i in interests if str(i).strip()),
    )

    d = _section(raw, "digest")
    hours = d.get("active_hours")
    if hours is not None:
        if not (isinstance(hours, list) and len(hours) == 2 and all(isinstance(h, int) for h in hours)):
            raise ConfigError("digest.active_hours must be [start, end], e.g. [8, 23]")
        hours = (hours[0], hours[1])
    digest = DigestConfig(
        timezone=d.get("timezone", "America/New_York"),
        now_window_hours=float(d.get("now_window_hours", 3)),
        events_lookback_hours=float(d.get("events_lookback_hours", 72)),
        max_messages_per_section=int(d.get("max_messages_per_section", 400)),
        max_message_chars=int(d.get("max_message_chars", 1_500)),
        retention_days=int(d.get("retention_days", 30)),
        active_hours=hours,
    )
    try:
        ZoneInfo(digest.timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"digest.timezone {digest.timezone!r} is not a known time zone") from e

    check_zulip_url(zulip.site)
    check_llm_url(llm.base_url)
    return Config(zulip=zulip, llm=llm, output=output, channels=channels, me=me, digest=digest)
