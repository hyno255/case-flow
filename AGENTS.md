# AGENTS.md — continuation guide

Guidance for AI coding agents (and fast-moving humans) continuing this work.

**Read [docs/architecture.md](docs/architecture.md) first**; the invariants
in [CONTRIBUTING.md](CONTRIBUTING.md) are binding. The docs/ tree is the
truth about the product.

## The verification bar

Every change must keep these green before it lands:

- `npm run typecheck` and `npm test` (store + server state-machine tests).
- `npm run quickstart` — the whole loop with the mock runner and dry-run
  write-backs: source/handler/route setup → fetch → process → status → bare
  and instructed eval → knowledge banked as markdown → recall → blind bench
  → `status --html`. This is the integration test.
- `caseflow doctor` passes on both scaffolds (`source init`, `handler init`)
  out of the box.
- Anything touching the runner path deserves one live run with a real agent
  (`agent.command` in `.caseflow/config.yaml` can point at any one-shot CLI).

## Architecture in three sentences

Everything installable is a plugin (one git package format; kinds: source,
handler, evaluator, recall). The hub stores per case only METADATA — the
shared spine, a small source-specific `meta` JSON, and a `content` pointer —
and owns state/routing/validation/evals; it only *indexes* knowledge — the
packages themselves are markdown in git (`CASE.md` + `evidence/` +
`ANSWER.md`, per-field grades approved|corrected|auto). Every case ×
generation gets a case home (`case.json` / `source/` fetch-written full
material / `context/` rebuildable scratch / `artifacts/` pointer-tracked
durable lane) — stages share it, cases never collide, and whether a
re-fetched case re-opens (generation bump, fresh source/) is the source
plugin's `on_existing` policy (ignore | replace | delta hook), which the
platform only triggers and applies. Banking promotes source/ + artifacts/
into evidence and auto-archives earlier generations' packages. The executor
has one engine, two executors over one script contract (`exec:` = bash runs
the plugin's script directly, deterministic, no retry; `agent:` = the pi
runner runs the same script as orchestrator — flags issues, judges per the
stage's `prompt:`), and two entry points: live (hub-claimed, every outcome
submitted) and blind replay (bench — fresh home, frozen evidence/source
exposed, no ANSWER and no prior artifacts in context). `caseflow agent`
launches the same runner interactively with shipped ops skills.

## Invariants (do not "simplify" away)

1. Platform never calls model APIs / holds credentials — ONE agent runner
   (pi, any provider) spawned under user auth; config names a model, never a
   key. Exec stages are plain scripts.
2. Deterministic routing; never an AI orchestrator.
3. Append-only `attempts`/`evals`; corrections are `overridden` rows;
   `UNIQUE(item, stage, attempt)` is the idempotency key.
4. Hub re-validates server-side; live-claim ownership + per-state
   preconditions on every submission; replays answer with the stored verdict.
5. Silence is never success: receipts required; expired leases write `lost`
   rows; every state has a `status` view.
6. Humans hold the gate: every case waits for `eval`; the evaluator plugin
   never gates a human decision (bare confirm spawns no agent).
7. Recall and the bench draw only from approved/corrected grades; ANSWER.md
   never enters a replayed handler's context.

## Known gaps / roadmap

1. **Automation ladder** (suggest → preselect → auto, per-field thresholds
   unlocked by N human-graded cases; auto-resolution audit sampling) —
   designed, deliberately not built yet.
2. **API auth** — hub binds localhost, no tokens; required before shared
   deployment.
3. **Retriever upgrades** — recall's search is a lexical scan (rebuildable by
   construction); embeddings computed runtime-side and an agentic librarian
   slot behind the same interface.
4. **Frozen tool backends in evidence/** — evidence carries the case's
   source material and artifacts (size-capped); the remaining gap is serving
   live-tool backends (logs/code queries) through the production tool
   interface at replay.
5. **Webhook sources**; **staleness flags**; **multi-rule routing** (routes
   are source→handler pairs only); **garbage collection for settled
   case-home generations** (they accumulate by design; a sweep for banked
   ones is future work).
6. **`--case` re-queue ergonomics**: `process --case` claims routed/
   processing cases; error-state cases need a manual state reset today.

## Development notes

- Node ≥ 20.11, npm workspaces; `npm run typecheck` = `tsc -b`. Day-to-day
  runs via `tsx` from source; `npm run build` for dist (hub build copies
  `schema.sql`).
- Env: `CASEFLOW_DB`, `CASEFLOW_PORT`, `CASEFLOW_HUB_URL`,
  `CASEFLOW_KNOWLEDGE` (corpus dir), `CASEFLOW_CASES` (case homes root),
  `CASEFLOW_AGENT=mock` (swap the runner for the synthetic backend —
  schema-conforming stage results, all-match evaluator judgments; the whole
  loop credential-free). Runner config: `.caseflow/config.yaml`
  (`agent.model`, optional `agent.command` override — keys never live
  there).
- Scaffold content is embedded in `packages/cli/src/scaffold.ts` — no
  install-tree file dependencies.
- Comments are self-contained: state the contract in plain words; never
  reference internal planning docs or phases.
