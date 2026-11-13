"""Zulip links in, the conversations behind them out, with people as @Firstname.

`crier grab` finds every recurse.zulipchat.com narrow link in some pasted
text, fetches each conversation whole, and renders it as Markdown in which
every person (sender, @-mention, quote attribution, or full name in prose)
is written "@Firstname". Two people who share a first name get their last
initial too: @AliceS, @AliceT.

Names are replaced in code, from what Zulip says about who is in the
conversation. The optional model pass (RC's gateway, never a hosted one)
only catches names Zulip doesn't know are names, like a surname on its own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from crier.guard import ZULIP_HOSTS

LINK_RE = re.compile(
    r"https://(?:" + "|".join(re.escape(h) for h in sorted(ZULIP_HOSTS)) + r")/#narrow/[^\s<>\"'`)\]]+"
)
# @**Full Name**, @**Full Name|123**, and the silent @_**Full Name|123** quotes use.
MENTION_RE = re.compile(r"@_?\*\*([^*|]+?)(?:\|\d+)?\*\*")
# A quote's attribution: "@_**Name|123** [said](https://...#narrow/...):"
SAID_RE = re.compile(r"\[said\]\([^)\s]*\)")
WILDCARDS = {"all", "everyone", "channel", "stream", "topic"}
OPERATOR_ALIASES = {"stream": "channel", "subject": "topic", "pm-with": "dm"}
CONVERSATION_OPERATORS = {"topic", "with", "dm", "id"}
GROUP_GAP_SECONDS = 10 * 60  # a sender's messages this close together share one header


class LinkError(Exception):
    """A link crier can't turn into a single conversation."""


def find_links(text: str) -> list[str]:
    """Every Zulip narrow link in `text`, in order, without repeats."""
    seen: dict[str, None] = {}
    for m in LINK_RE.finditer(text):
        seen.setdefault(m.group(0).rstrip(".,;:"), None)
    return list(seen)


def _decode(part: str) -> str:
    """Undo Zulip's hash encoding, which is percent-encoding with '.' for '%'."""
    return unquote(re.sub(r"\.([0-9A-Fa-f]{2})", r"%\1", part))


def _leading_int(s: str) -> int | None:
    m = re.match(r"(\d+)(?:-|$)", s)
    return int(m.group(1)) if m else None


def parse_narrow(url: str) -> list[dict]:
    """The API narrow for a web-app link like #narrow/channel/1-x/topic/y/with/2.

    `near` is dropped: crier fetches the whole conversation, not the bit
    around one message. A link to a whole channel is refused.
    """
    _, sep, path = url.partition("#narrow/")
    if not sep:
        raise LinkError("not a #narrow link")
    parts = [p for p in path.split("/") if p]
    if len(parts) % 2:
        parts = parts[:-1]  # a trailing operator with no operand
    narrow: list[dict] = []
    for op, raw in zip(parts[::2], parts[1::2]):
        op = OPERATOR_ALIASES.get(op, op)
        if op == "near":
            continue
        if op == "channel":
            n = _leading_int(raw)
            operand: object = n if n is not None else _decode(raw)
        elif op in ("with", "id"):
            if not raw.isdigit():
                raise LinkError(f"{op} wants a message id")
            operand = int(raw)
        elif op == "dm":
            ids = raw.split("-", 1)[0]
            try:
                operand = [int(x) for x in ids.split(",")]
            except ValueError:
                raise LinkError("can't read the people in a DM link") from None
        else:
            operand = _decode(raw)
        narrow.append({"operator": op, "operand": operand})
    if not CONVERSATION_OPERATORS & {t["operator"] for t in narrow}:
        raise LinkError("points at a whole channel or view, not one conversation")
    return narrow


def _canonical(name: str) -> str:
    """'Ada Lovelace (she/her) (W1'24)' -> 'Ada Lovelace'."""
    return " ".join(re.sub(r"\([^)]*\)", " ", name).split()) or name.strip()


def _words(name: str) -> list[str]:
    return _canonical(name).split()


@dataclass
class People:
    """Everyone a conversation names, and the @handle each one gets."""

    names: set[str] = field(default_factory=set)

    def add(self, name: str) -> None:
        if name and name.strip().lower() not in WILDCARDS:
            self.names.add(name.strip())

    def handles(self) -> dict[str, str]:
        """full name (raw and canonical) -> '@First', with last initials on clashes."""
        canons = {_canonical(n) for n in self.names}

        def first(c: str) -> str:
            return _alnum(_words(c)[0])

        def last(c: str) -> str:
            w = _words(c)
            return _alnum(w[-1]) if len(w) > 1 else ""

        out: dict[str, str] = {}
        for c in canons:
            clash = [d for d in canons if first(d) == first(c)]
            handle = first(c)
            if len(clash) > 1:
                initial = last(c)[:1].upper()
                same_initial = [d for d in clash if last(d)[:1].upper() == initial]
                handle += initial if len(same_initial) == 1 else last(c)
            out[c] = "@" + handle
        for raw in self.names:
            out[raw] = out[_canonical(raw)]
        return out


def _alnum(s: str) -> str:
    return re.sub(r"[^\w'-]", "", s) or s


class Scrubber:
    def __init__(self, handles: dict[str, str]):
        self.handles = handles
        # Longest first, so "Ada Lovelace (she/her)" wins over "Ada Lovelace".
        names = sorted(handles, key=len, reverse=True)
        self._prose = (
            re.compile(r"(?<![\w@])(?:" + "|".join(re.escape(n) for n in names) + r")(?!\w)")
            if names else None
        )

    def handle(self, name: str) -> str:
        name = name.strip()
        if name.lower() in WILDCARDS:
            return "@" + name.lower()
        return self.handles.get(name) or self.handles.get(_canonical(name)) or "@" + (
            _alnum(_words(name)[0]) if _words(name) else name
        )

    def text(self, s: str) -> str:
        s = MENTION_RE.sub(lambda m: self.handle(m.group(1)), s)
        s = SAID_RE.sub("said", s)
        if self._prose:
            s = self._prose.sub(lambda m: self.handles[m.group(0)], s)
        return s


def people_in(messages: Iterable[dict]) -> People:
    p = People()
    for m in messages:
        p.add(m.get("sender_full_name", ""))
        if isinstance(m.get("display_recipient"), list):
            for r in m["display_recipient"]:
                p.add(r.get("full_name", ""))
        for name in MENTION_RE.findall(m.get("content", "")):
            p.add(name)
    return p


def render(messages: list[dict], scrub: Scrubber, tz: ZoneInfo, truncated: bool = False) -> str:
    """One conversation as Markdown: a heading, then each sender's runs of messages."""
    first = messages[0]
    if first.get("type") == "private":
        who = ", ".join(sorted(scrub.handle(r["full_name"]) for r in first["display_recipient"]))
        lines = [f"## DM: {who}"]
    else:
        lines = [f"## #{first['display_recipient']} > {scrub.text(first['subject'])}"]
        topics = {m["subject"] for m in messages}
        if len(topics) > 1:  # a `with` link can span a topic that was renamed mid-way
            lines.append("_(spans topics: " + "; ".join(scrub.text(t) for t in sorted(topics)) + ")_")
    if truncated:
        lines.append(f"_(first {len(messages)} messages only)_")
    prev_sender, prev_ts = None, 0
    for m in messages:
        ts = int(m["timestamp"])
        if m["sender_full_name"] != prev_sender or ts - prev_ts > GROUP_GAP_SECONDS:
            when = datetime.fromtimestamp(ts, tz).strftime("%a %Y-%m-%d %H:%M")
            lines += ["", f"**{scrub.handle(m['sender_full_name'])}** · {when}"]
        lines.append(scrub.text(m["content"]).strip())
        prev_sender, prev_ts = m["sender_full_name"], ts
    return "\n".join(lines) + "\n"


LLM_SYSTEM = """You redact personal names in a chat transcript.

Every person is already written as @Firstname wherever the chat software
knew it was a name. Find any remaining reference to a person by full name
or surname and rewrite it as @Firstname, using the handles already in the
text when they match (for example "Lovelace" becomes "@Ada" if "@Ada" is
in the text). Leave first names alone, and leave names of software,
companies, places and authors of books or papers alone.

Change nothing else: not wording, spacing, Markdown, code, links or
order. Reply with the full transcript and nothing before or after it."""


def llm_pass(text: str, complete) -> str | None:
    """The model's rewrite of `text`, or None if it looks like more than a redaction."""
    out = complete(LLM_SYSTEM, text).strip()
    if not out or abs(len(out) - len(text.strip())) > max(200, len(text) // 10):
        return None
    return out + "\n"
