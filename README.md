# Caseflow

> **Turn any stream of work into cases: your AI judges, you decide, every decision compounds.**

Caseflow is an open-source, local-first platform for recurring judged work —
bug reports, security findings, CI failures, tickets. Your own AI agent does
the first pass on every case; you hold the gate; and each decision is banked
as knowledge your agents recall and a benchmark they must pass.

```
fetch → route → process (your AI) → eval (you) → write-back (receipted)
                                        │
                                        └──► knowledge ──► recall (wiki for agents, via MCP)
                                                      └──► bench  (blind replay, per-field scores)
```

**AI at the leaves, code at the branches.** Agents judge one case at a time;
routing, state, retries, and receipts are deterministic platform code. The
platform never calls a model API and never holds a credential — one agent
runner (the [pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
CLI, any provider) is spawned under your own auth, and deterministic stages
run as plain scripts.

## Quick start (two minutes, zero credentials)

Requires Node.js ≥ 20.11.

```bash
git clone https://github.com/hyno255/case-flow && cd case-flow
npm install
npm run quickstart
```

The quickstart runs the entire loop with a mock agent and dry-run write-backs:
fetch three bugs → route → process → the eval queue → a bare confirmation →
a corrected decision (`eval <case> "severity is high — breaches partner SLA"`)
→ two knowledge packages banked as plain markdown → `recall "login crash"`
finds the lesson → `eval --handler` blind-replays the banked cases and scores
the handler per field. Read `scripts/quickstart.sh` — it is the product in
50 lines.

Then do it for real: [docs/getting-started.md](docs/getting-started.md).

## The surface — 10 verbs

```bash
# setup
caseflow source init <name>      # scaffold a source plugin (scope lives in its yaml)
caseflow source add <dir|url>    # install & register it
caseflow handler init <name>     # scaffold a handler plugin (stage script + rubric + write-back)
caseflow handler add <dir|url>   # install & register (installs only — never a hidden bench run)
caseflow route <source> <handler>
caseflow doctor [dir]            # the runner, tools, and plugin contract checks

# daily
caseflow fetch <source>          # cheap: scripts + the source's change policy, no AI — cron it
caseflow process <handler> [--case <id>]   # your agents judge the queue
caseflow status [handler] [view] [--html f]
caseflow eval <case> ["your decision"]     # decide → write back → bank knowledge, one action
caseflow recall "query"          # ask everything you've decided before
caseflow agent ["question"]      # ops copilot: health, runs, failure triage (interactive without args)

# measure
caseflow eval --handler <h>      # blind-replay banked cases; per-field scores
```

Every case a human decides becomes a **knowledge package** — plain markdown
in git (`CASE.md` + frozen `evidence/` + `ANSWER.md` with per-field trust
grades). The same files are the wiki (`recall`, also an MCP tool any agent
can call) and the benchmark (`eval --handler` replays the problems blind —
the answers never enter the agent's context).

```bash
# give any MCP-speaking agent the wiki:
claude mcp add caseflow -- caseflow mcp
```

## What the platform guarantees

- **Never calls model APIs, never holds credentials** — the agent runner and
  fetch scripts run under your logins; config names a model, never a key.
- **Deterministic routing** — a config table, never an AI orchestrator.
- **Humans hold the gate** — nothing writes back until a case is evaluated;
  every write leaves a receipt; failures retry surgically.
- **Append-only history** — decisions, corrections, and lost leases are
  explicit rows; silence is never success.
- **Server-side validation** — the hub re-validates every agent result
  against the handler's declared schema and accepts submissions only from
  the runtime holding a live claim, in the right state.
- **Metadata in the hub, content in files** — the hub stores each case's
  small metadata and a pointer; full content lives in the case's home
  directory, owned by the source plugin (including what a change means).

## Repository layout

```
packages/protocol/   Contracts: intake, states, manifests, schemas,
                     knowledge packages, wire API (zod)
packages/hub/        Headless service: SQLite store, routing, claims/leases,
                     validation, the eval gate, knowledge search
packages/runtime/    Stage engine (bash + agent-orchestrator executors,
                     live + blind-replay), the runner, evaluator, doctor, hub client
packages/cli/        caseflow — the CLI, scaffolds, MCP server, status page
examples/            demo-bugs source + bug-triage handler (power the quickstart)
docs/                Guides: getting started, handlers, sources, eval &
                     knowledge, architecture, protocol, operations
```

## Documentation

- [Getting started](docs/getting-started.md) — from clone to your first decided case
- [Writing a handler](docs/writing-a-handler.md) — manifest, stage scripts and executors, write-back, evaluator
- [Writing a source](docs/writing-a-source.md) — the fetch contract, change policy; scope lives in the plugin
- [Eval & knowledge](docs/eval-and-knowledge.md) — decisions, trust grades, recall, the benchmark
- [Architecture](docs/architecture.md) — how it works and why
- [Protocol](docs/protocol.md) — the hub API and its invariants
- [Operations](docs/operations.md) — status views, the manager page, JSONL export
- [AGENTS.md](AGENTS.md) — continuing development with an AI coding agent

## License

[Apache-2.0](LICENSE)
