"""A small read-only client for the Zulip REST API.

Only what crier needs: who am I, and the messages in a channel before or
after a point. https://zulip.com/api/get-messages
"""

from __future__ import annotations

import configparser
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import httpx

from crier import USER_AGENT
from crier.guard import check_zulip_url

MAX_TRIES = 5


class ZulipError(Exception):
    """Zulip said no, or couldn't be reached."""


@dataclass(frozen=True)
class Message:
    id: int
    stream: str
    stream_id: int
    topic: str
    sender: str
    sender_email: str
    timestamp: int
    content: str

    @classmethod
    def from_api(cls, m: dict) -> Message:
        return cls(
            id=int(m["id"]),
            stream=m["display_recipient"],
            stream_id=int(m["stream_id"]),
            topic=m["subject"],
            sender=m["sender_full_name"],
            sender_email=m.get("sender_email", ""),
            timestamp=int(m["timestamp"]),
            content=m["content"],
        )


def read_zuliprc(path: Path) -> tuple[str, str]:
    """(email, api_key) from a zuliprc, the file Zulip's settings page downloads."""
    cp = configparser.ConfigParser()
    if not cp.read(path):
        raise ZulipError(f"can't read zuliprc at {path}")
    try:
        api = cp["api"]
        return api["email"], api["key"]
    except KeyError as e:
        raise ZulipError(f"{path}: needs an [api] section with email and key") from e


class ZulipClient:
    def __init__(
        self,
        site: str,
        email: str,
        api_key: str,
        *,
        page_size: int = 500,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        check_zulip_url(site)
        self.site = site.rstrip("/")
        self._page = max(1, min(page_size, 1000))
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=self.site + "/api/v1",
            auth=(email, api_key),
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,  # no proxies, netrc or SSL_CERT_FILE from the environment
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(MAX_TRIES):
            try:
                r = self._http.get(path, params=params)
            except httpx.HTTPError as e:
                raise ZulipError(f"GET {path}: {type(e).__name__}") from e
            if r.status_code == 429:
                self._sleep(_retry_after(r, attempt))
                continue
            try:
                data = r.json()
            except ValueError:
                raise ZulipError(f"GET {path}: HTTP {r.status_code}, not JSON") from None
            if r.status_code != 200 or data.get("result") != "success":
                msg = str(data.get("msg", ""))[:200]
                raise ZulipError(f"GET {path}: HTTP {r.status_code}: {msg}")
            return data
        raise ZulipError(f"GET {path}: still rate-limited after {MAX_TRIES} tries")

    def me(self) -> dict:
        return self._get("/users/me")

    def check_channel(self, channel: str) -> None:
        """Raise ZulipError if Zulip doesn't know `channel`.

        Fetches at most the newest message and throws it away; nothing is
        stored or printed.
        """
        self._messages(channel, "newest", 0, 0, True)

    def _messages(
        self, channel: str, anchor: int | str, before: int, after: int, include_anchor: bool
    ) -> tuple[list[Message], dict]:
        data = self._get(
            "/messages",
            {
                "anchor": str(anchor),
                "num_before": before,
                "num_after": after,
                "include_anchor": include_anchor,
                "narrow": json.dumps([{"operator": "channel", "operand": channel}]),
                "apply_markdown": False,
            },
        )
        msgs = [Message.from_api(m) for m in data["messages"] if m.get("type") == "stream"]
        return msgs, data

    def messages_after(self, channel: str, after_id: int) -> list[Message]:
        """Every message in `channel` newer than `after_id`, oldest first."""
        out: list[Message] = []
        anchor: int = after_id
        while True:
            msgs, data = self._messages(channel, anchor, 0, self._page, False)
            out.extend(msgs)
            raw = data["messages"]
            if data.get("found_newest") or not raw:
                return out
            anchor = raw[-1]["id"]

    def narrow_messages(self, narrow: list[dict], limit: int) -> tuple[list[dict], bool]:
        """Raw messages matching `narrow`, oldest first, and whether `limit` cut it short.

        Raw dicts rather than Message, because a DM narrow returns private
        messages, whose display_recipient is a list of people.
        """
        out: list[dict] = []
        anchor: int | str = "oldest"
        include = True
        while len(out) < limit:
            data = self._get(
                "/messages",
                {
                    "anchor": str(anchor),
                    "num_before": 0,
                    "num_after": min(self._page, limit - len(out)),
                    "include_anchor": include,
                    "narrow": json.dumps(narrow),
                    "apply_markdown": False,
                },
            )
            raw = data["messages"]
            out.extend(raw)
            if data.get("found_newest") or not raw:
                return out, False
            anchor, include = raw[-1]["id"], False
        return out, True

    def messages_since(self, channel: str, since_ts: int) -> list[Message]:
        """Every message in `channel` sent at or after `since_ts`, oldest first."""
        out: list[Message] = []
        anchor: int | str = "newest"
        include = True
        while True:
            msgs, data = self._messages(channel, anchor, self._page, 0, include)
            out[:0] = [m for m in msgs if m.timestamp >= since_ts]
            raw = data["messages"]
            if data.get("found_oldest") or not raw or raw[0]["timestamp"] < since_ts:
                return out
            anchor, include = raw[0]["id"], False


def _retry_after(r: httpx.Response, attempt: int) -> float:
    try:
        wait = float(r.headers.get("Retry-After", ""))
    except ValueError:
        try:
            wait = float(r.json().get("retry-after", 0))
        except (ValueError, AttributeError):
            wait = 0
    return min(max(wait, 2**attempt), 60)
