# crier

A cron job that reads RC's Zulip so I don't have to read all of it. Every 20
minutes it pulls new messages from the channels I name, has RC's own model
(GLM on the two DGX Sparks at the hub) or a local one boil them down, and rewrites two
notes in my RC vault: what's on at the hub right now, and today's digest.

Two sections carry the weight: **The wall** (where Zulip text may and may not
go) and **Setup**. The rest is reference.

## The wall

The RC manual forbids sending Zulip or Directory data to hosted AI. crier is
built so that obeying that rule doesn't depend on me remembering it.

1. **Zulip text goes to two kinds of place only.** `src/crier/guard.py` allows
   `https://recurse.zulipchat.com` for reading, and RC's LLM gateway
   (`llm.rcdis.co`, plus `llm.recurse.com`) or localhost for summarizing.
   Per the llm.recurse.com FAQ, the gateway is a DigitalOcean droplet that
   checks the key and relays over Tailscale to the Sparks; it logs usage, not
   content, and never forwards to a model provider. RC staff could look if
   they had to, so the text does leave the building, inside RC's own setup. Anything else in the config is refused at
   load time, and every client checks again before it connects. Redirects are
   off. Widening the allowlist takes a code change.
2. **Everything Zulip-derived lives in the RC vault.** The message cache is
   `.crier/crier.sqlite3` and the pages are `Areas/rc/digest/`, both under
   `~/projects/rc/recurse-vault`, which hosted assistants on this machine are
   denied.
3. **stdout carries counts, paths, my own channel names and error codes.** It
   never carries message text, so running `crier` in a terminal an assistant
   can see leaks nothing. `tests/test_cli.py` checks this against the
   fixtures.
4. **This repo holds no Recurser data.** Every person and message in `tests/`
   is invented. Real config, keys and caches are gitignored and live outside
   it anyway.

## Setup

```sh
uv tool install --editable ~/projects/rc/crier
mkdir -p ~/.config/crier
cp ~/projects/rc/crier/config.example.toml ~/.config/crier/config.toml
```

Then:

1. **Zulip key.** Zulip → Settings → Account & privacy → API key → download the
   zuliprc to `~/.config/crier/zuliprc`.
2. **LLM key.** Make one at llm.recurse.com (API Keys) and put it in the
   keychain: `security add-generic-password -a "$USER" -s crier-llm -w` (it
   prompts). The example config already points at the gateway, OpenAI-shaped,
   with model `default`, RC's alias for whatever it's serving.
3. **Channels.** Fill in `[channels]`: `hub` for where people say what's on
   right now, `events` for announcements, `others` for anything worth mining
   for "For you". `checkins` defaults to `checkins`.
4. **`crier check`** signs in to Zulip, sends the model one tiny prompt, and
   reports.
5. **`crier run --force`** once by hand, then open
   `Areas/rc/digest/zulip-now.md` in Obsidian.
6. **Schedule it:** see the comment at the top of
   `launchd/com.ianeff.crier.plist`.

## What it writes

| Page | Sections | Window | Rewritten |
|---|---|---|---|
| `zulip-now.md` | Right now at the hub; Coming up | hub: last 3h; events: last 72h | every run |
| `zulip-YYYY-MM-DD.md` | For you; Check-ins; Hub today | that calendar day | every run, today and yesterday |

The day pages aren't named plain `YYYY-MM-DD.md` because the RC vault's daily
notes already are. Both pages carry `kind: digest` and `status: processed`, so
the filing clerk in the vault's `AGENTS.md` leaves them alone.

## How a section gets made

Each section is one instruction plus a slice of the cache. Messages go to the
model as `[m123] Sat 14:00 Name in #channel > topic: text`, grouped by thread,
cut to 1,500 characters each, at most 400 per section (keyword hits kept first
for "For you"), packed into calls of at most 150,000 characters (the context
is 262,144 tokens). When a section
needs more than one call, a final call merges the partial answers. Check-ins
skip the merge, since each bullet is one person.

The model never writes a URL. It cites `[m123]`, and `links.py` turns each
citation into a Zulip link built from the messages it was actually shown. A
cited id that wasn't in the input is dropped, and a bullet with no real
citation left gets `_(no source)_` appended. That's the hallucination tell:
trust the ↗ links, not the prose.

A section is cached by a hash of its input, its prompt and the model id, so a
run with nothing new makes zero model calls. "Right now" is also redone every
30 minutes and "Coming up" every hour, since what counts as over depends on the
clock. RC's two DGX Sparks serve the whole batch, and this keeps me off them
when nothing has changed. When the model fails, that section says so on the
page, nothing is cached, and the next run tries again. An answer that hits
`max_output_tokens` (8,192, reasoning included) ends with a line saying it was
cut off. Requests send `reasoning_effort = "low"`, which keeps GLM's reasoning
in its own field instead of in the answer.

`[me] interests` are plain keywords. A hit marks the message line with ★ and the
matched words, which the prompt treats as a hint. The `profile` paragraph does
the actual steering.

## Commands

| Command | Does |
|---|---|
| `crier run [--force]` | fetch, then rewrite now + today + yesterday; skips outside `active_hours` unless forced |
| `crier fetch` | fetch only |
| `crier digest [--day YYYY-MM-DD]` | rewrite pages from the cache, no fetch |
| `crier check` | config, vault path, Zulip sign-in, one model ping |

Exit codes: 0 fine, 1 some channel or section failed (the rest still ran), 2
config or guard error. A lock file stops a launchd run and a manual run from
overlapping.

## Not built yet

- @-mentions and DMs. They'd go in a third section if the digest turns out to
  bury them.
- RC's calendar. "Coming up" only knows what was announced in Zulip.
- Anything that writes back to Zulip. crier only reads.
- Tests against the live gateway. `tests/` fakes it with the OpenAI shape the
  FAQ describes; `crier check` is the live test.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```
