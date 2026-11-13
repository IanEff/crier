import pytest

from crier.digest import CHECKINS, Digester, group_by_thread, pack
from crier.links import NO_SOURCE, defang, encode_hash, link_citations, message_url
from crier.llm import LLM, strip_reasoning
from crier.store import Store
from crier.zulip import Message
from tests.conftest import FIXTURE, NOW, FakeLLM

MSGS = [Message.from_api(m) for m in FIXTURE]
BY_ID = {m.id: m for m in MSGS}
SITE = "https://recurse.zulipchat.com"


def digester(cfg, fake_llm: FakeLLM, store: Store | None = None) -> Digester:
    store = store or Store(cfg.output.state_path / "crier.sqlite3")
    if not store.window(cfg.channels.all(), 0):
        store.add(MSGS)
    return Digester(cfg, store, LLM(cfg.llm, transport=fake_llm.transport), NOW)


# --- links ---

def test_encode_hash_matches_zulip():
    assert encode_hash("397 Bridge") == "397.20Bridge"
    assert encode_hash("v1.2 (beta)") == "v1.2E2.20.28beta.29"


def test_message_url():
    url = message_url(SITE, BY_ID[103])
    assert url == f"{SITE}/#narrow/channel/20-hub-happenings/topic/lunch/near/103"


def test_citations_link_and_flag_inventions():
    text = "- real [m103]\n- two [m103, m105]\n- invented [m999]\n- bare claim\nplain line [m104]"
    out = link_citations(text, BY_ID, SITE).splitlines()
    assert out[0] == f"- real [↗]({message_url(SITE, BY_ID[103])})"
    assert out[1].count("[↗]") == 2
    assert out[2] == "- invented" + NO_SOURCE
    assert out[3] == "- bare claim" + NO_SOURCE
    assert out[4].startswith("plain line [↗]") and NO_SOURCE not in out[4]


def test_plain_footnotes_untouched():
    out = link_citations("- see [1] and [link](https://x.example) [m101]", BY_ID, SITE)
    assert out.count("[1]") == 1
    assert out.startswith("- see [1] and link [↗]")


@pytest.mark.parametrize("injected", [
    "![](https://evil.example/?q=dumplings)",
    "![x](//evil.example/p.png)",
    '<img src="https://evil.example/p.png">',
    "<iframe src=https://evil.example></iframe>",
    "<https://evil.example/autolink>",
    "[click](https://evil.example/phish)",
    "see https://evil.example/bare",
    "obsidian://open?vault=x and file:///etc/passwd",
])
def test_model_links_and_images_are_stripped(injected):
    out = link_citations(f"- lunch at 12:30 {injected} [m103]", BY_ID, SITE)
    assert "evil.example" not in out and "file:" not in out and "<" not in out
    assert "obsidian://" not in out
    assert out.startswith("- lunch at 12:30")
    # Our own citation link survives.
    assert message_url(SITE, BY_ID[103]) in out


def test_reference_style_image_is_stripped():
    text = "- lunch ![x][e] [m103]\n[e]: https://evil.example/p.png\n[f]: //evil.example/q"
    out = link_citations(text, BY_ID, SITE)
    assert "evil.example" not in out
    # A line that only looks like a definition is kept.
    assert defang("[m103]: Note: lunch at noon") == "[m103]: Note: lunch at noon"


def test_defang_leaves_plain_text_alone():
    text = "- **Fri 4pm**: talk on a < b > c, `code`, [[wiki link]] [m104]"
    assert defang(text) == text


# --- chunking ---

def test_pack_keeps_threads_whole_when_it_can():
    groups = [["a" * 40, "b" * 40], ["c" * 40], ["d" * 40, "e" * 40]]
    chunks = pack(groups, budget=90)
    assert chunks == ["a" * 40 + "\n" + "b" * 40, "c" * 40, "d" * 40 + "\n" + "e" * 40]


def test_pack_splits_oversized_thread():
    chunks = pack([["x" * 50, "y" * 50, "z" * 50]], budget=60)
    assert chunks == ["x" * 50, "y" * 50, "z" * 50]


def test_group_by_thread_orders_by_first_message():
    groups = group_by_thread(MSGS)
    assert [g[0].id for g in groups][:2] == [101, 102]
    assert all(len({(m.stream, m.topic) for m in g}) == 1 for g in groups)


def test_strip_reasoning():
    assert strip_reasoning("<think>hmm</think>\n- a") == "- a"
    assert strip_reasoning("hmm</think>- a") == "- a"
    assert strip_reasoning("- a<think>cut off") == "- a"


# --- sections ---

def test_line_marks_interest_hits(cfg, fake_llm):
    d = digester(cfg, fake_llm)
    line = d.line(BY_ID[106])
    assert line.startswith("[m106] Sat 13:00 Grace Hopper in #rust-corner > borrowck ★pair:")
    assert "★" not in d.line(BY_ID[103])


def test_trim_prefers_matches(cfg, fake_llm):
    d = digester(cfg, fake_llm)
    object.__setattr__(cfg.digest, "max_messages_per_section", 2)
    kept = d.trim(MSGS, prefer_matches=True)
    # Hits are 101 (lsm), 102 (rust), 106 (pair); the two newest hits win over newer non-hits.
    assert {m.id for m in kept} == {102, 106}


def test_section_cites_only_real_messages(cfg, fake_llm):
    d = digester(cfg, fake_llm)
    msgs = [m for m in MSGS if m.stream == "checkins"]
    r = d.section(CHECKINS, msgs, "2026-11-07")
    assert r.summarized and r.error is None
    lines = r.body.splitlines()
    assert lines[0].startswith("- item from message 101 [↗](")
    assert lines[-1] == "- an invented item" + NO_SOURCE


def test_sections_are_cached(cfg, fake_llm):
    store = Store(cfg.output.state_path / "crier.sqlite3")
    digester(cfg, fake_llm, store).day_sections(NOW.date())
    first = len(fake_llm.prompts)
    assert first == 3  # for-you, check-ins, hub-today
    digester(cfg, fake_llm, store).day_sections(NOW.date())
    assert len(fake_llm.prompts) == first  # nothing new, no model calls

    store.add([Message.from_api({**FIXTURE[2], "id": 107, "content": "More dumplings."})])
    digester(cfg, fake_llm, store).day_sections(NOW.date())
    assert len(fake_llm.prompts) == first + 2  # for-you and hub-today changed; check-ins didn't


def test_model_failure_is_reported_not_cached(cfg, fake_llm):
    store = Store(cfg.output.state_path / "crier.sqlite3")
    fake_llm.fail_with = 503
    results = digester(cfg, fake_llm, store).now_sections()
    assert all(r.error == "HTTP 503: model overloaded" for r in results)
    fake_llm.fail_with = None
    results = digester(cfg, fake_llm, store).now_sections()
    assert all(r.error is None and r.summarized for r in results)


def test_now_window(cfg, fake_llm):
    d = digester(cfg, fake_llm)
    hub_now, coming = d.now_sections()
    # now is 14:30, window 3h: 105 (14:00) is in, 103 (12:00) too; nothing older in hub.
    prompt = fake_llm.prompts[0]["messages"][-1]["content"]
    assert "[m103]" in prompt and "[m105]" in prompt
    assert hub_now.messages == 2 and coming.messages == 1


def test_system_prompt_has_profile_and_no_vault_paths(cfg, fake_llm):
    d = digester(cfg, fake_llm)
    d.now_sections()
    system = fake_llm.prompts[0]["messages"][0]["content"]
    assert "Likes storage and Rust." in system
    assert "Interest keywords: rust, lsm, pair" in system
    assert "Saturday 07 November 2026, 14:30" in system


def test_long_messages_are_capped(cfg, fake_llm):
    d = digester(cfg, fake_llm)
    big = Message.from_api({**FIXTURE[0], "content": "word " * 2000})
    assert len(d.line(big)) < cfg.digest.max_message_chars + 200


def test_request_sends_reasoning_effort_and_flags_cutoff(cfg, fake_llm):
    import httpx

    from crier.llm import CUT_OFF

    seen = []

    def handler(request):
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "- half a list [m101]"}, "finish_reason": "length"}]})

    llm = LLM(cfg.llm, transport=httpx.MockTransport(handler))
    out = llm.complete("sys", "user")
    assert seen[0]["reasoning_effort"] == "low" and seen[0]["max_tokens"] == 8192
    assert out.endswith(CUT_OFF)
    # The cut-off note is not a bullet, so it doesn't get flagged as unsourced.
    assert link_citations(out, BY_ID, SITE).splitlines()[-1] == CUT_OFF
