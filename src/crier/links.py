"""Turning the model's [m123] citations into links back to Zulip.

The model never writes URLs. It cites message ids, and this code builds the
links from the messages it was actually shown. A cited id that wasn't in the
input is dropped, and a bullet left with no real source gets flagged, so a
made-up item can't pass as a sourced one.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from crier.zulip import Message

CITATION = re.compile(r"\[\s*(m\d+(?:\s*[,;]\s*m?\d+)*)\s*\]")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
NO_SOURCE = " _(no source)_"


def encode_hash(s: str) -> str:
    """Zulip's own encoding for narrow URLs: percent-encode, then % becomes '.'."""
    return quote(s, safe="").replace(".", "%2E").replace("%", ".")


def message_url(site: str, m: Message) -> str:
    return (
        f"{site}/#narrow/channel/{m.stream_id}-{encode_hash(m.stream)}"
        f"/topic/{encode_hash(m.topic)}/near/{m.id}"
    )


def link_citations(text: str, by_id: dict[int, Message], site: str) -> str:
    lines = []
    for line in text.splitlines():
        sourced = False

        def replace(match: re.Match) -> str:
            nonlocal sourced
            ids = [int(part.strip().lstrip("m")) for part in re.split(r"[,;]", match.group(1))]
            links = [f"[↗]({message_url(site, by_id[i])})" for i in ids if i in by_id]
            sourced = sourced or bool(links)
            return " ".join(links)

        out = re.sub(r"[ \t]+$", "", CITATION.sub(replace, line))
        if BULLET.match(line) and not sourced:
            out += NO_SOURCE
        lines.append(out)
    return "\n".join(lines)
