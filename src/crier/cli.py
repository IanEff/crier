"""crier's command line.

Output rule: crier prints counts, paths, channel names from your own config,
and error codes. It never prints message text. That way you can run it
anywhere, including a terminal a hosted assistant is reading, without
leaking Recursers' words.
"""

from __future__ import annotations

import argparse
import fcntl
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from crier import __version__
from crier.config import Config, ConfigError, load
from crier.digest import Digester, SectionResult
from crier.guard import GuardError
from crier.llm import LLM, LLMError
from crier.push import Pusher, PushError
from crier.render import NOW_PAGE, day_page, day_page_name, now_page, write_page
from crier.store import Store
from crier.zulip import ZulipClient, ZulipError, read_zuliprc


def say(msg: str) -> None:
    print(msg, flush=True)


@contextmanager
def run_lock(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def open_store(cfg: Config) -> Store:
    return Store(cfg.output.state_path / "crier.sqlite3")


def zulip_client(cfg: Config) -> ZulipClient:
    email, key = read_zuliprc(cfg.zulip.zuliprc)
    return ZulipClient(cfg.zulip.site, email, key, page_size=cfg.zulip.page_size)


def first_lookback_hours(cfg: Config, channel: str) -> float:
    """How far a channel's first fetch reaches back.

    Event channels reach back as far as "Coming up" looks, so announcements
    made a few days ago for this week aren't missed.
    """
    hours = cfg.zulip.lookback_hours
    if channel.lower() in {c.lower() for c in cfg.channels.events}:
        hours = max(hours, cfg.digest.events_lookback_hours)
    return hours


def fetch(
    cfg: Config, store: Store, client: ZulipClient, now: datetime, backfill_hours: float | None = None
) -> int:
    """Pull new messages for every watched channel. Returns how many channels failed.

    With backfill_hours, every channel is re-read from that far back regardless
    of its cursor; messages already cached are replaced, not duplicated.
    """
    total, failed = 0, 0
    for ch in cfg.channels.all():
        try:
            cur = store.cursor(ch)
            if backfill_hours is not None or cur is None:
                hours = backfill_hours if backfill_hours is not None else first_lookback_hours(cfg, ch)
                msgs = client.messages_since(ch, int(now.timestamp() - hours * 3600))
            else:
                msgs = client.messages_after(ch, cur)
        except ZulipError as e:
            say(f"  #{ch}: {e}")
            failed += 1
            continue
        total += store.add(msgs)
        if msgs:
            store.set_cursor(ch, max(m.id for m in msgs))
    pruned = store.prune(int(now.timestamp()) - cfg.digest.retention_days * 86_400)
    verb = f"backfilled {backfill_hours:g}h:" if backfill_hours is not None else "fetched"
    say(f"{verb} {total} new messages from {len(cfg.channels.all())} channels"
        + (f", pruned {pruned} old ones" if pruned else ""))
    return failed


def report(path: Path, results: list[SectionResult]) -> int:
    ran = sum(r.summarized for r in results)
    errors = [r for r in results if r.error]
    empty = sum(1 for r in results if not r.messages)
    cached = len(results) - ran - len(errors) - empty
    say(f"wrote {path} ({ran} summarized, {cached} cached, {empty} empty)")
    for r in errors:
        say(f"  {r.spec.key}: {r.error}")
    return len(errors)


def write_digests(
    cfg: Config, store: Store, llm: LLM, now: datetime, days: list[date]
) -> tuple[int, list[SectionResult]]:
    """Rewrite the pages. Returns (failed sections, today's results for pushing)."""
    d = Digester(cfg, store, llm, now)
    out = cfg.output.digest_path
    errors = 0
    results = d.now_sections()
    current = list(results)
    path = out / NOW_PAGE
    write_page(path, now_page(results, d.now), d.now.date())
    errors += report(path, results)
    for day in days:
        results = d.day_sections(day)
        if day == d.now.date():
            current += results
        path = out / day_page_name(day)
        if day != d.now.date() and not path.exists() and not any(r.messages for r in results):
            continue  # don't litter the vault with empty pages for days before the first run
        write_page(path, day_page(results, day, d.now), d.now.date())
        errors += report(path, results)
    return errors, current


def push_headlines(cfg: Config, store: Store, results: list[SectionResult]) -> int:
    """Push fresh headlines if [push] is configured. Returns 1 on failure."""
    if cfg.push is None:
        return 0
    pusher = Pusher(cfg, store)
    try:
        sent, covered = pusher.run(results)
    except PushError as e:
        say(f"push: {e}")
        return 1
    finally:
        pusher.close()
    say(f"push: {sent} notifications ({covered} headlines)")
    return 0


def make_llm(cfg: Config) -> LLM:
    return LLM(cfg.llm, api_key=cfg.llm.api_key())


def in_active_hours(cfg: Config, now: datetime) -> bool:
    hours = cfg.digest.active_hours
    if hours is None:
        return True
    start, end = hours
    h = now.hour
    return start <= h < end if start <= end else (h >= start or h < end)


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    now = datetime.now(cfg.digest.tz)
    if not args.force and not in_active_hours(cfg, now):
        say(f"outside active hours {cfg.digest.active_hours}; skipping (use --force)")
        return 0
    with run_lock(cfg.output.state_path) as got:
        if not got:
            say("another crier run is in progress; skipping")
            return 0
        store = open_store(cfg)
        with zulip_client(cfg) as client:
            failed = fetch(cfg, store, client, now, args.backfill)
        llm = make_llm(cfg)
        # Yesterday too, so messages between the last run and midnight make it in.
        # Its sections come from cache unless something changed.
        errors, current = write_digests(
            cfg, store, llm, now, [now.date(), now.date() - timedelta(days=1)]
        )
        say(f"{llm.calls} model calls")
        errors += push_headlines(cfg, store, current)
        return 1 if failed or errors else 0


def cmd_fetch(cfg: Config, args: argparse.Namespace) -> int:
    with run_lock(cfg.output.state_path) as got:
        if not got:
            say("another crier run is in progress; skipping")
            return 0
        store = open_store(cfg)
        with zulip_client(cfg) as client:
            return 1 if fetch(cfg, store, client, datetime.now(cfg.digest.tz), args.backfill) else 0


def cmd_digest(cfg: Config, args: argparse.Namespace) -> int:
    now = datetime.now(cfg.digest.tz)
    day = date.fromisoformat(args.day) if args.day else now.date()
    with run_lock(cfg.output.state_path) as got:
        if not got:
            say("another crier run is in progress; skipping")
            return 0
        llm = make_llm(cfg)
        errors, _ = write_digests(cfg, open_store(cfg), llm, now, [day])
        say(f"{llm.calls} model calls")
        return 1 if errors else 0


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    ok = True
    vault = cfg.output.vault
    if vault.is_dir():
        say(f"vault: ok ({vault})")
    else:
        say(f"vault: missing ({vault})")
        ok = False
    try:
        with zulip_client(cfg) as client:
            client.me()
            say("zulip: ok (signed in)")
            for ch in cfg.channels.all():
                try:
                    client.check_channel(ch)
                    say(f"  #{ch}: ok")
                except ZulipError as e:
                    say(f"  #{ch}: {e}")
                    ok = False
    except ZulipError as e:
        say(f"zulip: {e}")
        ok = False
    try:
        llm = make_llm(cfg)
        t = time.monotonic()
        llm.complete("Reply with the single word OK.", "Are you there?")
        say(f"llm: ok ({cfg.llm.model} at {cfg.llm.base_url}, {time.monotonic() - t:.1f}s)")
    except (LLMError, ConfigError) as e:
        say(f"llm: {e}")
        ok = False
    return 0 if ok else 1


def cmd_push_test(cfg: Config, args: argparse.Namespace) -> int:
    if cfg.push is None:
        say("push: no [push] section in the config")
        return 2
    pusher = Pusher(cfg, open_store(cfg))
    try:
        pusher.test()
    except PushError as e:
        say(f"push: {e} from {cfg.push.server}")
        return 1
    finally:
        pusher.close()
    say(f"push: test sent to topic {cfg.push.topic!r} via {cfg.push.server}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="crier", description="Zulip digest for the Recurse Center.")
    p.add_argument("--config", type=Path, help="config file (default ~/.config/crier/config.toml)")
    p.add_argument("--version", action="version", version=f"crier {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch new messages, then rewrite the digest pages")
    r.add_argument("--force", action="store_true", help="run even outside active_hours")
    r.add_argument("--backfill", type=float, metavar="HOURS",
                   help="re-read every channel from HOURS ago, ignoring cursors")
    r.set_defaults(func=cmd_run)
    f = sub.add_parser("fetch", help="fetch new messages only")
    f.add_argument("--backfill", type=float, metavar="HOURS",
                   help="re-read every channel from HOURS ago, ignoring cursors")
    f.set_defaults(func=cmd_fetch)
    dg = sub.add_parser("digest", help="rewrite digest pages from the cache only")
    dg.add_argument("--day", help="which day's page, YYYY-MM-DD (default today)")
    dg.set_defaults(func=cmd_digest)
    sub.add_parser("check", help="check config, Zulip sign-in and the model").set_defaults(func=cmd_check)
    sub.add_parser("push-test", help="send a test notification").set_defaults(func=cmd_push_test)
    args = p.parse_args(argv)
    try:
        cfg = load(args.config)
        return args.func(cfg, args)
    except (ConfigError, GuardError, ZulipError) as e:
        say(f"crier: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
