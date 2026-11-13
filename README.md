# crier

A town crier for the Recurse Center's Zulip. Every 20 minutes it reads the
channels you name, has RC's own model boil them down, and keeps two notes in
your Obsidian vault current: what's on at the hub right now, and the day's
digest. If you want, it also pushes the good bits to your phone.

It exists because Zulip at RC is a firehose and the fun stuff (an impromptu
talk, dumplings on the 4th floor, someone looking for a Raft pair) scrolls past
before you see it.

```
recurse.zulipchat.com ──read──▶ crier (on your Mac, every 20 min)
                                  │
                                  ├──summarize──▶ RC's LLM gateway ──▶ two DGX Sparks at the hub
                                  │
                                  ├──write──▶ your vault: zulip-now.md, zulip-YYYY-MM-DD.md
                                  │
                                  └──push──▶ ntfy on your Mac ──Tailscale──▶ your phone
```

Two sections carry the weight: **The wall**, because RC forbids sending Zulip
data to hosted AI and crier is built around that, and **Quick start**.

## What you get

Illustrative, with invented people and messages. `zulip-now.md` looks like this:

```markdown
# At the hub right now

_Updated Sat 14:30 by crier from 41 messages. Today's digest: [[zulip-2026-11-07]]._

## Right now at the hub

- Settlers of Catan at 6pm on the couches, two seats left [↗](https://recurse.zulipchat.com/#narrow/…)
- Ada is ordering dumplings for the 4th floor at 12:30; reply to get in [↗](…)

## Coming up

- **Fri 16:00**: talk on substitutability, main space [↗](…)
```

The day page, `zulip-YYYY-MM-DD.md`, has **For you** (the ten things you'd
hate to miss, ranked against your interests), **Check-ins** (one line per
person, with overlaps flagged so you know who to pair with) and **Hub today**.
Every bullet links to the Zulip message it came from.

## The wall

The RC manual forbids sending Zulip or Directory data to hosted AI. crier is
built so that keeping that rule doesn't depend on anyone remembering it.

1. **Zulip text goes to two kinds of place only.** `src/crier/guard.py` allows
   `recurse.zulipchat.com` for reading, and RC's LLM gateway or a model on
   localhost for summarizing. Any other host is refused when the config loads
   and again before every connection. Redirects are off, and proxy settings
   from the environment are ignored. Adding a host takes a code change. The
   LLM key goes only to RC's gateway, never to a model on localhost.
2. **The model never writes a link.** It cites message ids like `[m123]`, and
   `links.py` turns them into Zulip links using only the messages it was
   shown. A cited id that wasn't in its input is dropped, and a bullet with no
   real source left ends in `_(no source)_`. Any image, link, HTML or URL the
   model writes anyway is stripped, so a message telling the model to embed
   `![](https://...)` can't make Obsidian send text off the machine. Trust the
   ↗ links over the prose.
3. **Everything Zulip-derived stays on your machine:** the message cache
   (`.crier/` in your vault), the pages, and the push server's cache. Pushes
   go to an ntfy server on your Mac, which only your phone can reach over
   Tailscale. For instant iOS delivery, ntfy.sh relays a message id and a hash
   of the topic, never the text.
4. **The terminal shows counts, paths, your channel names and error codes,
   never message text.** You can run crier in a terminal an AI assistant can
   see. `tests/test_cli.py` and `tests/test_push.py` check this.
5. **This repo holds no Recurser data.** Every person and message in `tests/`
   and in this README is invented.

## Quick start

You need [uv](https://docs.astral.sh/uv/), an RC Zulip account, and an API
key from llm.recurse.com.

```sh
uv tool install --editable ~/projects/rc/crier      # wherever you cloned it
mkdir -p ~/.config/crier
cp config.example.toml ~/.config/crier/config.toml
```

1. **Zulip key.** Zulip → Personal settings → Account & privacy → API key →
   Download zuliprc, saved to `~/.config/crier/zuliprc` (`chmod 600` it).
2. **LLM key.** llm.recurse.com → API Keys. Put it in the keychain:
   `security add-generic-password -a "$USER" -s crier-llm -w` (it prompts, so
   the key stays out of your shell history).
3. **Channels.** Fill in `[channels]` in the config. Emoji are part of the
   name: write them as TOML escapes so copy-paste can't mangle them, e.g.
   `"\U0001F9D1‍\U0001F4BB current batches"` for 🧑‍💻.
4. **`crier check`.** It signs in to Zulip, checks each channel exists, and
   pings the model.
5. **`crier run --force`** once, then open `Areas/rc/digest/zulip-now.md`.
6. **Schedule it:**
   ```sh
   cp launchd/com.ianeff.crier.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ianeff.crier.plist
   ```
   The job runs every 20 minutes. crier skips runs outside `active_hours`
   (8:00–23:00 by default). The plist has my paths in it; change them to
   yours.

From here it's hands-off. Keep `zulip-now.md` open in Obsidian; its
"Updated" time tells you it's live.

## Phone pushes

New items from "Right now at the hub", "Coming up" and "For you" arrive as
notifications. Tapping one opens the Zulip message. An item counts as new
when it cites a message crier hasn't pushed before, so a reworded summary
doesn't buzz you twice. The first run after you turn pushes on is silent: it
marks everything already there as seen, so you don't get a day's backlog. More
than `max_per_run` (5) new items in one run fold into a single "…and N more".

Setup, once:

1. Sign in to Tailscale on the Mac and on the phone, and turn on HTTPS
   certificates for your tailnet (admin console → DNS).
2. `scripts/setup-push.sh`. It installs ntfy with Homebrew if it's missing,
   writes `~/.config/crier/ntfy.yml`, keeps ntfy running under launchd
   (`com.ianeff.crier-ntfy`), runs `tailscale serve`, and prints a `[push]`
   section with a random topic.
3. Paste that section into your config, then follow the printed phone steps:
   the ntfy app, your Mac's tailnet address as its server, subscribe to the
   topic.
4. `crier push-test`.

The phone has to be on Tailscale for a notification to show its text. Off the
tailnet, it can't reach the Mac to fetch the headline.

## Commands

| Command | Does |
|---|---|
| `crier run [--force] [--backfill HOURS]` | fetch, rewrite now + today + yesterday, push. `--force` ignores `active_hours`; `--backfill` re-reads every channel from HOURS ago |
| `crier fetch [--backfill HOURS]` | fetch only |
| `crier digest [--day YYYY-MM-DD]` | rewrite pages from the cache, no fetch, no push |
| `crier check` | config, vault path, Zulip sign-in, every channel, one model ping |
| `crier push-test` | one test notification |
| `crier grab [LINKS…] [--llm] [-o FILE]` | every Zulip `#narrow` link in the arguments, stdin or (by default) the clipboard → each whole conversation as Markdown, every person written `@Firstname` (`@AdaL` when two share a first name), onto the clipboard. `--llm` has RC's model catch surnames in prose too |

Exit codes: 0 fine, 1 a channel, section or push failed (the rest still ran),
2 config or guard error. A lock file stops the scheduled run and a manual one
from overlapping.

## How a section gets made

Each section is one instruction plus a slice of the cache. Messages go to the
model as `[m123] Sat 14:00 Name in #channel > topic: text`, grouped by thread,
cut to 1,500 characters each, at most 400 per section (keyword hits kept first
for "For you"), packed into calls of at most 150,000 characters. When a section
needs more than one call, a last call merges the partial answers. Check-ins
skip the merge, since each bullet is one person.

A section is cached by a hash of its input, its prompt and the model id, so a
run with nothing new makes zero model calls. RC's two Sparks serve the whole
batch, and this keeps crier off them when nothing has changed. "Right now" is
also redone every 30 minutes and "Coming up" every hour, because what counts as
over depends on the clock. A section the model fails says so on the page and
isn't cached; the next run tries again. An answer cut off at
`max_output_tokens` (8,192, reasoning included) says so too.

A channel's first fetch reaches back `lookback_hours` (24). Event channels
reach back `events_lookback_hours` (72), so an announcement from Thursday about
Saturday still counts.

`[me] profile` is what does the steering. `interests` are plain keywords: a
hit marks the line with ★, and the prompt treats that as a hint.

## Configuration

Every key, with its default and what it does, is in `config.example.toml`.
Secrets stay out of the config: the Zulip key lives in the zuliprc, and the
LLM key comes from `api_key_cmd` (the keychain) or `api_key_env`. To use a
local model instead, point `base_url` at Ollama (`http://localhost:11434/v1`)
and drop the key.

## Not built

- @-mentions and DMs.
- RC's calendar. "Coming up" only knows what was announced in the channels in
  `events`.
- Writing to Zulip. crier only reads.
- The Anthropic request shape (`api = "anthropic"`) against the live gateway.
  Only the OpenAI shape has been exercised.

## Development

```sh
uv sync
uv run pytest        # Zulip, the model and ntfy are all faked; no network
uv run ruff check .
```

Written with Claude (Anthropic), against invented fixtures only. No Zulip text
went to it.
