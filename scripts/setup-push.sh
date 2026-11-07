#!/usr/bin/env bash
# One-time setup for crier phone pushes. Safe to re-run.
#
# Needs: Tailscale signed in on this Mac, and HTTPS certificates turned on for
# the tailnet (admin console → DNS → HTTPS Certificates). Installs ntfy if
# it's missing, writes its config, keeps it running under launchd, puts it on
# the tailnet with `tailscale serve`, and prints what to paste and tap.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
conf="$HOME/.config/crier"
yml="$conf/ntfy.yml"
plist="$HOME/Library/LaunchAgents/com.ianeff.crier-ntfy.plist"
uid="$(id -u)"

say() { printf '\n==> %s\n' "$*"; }

say "Tailscale"
if ! tailscale status >/dev/null 2>&1; then
  echo "Tailscale isn't running or signed in. Open Tailscale.app, sign in, then re-run this."
  exit 1
fi
dns="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
echo "this Mac is $dns"

say "ntfy"
if ! command -v ntfy >/dev/null 2>&1; then
  brew install ntfy
fi
ntfy="$(command -v ntfy)"
echo "using $ntfy"

say "ntfy config → $yml"
mkdir -p "$conf"
if [[ -e "$yml" ]]; then
  echo "exists; leaving it alone"
else
  sed "s#https://MAC.TAILNET.ts.net#https://$dns#" "$repo/push/ntfy-server.example.yml" > "$yml"
  echo "written"
fi

say "launchd job"
sed "s#/opt/homebrew/bin/ntfy#$ntfy#" "$repo/launchd/com.ianeff.crier-ntfy.plist" > "$plist"
launchctl bootout "gui/$uid/com.ianeff.crier-ntfy" 2>/dev/null || true
launchctl bootstrap "gui/$uid" "$plist"
sleep 2
curl -fsS http://127.0.0.1:2586/v1/health && echo

say "tailscale serve (tailnet only, HTTPS)"
tailscale serve --bg 2586
tailscale serve status

topic="crier-$(openssl rand -hex 8)"
say "Done. Add this to $conf/config.toml:"
cat <<EOF

[push]
server = "http://127.0.0.1:2586"
topic = "$topic"
# obsidian_vault = "recurse-vault"   # your RC vault's name in Obsidian, for a "Digest" button

EOF
cat <<EOF
On the phone:
  1. Install Tailscale and sign in to the same tailnet; leave it connected.
  2. Install ntfy (App Store). Settings → Default server → https://$dns
  3. Subscribe to the topic: $topic
Then run: crier push-test
EOF
