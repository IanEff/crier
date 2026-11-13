"""Headlines to the phone, through an ntfy server on this Mac.

crier publishes only to localhost (guard.check_push_url). The phone's ntfy
app reaches that server over Tailscale. With `upstream-base-url` set, the
server tells ntfy.sh only a message id and a hash of the topic, and the phone
fetches the headline itself from the Mac (docs.ntfy.sh/config, "iOS instant
notifications"). The text never sits on a third-party server.

A headline is one bullet from a pushable section that cites at least one
message not pushed before. Rewording on a later run doesn't re-push it,
because "new" is decided by the cited message ids, not the wording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from crier import USER_AGENT
from crier.config import Config, PushConfig
from crier.digest import SectionResult, is_nothing
from crier.guard import check_push_url
from crier.links import BULLET, CITATION, cited_ids, defang, message_url
from crier.store import Store

PRIMED = "push_primed"
TITLES = {"hub-now": "At the hub now", "coming-up": "Coming up", "for-you": "For you"}
TAGS = {"hub-now": ["house"], "coming-up": ["calendar"], "for-you": ["star"]}
PRIORITY = {"hub-now": 4, "coming-up": 3, "for-you": 3}  # ntfy: 3 default, 4 high


class PushError(Exception):
    """The ntfy server failed. The message never carries headline text."""


@dataclass(frozen=True)
class Headline:
    section: str
    text: str
    ids: tuple[int, ...]
    url: str  # Zulip link to the first cited message


def clean(line: str) -> str:
    """A bullet as plain text: no marker, citations or markdown emphasis."""
    text = BULLET.sub("", line, count=1)
    text = defang(CITATION.sub("", text))
    text = re.sub(r"\*\*|__|`", "", text)
    return " ".join(text.split())


def headlines(results: list[SectionResult], push: PushConfig, site: str) -> list[Headline]:
    """Sourced bullets from the pushable sections, in the configured section order."""
    by_key = {r.spec.key: r for r in results}
    out = []
    for key in push.sections:
        r = by_key.get(key)
        if r is None or r.error or is_nothing(r.raw):
            continue
        for line in r.raw.splitlines():
            if not BULLET.match(line):
                continue
            ids = tuple(i for i in cited_ids(line) if i in r.by_id)
            text = clean(line)
            if ids and text:
                out.append(Headline(key, text, ids, message_url(site, r.by_id[ids[0]])))
    return out


def fresh(items: list[Headline], store: Store) -> list[Headline]:
    """Headlines citing at least one unpushed message, each message claimed once."""
    seen = store.pushed(i for h in items for i in h.ids)
    out = []
    for h in items:
        if any(i not in seen for i in h.ids):
            out.append(h)
            seen.update(h.ids)
    return out


class Pusher:
    def __init__(self, cfg: Config, store: Store, *, transport: httpx.BaseTransport | None = None):
        assert cfg.push is not None
        check_push_url(cfg.push.server)
        self.cfg = cfg
        self.push = cfg.push
        self.store = store
        self._http = httpx.Client(
            base_url=self.push.server,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
            transport=transport,
            follow_redirects=False,
            trust_env=False,  # an HTTP_PROXY would get the headlines in plaintext
        )

    def close(self) -> None:
        self._http.close()

    def digest_url(self) -> str | None:
        if not self.push.obsidian_vault:
            return None
        page = f"{self.cfg.output.digest_dir}/zulip-now"
        return f"obsidian://open?vault={quote(self.push.obsidian_vault)}&file={quote(page)}"

    def _send(self, title: str, message: str, *, click: str | None, tags: list[str],
              priority: int) -> None:
        body: dict = {"topic": self.push.topic, "title": title, "message": message,
                      "tags": tags, "priority": priority}
        if click:
            body["click"] = click
        digest = self.digest_url()
        if digest:
            body["actions"] = [{"action": "view", "label": "Digest", "url": digest}]
        try:
            r = self._http.post("/", json=body)
        except httpx.HTTPError as e:
            raise PushError(type(e).__name__) from None
        if r.status_code != 200:
            raise PushError(f"HTTP {r.status_code}")

    def test(self) -> None:
        self._send("crier", "Push test: if you can read this on your phone, pushes work.",
                   click=None, tags=["white_check_mark"], priority=3)

    def run(self, results: list[SectionResult]) -> tuple[int, int]:
        """Push fresh headlines. Returns (notifications sent, headlines covered).

        The first run marks everything current as already seen and sends
        nothing, so turning pushes on doesn't dump a day's backlog.
        """
        items = fresh(headlines(results, self.push, self.cfg.zulip.site), self.store)
        if self.store.get_meta(PRIMED) is None:
            self.store.mark_pushed(i for h in items for i in h.ids)
            self.store.set_meta(PRIMED, "1")
            return 0, 0
        if not items:
            return 0, 0
        cap = self.push.max_per_run
        single = items if len(items) <= cap else items[: cap - 1]
        rest = items[len(single):]
        sent = 0
        for h in single:
            self._send(TITLES[h.section], h.text, click=h.url, tags=TAGS[h.section],
                       priority=PRIORITY[h.section])
            self.store.mark_pushed(h.ids)
            sent += 1
        if rest:
            self._send("crier", f"…and {len(rest)} more in the digest.", click=self.digest_url(),
                       tags=["newspaper"], priority=2)
            self.store.mark_pushed(i for h in rest for i in h.ids)
            sent += 1
        return sent, len(items)
