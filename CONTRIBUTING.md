# Contributing to Caseflow

Thanks for helping! This page covers the mechanics; the *why* behind the
architecture lives in [docs/architecture.md](docs/architecture.md).

## Development setup

Node.js ≥ 20.11, npm workspaces.

```bash
npm install
npm run typecheck     # tsc -b across all packages
npm test              # hub tests (throwaway SQLite, no server needed)
npm run quickstart    # full end-to-end loop with the mock agent
```

All three must pass before you open a PR — the quickstart is the integration test.

Notes:

- Sources run directly via `tsx`; `npm run build` compiles to each package's
  `dist/`. If you edit a package and something *built* imports it, rebuild —
  workspace imports resolve to `dist/`, so stale builds silently mask changes.
- `packages/hub/src/schema.sql` is loaded from the directory next to the code;
  the hub build copies it into `dist/`.
- Useful env: `CASEFLOW_DB`, `CASEFLOW_PORT`, `CASEFLOW_HUB_URL`.

## Invariants — do not "simplify" these away

PRs that break one of these will be declined regardless of how much code they
delete:

1. **Routing is deterministic.** A source→handler table; never an AI
   orchestrator.
2. **`attempts` and `evals` are append-only.** Corrections are new rows
   (`overridden` status), never UPDATEs. `UNIQUE(item_id, stage_name,
   attempt)` is the idempotency key.
3. **The hub re-validates every result server-side** and accepts submissions
   only from the live-claim holder, in the right state. Runtimes are
   untrusted.
4. **Silence is never success.** Write-back scripts must print receipts;
   expired leases write explicit `lost` rows; every state has a status view;
   run stats are reconciled from stored rows.
5. **The platform never holds model or source credentials.** The agent
   runner is spawned under the user's auth; config names a model, never a
   key. Execution follows auth.
6. **Humans hold the gate.** Every case waits for `eval`; the evaluator
   plugin never gates a human decision; recall and the bench draw only from
   approved/corrected grades, and ANSWER.md never enters a replayed
   handler's context.
7. **Plugins never touch storage.** All state flows through the protocol API.
8. **The hub stores metadata, never content.** A case row is the shared spine
   + small `meta` JSON + a `content` pointer into the case home; what a
   changed source record means belongs to the source plugin (`on_existing`),
   never to intake.

## What a good change looks like

- **Small and self-explanatory.** Comments state the invariant or contract
  they protect in plain words — never a reference the reader can't follow. If
  your change alters how the system works, update `docs/architecture.md` (or
  the relevant guide) in the same PR.
- **Errors point at the user's files**, never framework internals. `doctor`
  output is product surface — keep the "here's the fix" tone.
- **Contracts change in `packages/protocol` first.** Hub, runtime, and CLI
  consume the zod types; don't fork shapes locally.
- **Tests ride along.** Hub behavior belongs in `packages/hub/src/*.test.ts`;
  anything user-visible end-to-end belongs in `scripts/quickstart.sh`.

## Good first contributions

Roadmap items are listed in [AGENTS.md](AGENTS.md) (kept current with known
gaps). Template handlers for
new domains (pipeline failures, security findings) and real-world source
examples are especially welcome — they're self-contained and immediately
useful.

## Conduct

Be kind, assume good intent, critique code not people.
