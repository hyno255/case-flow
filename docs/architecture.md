# Architecture

How Caseflow works and why it is shaped this way. Hands-on guides:
[getting started](getting-started.md) · wire API: [protocol](protocol.md).

## Principles

1. **AI at the leaves, code at the branches.** Agents judge one case at a
   time; routing, sequencing, retries, and state are deterministic code and
   config. Exactly one automation home — plugin code in git — never a second
   engine on the case spine.
2. **Everything installable is a plugin.** One git package format, four
   kinds: sources (bring cases in), handlers (judge them), evaluators
   (structure decisions), recall (retrieval). Same verbs for every kind.
3. **The platform never calls a model API, never holds a credential.** All
   intelligence runs through ONE agent runner — the pi CLI (any provider),
   spawned as a subprocess under the user's own auth. Caseflow config names
   a model; the runner holds the keys. Exec stages can shell out to any CLI
   they like — still the user's auth.
4. **Humans hold the gate.** One mode ships: every case waits for a human
   eval. Automation tiers are a designed future, unlocked per field by
   benchmark evidence — never by config alone.
5. **Silence is never success.** Receipts for every write, explicit `lost`
   rows for expired leases, a status view for every state.
6. **Append-only history.** Decisions, corrections, failures — new rows,
   never updates.
7. **Metadata in the hub, content in files.** The hub stores each case's
   small metadata and a pointer; the full material lives in the case home's
   `source/` zone, and what a changed source record *means* is the source
   plugin's declared policy — the platform only triggers it and applies the
   verdict.

## System overview

```
 sources (plugins)          THE HUB (one small local service)         handlers (plugins)
┌──────────────┐   fetch   ┌──────────────────────────────────┐  claim  ┌──────────────────┐
│ fetch.sh +   │──────────▶│ intake: metadata + content ptr   │◀───────▶│ stages: YOUR     │
│ scope +      │  reopen   │ routes: source → handler         │ leases  │ scripts, run     │
│ change policy│──────────▶│ attempts: append-only, validated │         │ by bash or by the│
└──────┬───────┘           │ evals: the human decisions       │         │ agent orchestr.  │
       │ content (files)   │ claims/leases · runs · stats     │         └──────────────────┘
       ▼                   └──────────────────────────────────┘
 .caseflow/cases/<id>/gen-<n>/     ▲                    ▲
   source/ · context/ · artifacts/ │                    │
 knowledge/ (git)                  │                    │
┌──────────────┐  scan/search      │                    │
│ CASE.md      │◀──────────  eval (decide → write back → bank)
│ evidence/    │──────────▶  recall (CLI + MCP) · eval --handler (blind bench)
│ ANSWER.md    │
└──────────────┘
```

- **Hub**: Fastify + SQLite, binds localhost. Owns case state, routing,
  claims/leases, server-side validation, evals, and the knowledge search.
  Per case it stores only *metadata* — the shared spine (ids, title, state,
  generation, timestamps, promoted fields), a small source-specific `meta`
  JSON, and a `content` pointer to the case home. Knowledge itself lives in
  git — the hub's index is a rebuildable scan.
- **Runtime**: executes each stage inside the **case home** — one directory
  per case × generation with `case.json` (platform-written: metadata + prior
  results) and three zones: `source/` (the full material, written at fetch
  by the source plugin, read-only after), `context/` (plugin scratch:
  checkouts, builds — rebuildable, wiped at done), and `artifacts/` (the
  durable lane — pointer-tracked, fed to write-back, promoted to knowledge
  evidence at eval). Every stage is a plugin script with one of two
  executors: `exec:` runs it under bash directly (record on stdin, JSON on
  stdout — no retry, deterministic); `agent:` hands the same script to the
  runner as orchestrator — run it in the workspace, write only within it,
  flag any issue, answer with the stage's JSON — with the stage's `prompt:`
  injected verbatim.
  Stages of one pass share the home; concurrent cases and re-opened
  generations never collide. JSON extraction, validation, one retry with
  errors appended (agent stages only). Two entry points over one engine:
  the live path (hub-claimed, every outcome submitted) and the blind-replay
  path (bench: fresh home, frozen source material exposed, no answer in
  context).
- **CLI**: the 10-verb surface, plugin scaffolds, the MCP server, the status
  page, and `caseflow agent` — the interactive ops copilot: the runner
  launched with shipped operator skills (health, operate, failure triage,
  explain), acting only through the same public verbs and read API under the
  user's auth. A thin shell over the hub client — any richer client uses the
  same API.

## The case lifecycle

```
intake ──route──▶ routed ──claim──▶ processing ──▶ needs_eval ──eval──▶ writing_back ──receipt──▶ done
           │                          │  lost lease → explicit row, back to claimable       │
           ▼                          ▼                                     failed receipt ─┘ (stays retryable)
       unrouted                dismissed (screen) / error (invalid output) — visible in `status problems`
```

A re-fetched known case refreshes metadata and nothing else — unless the
source's declared `on_existing` policy (replace, or a delta hook reading old
and new content) says the new material supersedes the old. Then the case
**re-opens**: generation bump, fresh `source/`, back through the pipeline —
never while a runtime holds a live lease.

Claims carry ~15-minute leases extended by heartbeats; a vanished runtime's
cases return to the pool with an explicit `lost` row, resuming from the last
persisted stage — attempt numbers are hub-negotiated, submissions idempotent.
The hub accepts results only from the live-claim holder, only in the right
state, only for declared stages, and re-validates every result: no
submission can skip the eval gate or re-open a settled case.

## The knowledge loop

Deciding a case (`eval`) banks it as a package — problem and verification in
separate files (`CASE.md` + `evidence/` vs `ANSWER.md`), per-field trust
grades (`approved`/`corrected`), with the case home's `source/` material and
artifacts promoted into `evidence/` and earlier generations of the same case
auto-archived. That split is what makes the corpus
dual-use: `recall` serves the lessons to humans and (via MCP) to agents
mid-task; `eval --handler` replays the problems blind and scores the handler
per field — enum fields exactly, free-text fields via the evaluator against
the recorded analysis. See [eval-and-knowledge.md](eval-and-knowledge.md).

## Security model

- No model or source credentials in the hub, ever; execution follows auth.
- Each case runs in its own home holding only that case's data; prompts
  mark case content as untrusted data.
- External writes happen only through declared write-back scripts producing
  receipts.
- The hub binds localhost and assumes a trusted environment — put auth in
  front of it before any shared deployment.
- `requires.check` commands in a manifest run under your shell at `doctor` /
  `process`: treat third-party plugins like any code you run, and read them
  first.
