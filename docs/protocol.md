# Protocol reference

The hub speaks a small HTTP+JSON API. Anything that speaks it is a valid
runtime or client. Types live in `@caseflow/protocol` (zod schemas); a thin
typed client is `HubClient` in `@caseflow/runtime`.

Defaults: `http://127.0.0.1:7377` (`CASEFLOW_PORT` / `CASEFLOW_HUB_URL`).
The hub assumes a trusted network — add auth in front before sharing it.

## Invariants

1. **Explicit results only.** Every stage outcome arrives via
   `POST /v1/results` with a closed status enum
   (`ok | invalid_output | agent_error | precheck_failed | aborted`); the
   hub itself writes `lost` (lease expiry) and `overridden` (eval
   corrections) — runtimes cannot submit those.
2. **The hub re-validates.** `ok` results are checked against the handler's
   manifest schema server-side and downgraded to `invalid_output` on
   mismatch — trust `recorded_status`, not what you sent.
3. **Ownership + state preconditions.** Submissions are accepted only from
   the runtime holding a live claim, only for stages the manifest declares
   (400 otherwise), and only in the right state (`output` while
   `writing_back`, stages while `processing`) — 409 otherwise. Replays of a
   recorded `(case, stage, attempt)` return `idempotent_replay: true` with
   the stored verdict before any guard.
4. **Append-only history**; `UNIQUE(case, stage, attempt)` is the idempotency
   key; attempt numbers come from the hub (`next_attempts` in claims).

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/handshake` | Protocol + capability report (`{agents: [{name, ok}], tools: [{name, ok}]}`); required before claiming (426 on major-version mismatch). Claims 412 when the handler references an agent the runtime cannot resolve, or a required tool is missing |
| `POST /v1/ingest` | Metadata-only intake: `{source_id, cases_root, items:[{external_id, title, meta}]}` → counts `{new, existing, rejected, routed, unrouted}` + per-item `results` (`case_id`, `generation`, `existed`, `content` — the pointer the CLI fills with fetched files, `prior_meta` for delta hooks) |
| `POST /v1/cases/:id/reopen` | The source plugin's verdict that new content supersedes the old: bumps the generation, swings `content` to the fresh gen's `source/`, re-queues the case. Refused (`reopened: false`) while a live lease holds it |
| `POST /v1/handlers` · `GET /v1/handlers/:team/:name` | Register / fetch handler plugins (validated manifests) |
| `POST /v1/sources` · `GET /v1/sources` | Register / list source plugins |
| `POST /v1/routes` | `{source_id, handler_id}` — first-match wire; 404 for unregistered ends |
| `POST /v1/claims` | `{runtime_id, handler_id, phase: pipeline\|output, case_id?, limit}` → items with `meta`, `content` (the case home's source/ path), `generation` (keys the case-home dir; bumps when the source re-opens the case), `prior_results`, `next_attempts`, lease (~15 min; capability-gated, 412) |
| `POST /v1/heartbeat` | Extend this runtime's live leases (~60s cadence) |
| `POST /v1/results` | Submit a stage/output result (see invariants); optional `artifacts: [paths]` (pointers into the artifacts lane), `duration_ms`, and `log` (pointer into the logs lane) — content stays opaque to the platform |
| `POST /v1/evals` | The human decision: `{item_id, input?, fields: {name: {value, grade}}, reasons?, lesson?, user}` — 409 unless the case is `needs_eval`; corrections land as `overridden` rows; case → `writing_back` |
| `GET /v1/status` | Counts per state + `needs_eval` / `in_flight` / `problems` / `done` views |
| `GET /v1/stats` | Per-handler: states, severity, evals (decided/corrected/agreement), median time-to-done, weekly intake, last run |
| `GET /v1/knowledge/search?q=&k=&handler=` | Lexical search over the knowledge packages (evaluated grades only; rebuildable scan) |
| `POST /v1/runs` · `POST /v1/runs/:id/finish` | Run bookkeeping; stats reconciled by the hub from stored rows |
| `GET /v1/items/:id` | Full case record: `meta`, `content` pointer, state, `generation`, latest results (corrections winning), `artifacts` pointer union, per-attempt execution summary (`agent`, `duration_ms`, `log`) |

## State machine (hub-enforced)

```
new → routed → processing → needs_eval → writing_back → done
        │          │                          │
        ▼          ▼                          └ failed/missing receipt: stays writing_back (retryable)
    unrouted   dismissed (screen) · error (invalid output) · lost rows on lease expiry
```

An `output` receipt of `{"status":"ok"}` completes the case; `failed` or a
missing receipt keeps it `writing_back` so the next output-phase claim
retries exactly the failures.

## MCP

`caseflow mcp` serves stdio MCP with read-only tools
`recall_knowledge(query, k?)` and `get_case(id)` — backed by the same hub
endpoints. Writes never travel over MCP; they go through the receipted
write-back path exclusively.
