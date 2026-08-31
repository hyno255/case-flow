# Eval & knowledge

The part that compounds. Every case a human decides becomes, in one action,
a recorded decision, a receipted write-back, and a **knowledge package** —
which is simultaneously wiki material (`recall`) and a benchmark item
(`eval --handler`).

## The eval verb

```bash
caseflow eval <case>                    # bare: confirm the agent's proposal
caseflow eval <case> "your decision"    # text: your decision or an instruction
caseflow eval --handler <h> [--sample n]   # the benchmark (below)
```

- **Bare confirmation** grades every proposal field `approved`. No agent
  spawns — one keystroke stays free.
- **With text**, the handler's **evaluator** (first-party instructions,
  overridable per handler, run through the default agent) structures your
  words into per-field verdicts:
  fields you contradicted become `corrected` with your value; the rest stay
  `approved`. The evaluator also writes the `lesson` and `reasons` that
  become the package body. It never gates you — it structures you.
- Then, atomically from your point of view: the decision is recorded
  (append-only; corrections win everywhere downstream, including promoted
  fields), the write-back runs and prints its receipt (failures stay
  retryable via the next `process` sweep), and the package is banked.

## The knowledge package

```
knowledge/2026-08-27-crash-on-login/
├── CASE.md              # the PROBLEM — all a handler may ever see at replay
├── evidence/
│   ├── case.json        # the frozen intake metadata (replay input)
│   ├── source/…         # the case home's source/ zone — the material as decided
│   ├── artifacts/…      # the pipeline's artifacts lane, promoted at banking
│   └── logs/…           # the execution record — how the pipeline got here
└── ANSWER.md            # the VERIFICATION — per-field {value, grade} + Lesson + Analysis
```

Banking promotes exactly what should survive: the case **as the generation the
human decided** (not whatever the latest fetch mutated it into) — its intake
metadata and its `source/` material — plus the pipeline's `artifacts/`
(size-capped). `context/` never crosses — it's rebuildable by contract; code
evidence stays a `repo@commit` pointer, never a copy. When the source
re-opens a case and you decide the new generation, the earlier package is
automatically marked `archived: true` — recall and the bench see only the
current truth, history stays on disk.

Plain markdown in git: open it, edit the lesson, PR-review a teammate's
correction. The hub holds no truth about knowledge — it only scans and
searches these files, so the index is rebuildable by construction.

**Trust grades** read as what happened: `approved` (human confirmed the
agent's value) · `corrected` (human supplied the value) · `auto` (reserved
for the future automation tier). Only approved/corrected fields feed recall
and the benchmark.

## Recall — the wiki, for humans and agents

```bash
caseflow recall "EU latency"
```

Lexical search over the packages (evaluated cases only, always), returning
lessons with their outcomes. The identical query is an **MCP tool**, so any
agent can consult the wiki mid-task — pull, not push; the agent's own
judgment decides when the past is relevant:

```bash
claude mcp add caseflow -- caseflow mcp
# tools: recall_knowledge(query, k) · get_case(id)
```

## The benchmark — your history, replayed blind

```bash
caseflow eval --handler my-team/triage
#   severity   92%  (11/12)
#   owner      83%  (10/12)
```

Each banked case replays through the handler's real stages in a **fresh case
home**, materialized exactly like a live one: the frozen `evidence/source/`
fills `source/`. **ANSWER.md — and the original pipeline's artifacts — stay
structurally outside the agent's context**: the replay must earn its
conclusions from the same inputs the original had, and the problem/answer
split makes that a property of the file layout, not a promise.
Scoring is derived from your schema, never configured:

- **enum fields** (severity) → exact match against the banked value;
- **free-string fields** (owner, summary) → the evaluator judges the fresh
  output against the banked value *and* your recorded analysis.

Use it the way you'd use a test suite: edit your rubric, replay, see which
field moved. `CASEFLOW_AGENT=mock` makes the replay free (and useless for
quality — it tests plumbing); the real signal costs real agent calls, so
`--sample` exists.

## Interop

```bash
caseflow status --format jsonl    # {input, ground_truth} per banked case
```

The standard eval-item shape — pipe your corpus into promptfoo, Braintrust,
or LangSmith whenever you want their tooling on top of your history.
