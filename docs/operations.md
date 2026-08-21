# Operations: status, the manager page, exports

Everything here derives from the store — decisions, promoted fields, run
history. Nobody files paperwork.

## `caseflow status`

```
$ caseflow status my-team/triage
states: {"needs_eval":3,"done":12}

NEEDS EVAL (3):
  01M1…BX  app#88455  [high infra]  Checkout page slow for EU users
  …
decide: caseflow eval <case> ["your decision"]

my-team/triage: 12 decided, 3 corrected — agreement 75% · 6.5h median to done
```

Views — every state has one, nothing is silent:

| View | Shows |
|---|---|
| `status … needs-eval` | Cases waiting on a human |
| `status … in-flight` | routed / processing / writing_back |
| `status … problems` | unrouted · error · dismissed — the ones needing attention |
| `status … done` | Completed cases |

**Agreement** = decided cases confirmed without a correction; it's computed
from recorded eval grades, and it's the number that tells you when a handler
has earned trust (and, later, automation).

## `caseflow status --html status.html`

A single self-contained HTML page (no scripts, no external assets — safe to
email or publish internally): per-handler metric cards (agreement, median
time-to-done, queue depth), state and severity bars, weekly intake. Cron it,
or attach it wherever your team reports.

## `caseflow status --format jsonl`

One line per banked knowledge case, in the standard eval-item shape:

```json
{"input": {"external_id": "…", "title": "…", "meta": {…}}, "ground_truth": {"severity": "high", "owner": "infra"}}
```

Only approved/corrected fields are exported. Pipe it into promptfoo,
Braintrust, or LangSmith when you want their tooling over your history.

## Recovery cheatsheet

| Symptom | Where it shows | Fix |
|---|---|---|
| Agent produced invalid output twice | `status problems` (error, raw output preserved) | Fix the instructions/schema; re-run with `process --case` once requeued — or ask `caseflow agent` to triage the failure from the recorded evidence |
| Write-back failed (tracker down) | case stays `writing_back` | Next `caseflow process <handler>` retries exactly the failures |
| Runtime died mid-run | explicit `lost` row; case claimable again | Just run `process` again — it resumes from the last persisted stage |
| Case matched no route | `status problems` (unrouted) | `caseflow route <source> <handler>` — unrouted cases re-route automatically |
