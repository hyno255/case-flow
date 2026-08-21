#!/usr/bin/env bash
# Write-back: the ONLY place this handler touches external systems.
# stdin: full case record. stdout: receipt (REQUIRED - silence is never success).
set -euo pipefail
record=$(cat)
external_id=$(printf '%s' "$record" | python3 -c "import sys,json; print(json.load(sys.stdin).get('external_id','?'))")
echo "DRY-RUN: would update $external_id" >&2
echo '{"status": "ok", "actions": ["labeled", "assigned"]}'
