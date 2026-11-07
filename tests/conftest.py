"""Fakes for Zulip and the model. Every person and message here is invented."""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from crier.config import parse

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 7, 14, 30, tzinfo=TZ)
T0 = int(datetime(2026, 11, 7, 9, 0, tzinfo=TZ).timestamp())  # 9am the same day

STREAM_IDS = {"checkins": 10, "hub-happenings": 20, "events-board": 30, "rust-corner": 40}


def zmsg(id: int, channel: str, topic: str, sender: str, ts: int, content: str) -> dict:
    return {
        "id": id,
        "type": "stream",
        "display_recipient": channel,
        "stream_id": STREAM_IDS[channel],
        "subject": topic,
        "sender_full_name": sender,
        "sender_email": sender.lower().replace(" ", ".") + "@example.com",
        "timestamp": ts,
        "content": content,
    }


FIXTURE = [
    zmsg(101, "checkins", "Ada Lovelace", "Ada Lovelace", T0 + 60,
         "Today: finishing my LSM tree compaction, then pairing on Raft after lunch."),
    zmsg(102, "checkins", "Grace Hopper", "Grace Hopper", T0 + 120,
         "Writing a COBOL-to-Rust transpiler. Stuck on lifetimes."),
    zmsg(103, "hub-happenings", "lunch", "Edsger Dijkstra", T0 + 3 * 3600,
         "Ordering dumplings for the 4th floor at 12:30, reply if you want in."),
    zmsg(104, "events-board", "Fri talk", "Barbara Liskov", T0 + 3600,
         "Friday 4pm: talk on substitutability in the main space."),
    zmsg(105, "hub-happenings", "board games", "Ada Lovelace", T0 + 5 * 3600,
         "Settlers at 6pm on the couches, two seats left."),
    zmsg(106, "rust-corner", "borrowck", "Grace Hopper", T0 + 4 * 3600,
         "Anyone want to pair on the borrow checker and a linked list?"),
]

# Strings that must never show up on stdout.
SECRET_WORDS = ["dumplings", "Settlers", "substitutability", "COBOL", "Lovelace", "Hopper",
                "Dijkstra", "Liskov", "compaction"]


class FakeZulip:
    """Just enough of GET /messages to exercise anchors and paging."""

    def __init__(self, messages: list[dict]):
        self.messages = sorted(messages, key=lambda m: m["id"])
        self.requests: list[httpx.Request] = []
        self.rate_limit_next = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"result": "error"})
        path = request.url.path
        if path.endswith("/users/me"):
            return httpx.Response(200, json={"result": "success", "full_name": "Test User"})
        assert path.endswith("/messages"), path
        q = request.url.params
        channel = json.loads(q["narrow"])[0]["operand"]
        pool = [m for m in self.messages if m["display_recipient"].lower() == channel.lower()]
        if channel.lower() not in {k.lower() for k in STREAM_IDS}:
            return httpx.Response(400, json={"result": "error", "msg": f"Invalid channel name '{channel}'"})
        anchor = 10**18 if q["anchor"] == "newest" else int(q["anchor"])
        before, after = int(q["num_before"]), int(q["num_after"])
        include = q["include_anchor"] == "true"
        older = [m for m in pool if m["id"] < anchor]
        newer = [m for m in pool if m["id"] > anchor]
        at = [m for m in pool if m["id"] == anchor] if include else []
        page = (older[-before:] if before else []) + at + (newer[:after] if after else [])
        return httpx.Response(200, json={
            "result": "success",
            "messages": page,
            "found_oldest": len(older) <= before,
            "found_newest": len(newer) <= after,
        })

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeLLM:
    """Answers every prompt with one bullet per cited thread, plus one made-up citation."""

    def __init__(self):
        self.prompts: list[dict] = []
        self.fail_with: int | None = None
        self.reply_prefix = ""

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.prompts.append(body)
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"error": {"message": "model overloaded"}})
        user = body["messages"][-1]["content"]
        ids = re.findall(r"^\[m(\d+)\]", user, re.MULTILINE)
        if not ids:
            text = "NOTHING"
        else:
            text = "\n".join(f"- item from message {i} [m{i}]" for i in ids)
            text += "\n- an invented item [m999999]"
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": self.reply_prefix + text}}]
        })

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def raw_config(vault: str, zuliprc: str = "/nonexistent/zuliprc") -> dict:
    return {
        "zulip": {"site": "https://recurse.zulipchat.com", "zuliprc": zuliprc, "lookback_hours": 24},
        "llm": {"api": "openai", "base_url": "https://llm.recurse.com/v1", "model": "GLM-5.3-Flash"},
        "output": {"vault": vault},
        "channels": {
            "checkins": ["checkins"],
            "hub": ["hub-happenings"],
            "events": ["events-board"],
            "others": ["rust-corner"],
        },
        "me": {"profile": "Likes storage and Rust.", "interests": ["rust", "lsm", "pair"]},
        "digest": {"timezone": "America/New_York"},
    }


@pytest.fixture
def cfg(tmp_path):
    return parse(raw_config(str(tmp_path / "vault")))


@pytest.fixture
def fake_zulip():
    return FakeZulip(FIXTURE)


@pytest.fixture
def fake_llm():
    return FakeLLM()
