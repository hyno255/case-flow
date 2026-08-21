/**
 * Embedded scaffolds for `source init` / `handler init` — working products to
 * edit down, not skeletons to build up. No files are read from the install
 * tree, so init works anywhere.
 */
import type { KnowledgePackage } from "@caseflow/protocol";

export const SOURCE_YAML = (id: string) => `# Source plugin: brings cases in. Scope lives HERE, never on the command line.
id: ${id}
run: ./fetch.sh
params:
  repo: { default: "" }        # <-- set your scope, e.g. your-org/your-app
  since: { default: 24h }
# What a re-fetched KNOWN case means is this source's call, not the platform's:
#   ignore  — nothing (default); replace — always re-open with the new content;
#   delta   — ask ./delta.sh (stdin {old:{path,meta}, new:{path,meta}} ->
#             stdout {"changed": true|false}; the script may diff files or ask your agent).
on_existing: ignore
requires:
  tools:
    - { name: curl, check: "curl --version" }
    - { name: jq, check: "jq --version" }
`;

export const SOURCE_FETCH = `#!/usr/bin/env bash
# Fetch open GitHub issues. Works unauthenticated for public repos; swap the
# curl for \`gh\` or your own system's CLI.
#
# The fetch contract:
#   stdout — one METADATA line per case: {external_id, title, meta}
#            (external_id stable; meta small — it is the search/prompt seed)
#   files  — full content (any size) under "$out/<external_id>/", e.g. the
#            description, attachments, logs; it becomes the case's source/ zone
# Params arrive as flags (--repo ... --since ...); --out is platform-provided.
set -euo pipefail
repo="" since="24h" out=""
while [[ $# -gt 0 ]]; do case "$1" in
  --repo)  repo="$2";  shift 2 ;;
  --since) since="$2"; shift 2 ;;
  --out)   out="$2";   shift 2 ;;
  *) shift ;;
esac; done
[[ -n "$repo" ]] || { echo "set repo: in source.yaml (scope lives in the plugin)" >&2; exit 1; }

curl -sf "https://api.github.com/repos/$repo/issues?state=open&per_page=30" |
jq -c '.[] | select(.pull_request == null)' |
while read -r issue; do
  id="$repo#$(jq -r '.number' <<<"$issue")"
  mkdir -p "$out/$id"
  jq -r '.body // "(no description)"' <<<"$issue" > "$out/$id/description.md"
  jq -c --arg repo "$repo" --arg id "$id" '{
    external_id: $id,
    title: .title,
    meta: { repo: $repo, number: .number, author: .user.login, labels: [.labels[].name] }
  }' <<<"$issue"
done
`;

export const HANDLER_YAML = (id: string) => `# Handler plugin: judges cases. Behavior lives here; deployment lives on routes.
# Every stage is YOUR script; the key picks its executor — exactly one of:
#   exec: <script>   bash runs it directly (deterministic, no AI;
#                    case record on stdin, JSON on stdout)
#   agent: <script>  the configured agent runs it as orchestrator — executes the
#                    script in the case workspace, flags any issue, answers with
#                    the stage's JSON. \`prompt:\` injects your guidance into it.
id: ${id}
version: 0.1.0
stages:
  - name: triage
    agent: ./triage.sh           # CASEFLOW_AGENT=mock dry-runs agent stages without credentials
    prompt: |
      Classify this case from the script's output and ./source/.
      Severity rubric (edit to match your world):
      - critical: data loss, security exposure, payment failure, full outage
      - high: core flow broken for a user segment, no workaround
      - medium: degraded experience, workaround exists
      - low: cosmetic, copy, minor UX
      Owner mapping: crashes/errors -> oncall · performance -> infra · copy/UI -> design.
      summary: one sentence a reviewer can verify quickly.
    output_schema:
      severity: { enum: [low, medium, high, critical] }   # enum -> exact-checked in bench
      owner: string                                       # free string -> AI-judged in bench
      summary: string
writeback: ./writeback.sh        # stdin: case record -> stdout: receipt (REQUIRED)
promotes:                        # feed status/reporting without coupling
  severity: triage.severity
  owner: triage.owner
# evaluator: ./my-evaluator.md   # optional: override the first-party evaluator instructions
`;

export const HANDLER_TRIAGE_SCRIPT = `#!/usr/bin/env bash
# The stage script: gather this case's evidence. The agent executor runs it,
# then judges per the prompt in handler.yaml. Grow it as needed — pull logs,
# run a repro, query your systems.
set -euo pipefail
cat ./case.json
echo "--- source material ---"
cat ./source/* 2>/dev/null || echo "(no source files)"
`;

export const HANDLER_WRITEBACK = `#!/usr/bin/env bash
# Write-back: the ONLY place this handler touches external systems.
# stdin: full case record. stdout: receipt (REQUIRED — silence is never success).
# Real version, e.g.: gh issue edit "$number" --add-label "sev:$severity"
set -euo pipefail
record=$(cat)
external_id=$(printf '%s' "$record" | python3 -c "import sys,json; print(json.load(sys.stdin).get('external_id','?'))")
echo "DRY-RUN: would update $external_id" >&2
echo '{"status": "ok", "actions": ["labeled", "assigned"]}'
`;

/**
 * Seed knowledge: shipped examples that double as the first benchmark cases.
 * `source` holds the files a replay finds under evidence/source/ — the same
 * material a live case would carry in its home's source/ zone.
 */
export const SEED_CASES = (handlerId: string): { pkg: Omit<KnowledgePackage, "dir">; source: Record<string, string> }[] => [
  {
    pkg: {
      case_id: "seed-1", handler_id: handlerId, source_id: "seed",
      title: "Crash on login when session token expired",
      tags: ["crash", "auth", "seed"],
      banked_at: "2026-08-27T00:00:00.000Z",
      case: {
        external_id: "SEED-1", title: "Crash on login when session token expired",
        meta: { labels: ["bug"] },
      },
      fields: {
        severity: { value: "high", grade: "corrected" },
        owner: { value: "oncall", grade: "approved" },
        summary: { value: "Expired-token path dereferences a null session on login", grade: "approved" },
      },
      decided_by: "seed",
      lesson: "Crashes in the login path are high severity even when a restart works around them — the affected user cannot reach the workaround.",
      analysis: "Agent proposed medium citing the restart workaround; corrected to high: login is the front door, and the crash blocks the user before any workaround applies.",
    },
    source: {
      "description.md": "App crashes with a null-pointer when the stored session token has expired and the user taps login. Restarting the app works around it once, but the crash returns on the next expired token.\n",
    },
  },
  {
    pkg: {
      case_id: "seed-2", handler_id: handlerId, source_id: "seed",
      title: "Typo on the receipt email subject line",
      tags: ["copy", "email", "seed"],
      banked_at: "2026-08-27T00:00:00.000Z",
      case: {
        external_id: "SEED-2", title: "Typo on the receipt email subject line",
        meta: { labels: ["bug"] },
      },
      fields: {
        severity: { value: "low", grade: "approved" },
        owner: { value: "design", grade: "approved" },
        summary: { value: "Spelling fix in the receipt email subject", grade: "approved" },
      },
      decided_by: "seed",
      lesson: "Customer-visible copy errors are real but low severity when no flow is affected.",
      analysis: "Agent proposal accepted unchanged.",
    },
    source: {
      "description.md": "'recieved' should be 'received' in the order confirmation subject.\n",
    },
  },
];
