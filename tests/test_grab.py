"""crier grab: links in, @Firstname transcripts out. Every person and message is invented."""

import json

import httpx
import pytest

from crier import cli
from crier.grab import (
    LinkError,
    People,
    Scrubber,
    find_links,
    llm_pass,
    parse_narrow,
    people_in,
    render,
)
from crier.zulip import ZulipClient
from tests.conftest import T0, TZ

LINK = ("https://recurse.zulipchat.com/#narrow/channel/40-rust-corner/topic/"
        "borrowck.20.2F.20linked.20lists/with/301")


def test_find_links_dedupes_and_ignores_other_hosts():
    page = f"see {LINK}, and ({LINK}) and https://evil.example/#narrow/channel/1-x/topic/y"
    assert find_links(page) == [LINK]


def test_parse_narrow_decodes_zulip_hash_encoding():
    assert parse_narrow(LINK) == [
        {"operator": "channel", "operand": 40},
        {"operator": "topic", "operand": "borrowck / linked lists"},
        {"operator": "with", "operand": 301},
    ]


def test_parse_narrow_drops_near_and_reads_dm_ids():
    assert parse_narrow("https://recurse.zulipchat.com/#narrow/stream/40-x/subject/y/near/9") == [
        {"operator": "channel", "operand": 40},
        {"operator": "topic", "operand": "y"},
    ]
    assert parse_narrow("https://recurse.zulipchat.com/#narrow/dm/7,8-group") == [
        {"operator": "dm", "operand": [7, 8]},
    ]


def test_parse_narrow_refuses_a_whole_channel():
    with pytest.raises(LinkError):
        parse_narrow("https://recurse.zulipchat.com/#narrow/channel/40-rust-corner")


def test_handles_add_last_initial_only_on_clashes():
    p = People({"Ada Lovelace (she/her)", "Ada Turing", "Grace Hopper", "Ada Lamarr"})
    h = p.handles()
    assert h["Grace Hopper"] == "@Grace"
    assert h["Ada Turing"] == "@AdaT"
    assert h["Ada Lovelace"] == h["Ada Lovelace (she/her)"] == "@AdaLovelace"
    assert h["Ada Lamarr"] == "@AdaLamarr"


def test_scrubber_rewrites_mentions_quotes_and_prose():
    s = Scrubber(People({"Grace Hopper", "Edsger Dijkstra"}).handles())
    text = ("@_**Grace Hopper|12** [said](https://recurse.zulipchat.com/#narrow/near/5):\n"
            "```quote\nhi\n```\n@**Edsger Dijkstra** and @**all**: Grace Hopper agrees.")
    assert s.text(text) == "@Grace said:\n```quote\nhi\n```\n@Edsger and @all: @Grace agrees."


def stream_msg(id, sender, ts, content, topic="borrowck / linked lists"):
    return {"id": id, "type": "stream", "display_recipient": "rust-corner", "stream_id": 40,
            "subject": topic, "sender_full_name": sender, "timestamp": ts, "content": content}


THREAD = [
    stream_msg(300, "Grace Hopper", T0, "Anyone want to pair on a linked list?"),
    stream_msg(301, "Grace Hopper", T0 + 60, "Cc @**Barbara Liskov|9**"),
    stream_msg(302, "Barbara Liskov", T0 + 3600, "Yes! Ask Edsger Dijkstra too."),
]


def test_render_groups_a_senders_run_and_scrubs_everything():
    scrub = Scrubber(people_in(THREAD).handles())
    out = render(THREAD, scrub, TZ)
    assert out.startswith("## #rust-corner > borrowck / linked lists\n")
    assert out.count("**@Grace**") == 1  # two messages a minute apart share a header
    assert "Cc @Barbara" in out
    for surname in ("Hopper", "Liskov"):
        assert surname not in out
    # Not a sender or a mention, so code can't know it's a name; that's the --llm pass's job.
    assert "Edsger Dijkstra" in out


def test_llm_pass_rejects_a_rewrite_that_changes_more_than_names():
    text = "**@Grace** · Sat\n" + "word " * 200
    assert llm_pass(text, lambda sys, u: u.replace("word", "w")) is None
    assert llm_pass(text, lambda sys, u: u) == text.strip() + "\n"


class FakeNarrowZulip:
    def __init__(self, messages):
        self.messages = messages
        self.narrows = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        narrow = json.loads(q["narrow"])
        self.narrows.append(narrow)
        with_id = next((t["operand"] for t in narrow if t["operator"] == "with"), None)
        topic = next((m["subject"] for m in self.messages if m["id"] == with_id), None)
        pool = [m for m in self.messages if m["subject"] == topic]
        return httpx.Response(200, json={"result": "success", "messages": pool, "found_newest": True})


def test_grab_end_to_end_prints_no_message_text(tmp_path, monkeypatch, capsys, cfg):
    zulip = FakeNarrowZulip(THREAD)
    monkeypatch.setattr(cli, "zulip_client", lambda cfg: ZulipClient(
        cfg.zulip.site, "me@example.com", "abc", transport=httpx.MockTransport(zulip.handler)))
    monkeypatch.setattr(cli, "load", lambda path: cfg)
    copied = {}
    monkeypatch.setattr(cli, "_pb", lambda cmd, data=None: copied.setdefault(cmd, data) or b"")

    # The same conversation twice, via two message ids, is fetched twice but kept once.
    other = LINK.replace("/with/301", "/with/302")
    assert cli.main(["grab", f"look: {LINK} and {other}"]) == 0

    text = copied["pbcopy"].decode()
    assert "**@Barbara**" in text and "Liskov" not in text
    out = capsys.readouterr().out
    assert "1 conversation(s), 3 messages, 2 people" in out
    assert "1 duplicate link(s) folded" in out
    for word in ("linked list", "Grace", "Barbara", "Liskov", "pair"):
        assert word not in out
