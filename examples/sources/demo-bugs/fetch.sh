#!/usr/bin/env bash
# The fetch contract: one METADATA line per case on stdout
# ({external_id, title, meta}); full content as files under "$out/<external_id>/".
set -euo pipefail
out=""
while [[ $# -gt 0 ]]; do case "$1" in
  --out) out="$2"; shift 2 ;;
  *) shift ;;
esac; done

emit() { # id, title, meta-json, description
  mkdir -p "$out/$1"
  printf '%s\n' "$4" > "$out/$1/description.md"
  printf '{"external_id": "%s", "title": "%s", "meta": %s}\n' "$1" "$2" "$3"
}

emit BUG-1 "Crash on login when session token expired" '{"labels": ["bug", "crash"]}' \
  "App crashes with a null-pointer when the stored session token has expired and the user taps login. Restarting the app works around it."
emit BUG-2 "Checkout page slow for EU users" '{"labels": ["bug", "performance"]}' \
  "p95 latency 4s from EU region since Tuesday. Possibly CDN config."
emit BUG-3 "Typo on receipt email" '{"labels": ["bug", "copy"]}' \
  "'recieved' should be 'received' in the order confirmation subject."
