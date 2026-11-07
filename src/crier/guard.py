"""Where Zulip text is allowed to go.

The RC manual forbids sending Zulip or Directory data to hosted AI. Every
outbound request crier makes is checked here first, so a typo in the config
can't point it at a hosted API. Widening these sets is a code change, and
that's deliberate: it should take a commit, not a config edit.
"""

from urllib.parse import urlsplit

ZULIP_HOSTS = frozenset({"recurse.zulipchat.com"})
# llm.recurse.com is the web UI (Heroku); its API answers 421 and points at
# llm.rcdis.co, the gateway droplet RC deploys with Disco. Per the
# llm.recurse.com FAQ, the gateway checks the key and relays over Tailscale to
# the two DGX Sparks at the hub, logs usage but not content, and never forwards
# to a model provider. Checked 2026-11-07.
LLM_HOSTS = frozenset({"llm.recurse.com", "llm.rcdis.co"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class GuardError(Exception):
    """A URL crier refuses to talk to."""


def _scheme_host(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    # .hostname drops userinfo, so "https://llm.recurse.com@evil.example" is evil.example.
    return parts.scheme.lower(), (parts.hostname or "").lower()


def check_zulip_url(url: str) -> None:
    scheme, host = _scheme_host(url)
    if scheme != "https" or host not in ZULIP_HOSTS:
        raise GuardError(f"refusing Zulip site {url!r}: only https://recurse.zulipchat.com")


def check_llm_url(url: str) -> None:
    scheme, host = _scheme_host(url)
    if host in LOCAL_HOSTS and scheme in ("http", "https"):
        return
    if host in LLM_HOSTS and scheme == "https":
        return
    allowed = ", ".join(sorted(LLM_HOSTS | LOCAL_HOSTS))
    raise GuardError(f"refusing LLM endpoint {url!r}: Zulip text may only go to {allowed}")
