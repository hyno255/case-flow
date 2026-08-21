#!/usr/bin/env bash
# The full loop, hands-on, with ZERO real agents (CASEFLOW_AGENT=mock) and
# dry-run write-backs. Proves: fetch -> route -> process -> status -> eval
# (confirm + correct) -> knowledge banked -> recall -> bench.
set -euo pipefail
cd "$(dirname "$0")/.."
WORK=$(mktemp -d /tmp/caseflow-quickstart.XXXX)
export CASEFLOW_DB="$WORK/hub.db"
export CASEFLOW_KNOWLEDGE="$WORK/knowledge"
export CASEFLOW_CASES="$WORK/cases"
export CASEFLOW_AGENT=mock
HUB_URL=${CASEFLOW_HUB_URL:-http://127.0.0.1:7377}

if curl -sf "$HUB_URL/v1/status" >/dev/null 2>&1; then
  echo "✖ something is already listening at $HUB_URL — stop it first"; exit 1
fi
npx tsx packages/hub/src/server.ts > "$WORK/hub.log" 2>&1 & HUB_PID=$!
trap "kill $HUB_PID 2>/dev/null || true" EXIT
for _ in $(seq 1 30); do curl -sf "$HUB_URL/v1/status" >/dev/null 2>&1 && break; sleep 0.3; done
kill -0 "$HUB_PID" 2>/dev/null || { echo "✖ hub failed to start (see $WORK/hub.log)"; exit 1; }
T="npx tsx packages/cli/src/caseflow.ts"

echo "━━ setup: source + handler + route ━━"
$T source add examples/sources/demo-bugs
$T handler add examples/handlers/bug-triage
$T route demo-bugs demo/bug-triage
$T doctor examples/handlers/bug-triage           # mock runner: everything passes credential-free

echo; echo "━━ fetch (cheap, no AI) — twice: known cases follow the source's on_existing policy ━━"
$T fetch demo-bugs
$T fetch demo-bugs

echo; echo "━━ process (agents judge; nothing written back) ━━"
$T process demo/bug-triage

echo; echo "━━ status: the queue ━━"
$T status demo/bug-triage

FIRST=$(curl -s "$HUB_URL/v1/status" | python3 -c "import sys,json; print(json.load(sys.stdin)['needs_eval'][0]['item_id'])")
SECOND=$(curl -s "$HUB_URL/v1/status" | python3 -c "import sys,json; print(json.load(sys.stdin)['needs_eval'][1]['item_id'])")

echo; echo "━━ eval: bare = confirm the proposal ━━"
$T eval "$FIRST"

echo; echo "━━ eval: with text = your decision (evaluator structures it) ━━"
$T eval "$SECOND" "severity is high - EU checkout latency breaches partner SLA; infra owns CDN"

echo; echo "━━ recall: ask what you've decided before ━━"
$T recall "login crash"

echo; echo "━━ bench: blind-replay banked cases, per-field scores ━━"
$T eval --handler demo/bug-triage

echo; echo "━━ status page for managers ━━"
$T status --html "$WORK/status.html"

echo; echo "Quickstart complete."
echo "  hub db:    $CASEFLOW_DB"
echo "  cases:     $CASEFLOW_CASES   (one home per case x generation: source/ context/ artifacts/)"
echo "  knowledge: $CASEFLOW_KNOWLEDGE   (open the banked packages - they're just markdown)"
echo "  status:    $WORK/status.html"
