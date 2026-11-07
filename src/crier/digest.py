"""Turning cached messages into digest sections.

Each section is one job for the model: a slice of messages, an instruction,
and a short markdown list back. Sections are cached by a hash of their input,
so a run where nothing new arrived costs no model calls. That matters,
because RC's two DGX Sparks are shared by the whole batch.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime

from crier.config import Config
from crier.links import link_citations
from crier.llm import LLM, LLMError
from crier.store import Store
from crier.zulip import Message

# Bump to invalidate every cached section after a prompt change in code.
PROMPT_VERSION = "1"

SYSTEM = """\
You write a Zulip digest for one person at the Recurse Center (RC), a \
self-directed programming retreat in Brooklyn. They're drowning in messages \
and don't want to miss the good stuff.

Rules:
- Use only the messages you're given. Never invent people, events, times, places or details.
- End every bullet with the id(s) of the message(s) it came from, like [m123] or [m123, m456].
- Reply with markdown bullets ("- ") only: no preamble, no headings, no closing remarks.
- Times are local ({tz}). It is now {now}.
- If nothing qualifies, reply with exactly: NOTHING
- Lines marked ★ matched one of the reader's interest keywords (listed after the ★).

About the reader:
{profile}
"""

MERGE = """\
Below are partial answers to the same task, each made from a different batch \
of messages. Combine them into one list: drop duplicates, keep the [m...] \
ids, and follow the task's limits and ordering.

The task was:
{task}

Partial answers:
"""


@dataclass(frozen=True)
class Spec:
    key: str
    title: str
    task: str
    merge: bool = True  # combine per-chunk answers with one more model call
    bucket_seconds: int = 0  # redo at least this often, for answers that depend on the clock


HUB_NOW = Spec(
    "hub-now",
    "Right now at the hub",
    """\
These are recent messages from the channels where people say what's happening \
at the RC hub. Tell the reader what's going on right now or in the next couple \
of hours that they could walk over and join: talks, food, games, outings, \
people looking for company or a pair. Leave out anything that's clearly over. \
Most current first, at most 10 bullets, each under 25 words, with when and \
where if the messages say.""",
    bucket_seconds=30 * 60,
)

COMING_UP = Spec(
    "coming-up",
    "Coming up",
    """\
These are event announcements. List the events that haven't finished yet, \
soonest first, as "- **<day> <time>**: <what> (<where>)". Leave out events \
that are over. If the time is unclear, say so rather than guessing. At most \
15 bullets.""",
    bucket_seconds=60 * 60,
)

FOR_YOU = Spec(
    "for-you",
    "For you",
    """\
Pick out what this reader would hate to miss: things that fit their \
interests, and anything fun or social they could join. A ★ is a hint, not a \
reason on its own. At most 10 bullets, best first, each ending with a few \
words on why it's for them.""",
)

CHECKINS = Spec(
    "checkins",
    "Check-ins",
    """\
These are daily check-ins; each person posts in a topic named after \
themselves. Write one bullet per person: "- **<name>**: <what they're \
working on, stuck on or looking for, under 25 words>". When it overlaps the \
reader's interests, add " (overlap: <why, a few words>)" so they know who \
to pair with. Put the overlaps first.""",
    merge=False,  # bullets are per person; batches don't overlap
)

HUB_TODAY = Spec(
    "hub-today",
    "Hub today",
    """\
Summarize what happened in these channels today: one bullet per thread worth \
knowing about, most interesting first, at most 12 bullets.""",
)


@dataclass
class SectionResult:
    spec: Spec
    body: str
    messages: int
    summarized: bool = False  # True if the model ran this time (not cached, not empty)
    error: str | None = None
    raw: str = ""  # the model's answer before linking, [m123] citations intact
    by_id: dict[int, Message] = field(default_factory=dict)  # the messages it was shown


def is_nothing(text: str) -> bool:
    return not text.strip() or text.strip().strip(".").upper() == "NOTHING"


def group_by_thread(msgs: list[Message]) -> list[list[Message]]:
    """Messages grouped by (channel, topic), threads in order of their first message."""
    groups: dict[tuple[str, str], list[Message]] = {}
    for m in sorted(msgs, key=lambda m: (m.timestamp, m.id)):
        groups.setdefault((m.stream.lower(), m.topic.lower()), []).append(m)
    return list(groups.values())


def pack(groups: list[list[str]], budget: int) -> list[str]:
    """Pack whole groups of lines into chunks of at most `budget` chars.

    A group bigger than the budget is split by line. A single line is never
    split; lines are already capped at max_message_chars.
    """
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for group in groups:
        gsize = sum(len(line) + 1 for line in group)
        if cur and size + gsize > budget:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        for line in group:
            if cur and size + len(line) + 1 > budget:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            cur.append(line)
            size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


class Digester:
    def __init__(self, cfg: Config, store: Store, llm: LLM, now: datetime):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.tz = cfg.digest.tz
        self.now = now.astimezone(self.tz)
        self._interests = [
            (term, re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE))
            for term in cfg.me.interests
        ]

    # --- formatting messages for the model ---

    def matches(self, m: Message) -> list[str]:
        text = f"{m.topic} {m.content}"
        return [term for term, rx in self._interests if rx.search(text)]

    def line(self, m: Message) -> str:
        when = datetime.fromtimestamp(m.timestamp, self.tz).strftime("%a %H:%M")
        text = " ".join(m.content.split())
        cap = self.cfg.digest.max_message_chars
        if len(text) > cap:
            text = text[:cap] + " …"
        hits = self.matches(m)
        star = f" ★{', '.join(hits)}" if hits else ""
        return f"[m{m.id}] {when} {m.sender} in #{m.stream} > {m.topic}{star}: {text}"

    def system(self) -> str:
        profile = self.cfg.me.profile or "(no profile given)"
        if self.cfg.me.interests:
            profile += "\nInterest keywords: " + ", ".join(self.cfg.me.interests)
        return SYSTEM.format(
            tz=self.cfg.digest.timezone,
            now=self.now.strftime("%A %d %B %Y, %H:%M"),
            profile=profile,
        )

    def trim(self, msgs: list[Message], prefer_matches: bool = False) -> list[Message]:
        """Cap a section's input, keeping the newest (and, if asked, keyword hits first)."""
        limit = self.cfg.digest.max_messages_per_section
        if len(msgs) <= limit:
            return msgs
        newest = sorted(msgs, key=lambda m: (m.timestamp, m.id), reverse=True)
        if prefer_matches:
            newest.sort(key=lambda m: not self.matches(m))  # stable: hits first, each newest-first
        return sorted(newest[:limit], key=lambda m: (m.timestamp, m.id))

    # --- running one section ---

    def _input_hash(self, spec: Spec, msgs: list[Message]) -> str:
        h = hashlib.sha256()
        for part in (PROMPT_VERSION, SYSTEM, self.cfg.me.profile, *self.cfg.me.interests,
                     self.cfg.llm.model, spec.task):
            h.update(part.encode())
            h.update(b"\0")
        for m in msgs:
            h.update(f"{m.id}:{m.topic}:{m.content}\0".encode())
        if spec.bucket_seconds:
            h.update(str(int(self.now.timestamp()) // spec.bucket_seconds).encode())
        return h.hexdigest()

    def _summarize(self, spec: Spec, msgs: list[Message]) -> str:
        system = self.system()
        header = f"{spec.task}\n\nMessages:\n"
        budget = max(2_000, self.cfg.llm.max_input_chars - len(system) - len(header))
        groups = [[self.line(m) for m in thread] for thread in group_by_thread(msgs)]
        parts = []
        for chunk in pack(groups, budget):
            out = self.llm.complete(system, header + chunk)
            if not is_nothing(out):
                parts.append(out)
        if not parts:
            return "NOTHING"
        if len(parts) == 1 or not spec.merge:
            return "\n".join(parts)
        return self.llm.complete(system, MERGE.format(task=spec.task) + "\n\n---\n\n".join(parts))

    def section(self, spec: Spec, msgs: list[Message], scope: str) -> SectionResult:
        if not msgs:
            return SectionResult(spec, "_Nothing yet._", 0)
        key = f"{scope}:{spec.key}"
        input_hash = self._input_hash(spec, msgs)
        raw = self.store.cached(key, input_hash)
        summarized = False
        if raw is None:
            try:
                raw = self._summarize(spec, msgs)
            except LLMError as e:
                return SectionResult(
                    spec, f"_Couldn't summarize this section this run: {e}_", len(msgs), error=str(e)
                )
            self.store.remember(key, input_hash, raw)
            summarized = True
        if is_nothing(raw):
            body = "_Nothing worth flagging._"
        else:
            body = link_citations(raw, {m.id: m for m in msgs}, self.cfg.zulip.site)
        return SectionResult(
            spec, body, len(msgs), summarized=summarized, raw=raw, by_id={m.id: m for m in msgs}
        )

    # --- the two pages ---

    def now_sections(self) -> list[SectionResult]:
        ch, d = self.cfg.channels, self.cfg.digest
        now_ts = int(self.now.timestamp())
        out = []
        if ch.hub:
            msgs = self.store.window(ch.hub, now_ts - int(d.now_window_hours * 3600))
            out.append(self.section(HUB_NOW, self.trim(msgs), "now"))
        if ch.events:
            msgs = self.store.window(ch.events, now_ts - int(d.events_lookback_hours * 3600))
            out.append(self.section(COMING_UP, self.trim(msgs), "now"))
        return out

    def day_bounds(self, day: date) -> tuple[int, int]:
        start = datetime.combine(day, dtime.min, self.tz)
        end = datetime.combine(day + timedelta(days=1), dtime.min, self.tz)
        return int(start.timestamp()), int(end.timestamp())

    def day_sections(self, day: date) -> list[SectionResult]:
        ch = self.cfg.channels
        since, until = self.day_bounds(day)
        scope = day.isoformat()
        out = []
        rest = ch.not_checkins()
        if rest:
            msgs = self.store.window(rest, since, until)
            out.append(self.section(FOR_YOU, self.trim(msgs, prefer_matches=True), scope))
        if ch.checkins:
            msgs = self.store.window(ch.checkins, since, until)
            out.append(self.section(CHECKINS, self.trim(msgs), scope))
        if ch.hub:
            msgs = self.store.window(ch.hub, since, until)
            out.append(self.section(HUB_TODAY, self.trim(msgs), scope))
        return out
