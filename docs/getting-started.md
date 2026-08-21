# Getting started

From clone to your first decided case. The sandbox path (mock agent, dry-run
write-backs) takes minutes and needs no credentials; the real path needs the
agent runner — the pi CLI — with a provider you're authenticated to:

```bash
npm i -g @earendil-works/pi-coding-agent
# name your model once (keys stay in pi's own auth, never in caseflow):
mkdir -p .caseflow && printf 'agent:\n  model: anthropic/claude-sonnet-5\n' > .caseflow/config.yaml
```

**Credentials, set once** (no env var per shell): run `pi` and type `/login`
to store a provider key or subscription OAuth in `~/.pi/agent/auth.json` —
or write that file directly. Env vars (`ANTHROPIC_API_KEY`, …) also work but
are never required.

**Custom endpoints** (LiteLLM, vLLM, Ollama, corporate gateways): declare the
provider once in `~/.pi/agent/models.json` — any OpenAI- or
Anthropic-compatible endpoint works:

```json
{
  "providers": {
    "litellm": {
      "baseUrl": "https://litellm.your-company.com/v1",
      "api": "openai-completions",
      "apiKey": "!op read 'op://vault/litellm/key'",
      "models": [{ "id": "your-model-name" }]
    }
  }
}
```

(`apiKey` takes a literal, `$ENV_VAR`, or a `!command` for vault lookups.)
Then point caseflow at it: `agent.model: litellm/your-model-name`. Every
agent stage, the evaluator, and `caseflow agent` use it — one config, keys
never touch caseflow.

```bash
git clone https://github.com/hyno255/case-flow && cd case-flow
npm install
npm run quickstart        # the whole loop, sandboxed — watch it once
```

Alias the CLI and start the hub (one small local process that owns state):

```bash
alias caseflow="npx tsx $(pwd)/packages/cli/src/caseflow.ts"
npm run hub &             # 127.0.0.1:7377 · db at ./caseflow.db · knowledge at ./knowledge
```

Env knobs: `CASEFLOW_DB`, `CASEFLOW_PORT`, `CASEFLOW_HUB_URL`,
`CASEFLOW_KNOWLEDGE` (the knowledge corpus dir), `CASEFLOW_CASES` (the case
homes root), `CASEFLOW_AGENT=mock` (swap the runner for the synthetic
backend — the whole loop, zero credentials).

## 1. Point it at your stream

```bash
caseflow source init my-github
#   edit my-github/source.yaml → repo: your-org/your-app   (scope lives in the plugin)
caseflow source add ./my-github
```

The scaffolded source fetches open GitHub issues via the public API — swap
`fetch.sh` for anything honoring the fetch contract: one metadata line per
case (`{external_id, title, meta}`) plus the full content as files. What a
re-fetched known case means (ignore / replace / delta) is the source's own
declared policy. See [writing-a-source.md](writing-a-source.md).

## 2. Make the judge yours

```bash
caseflow handler init my-team/triage
#   edit triage/handler.yaml (the prompt) → your severity rubric, your owner mapping
#   edit triage/triage.sh                  → the evidence your stage gathers
#   edit triage/writeback.sh             → what "done" means (label, assign, comment)
caseflow handler add ./triage
caseflow route my-github my-team/triage
caseflow doctor ./triage
```

`doctor` checks the runner and its auth, every stage's script, and
dry-runs the write-back (it must print a receipt). The scaffold
ships two seed knowledge cases so recall and the bench are never empty.

## 3. The daily loop

```bash
caseflow fetch my-github            # cheap — cron this hourly; known cases follow your change policy
caseflow process my-team/triage     # the runner judges the queue (CASEFLOW_AGENT=mock to dry-run)
caseflow status                     # NEEDS EVAL n · problems · agreement
```

Nothing has touched GitHub yet. Every case waits for you:

```bash
caseflow eval 01ABC…                          # agent was right → confirm
caseflow eval 01DEF… "high — partner SLA breach; infra owns CDN"   # correct it
```

One action each: the decision is recorded, the write-back runs (receipted;
failures retry on the next `process`), and the case is banked into
`knowledge/` as a markdown package you can open, edit, or PR-review — with
the case's `source/` material and anything the agent saved to `artifacts/`
promoted into the package's `evidence/`. (Each case lives in its own
`.caseflow/cases/<case>/gen-<n>/` home, so concurrent cases and re-opened
versions never share files.)

## 4. The compounding part

```bash
caseflow recall "EU latency"        # the lesson from every similar past decision
caseflow agent                      # ops copilot: "check health", "triage BUG-2", "run the loop"
claude mcp add caseflow -- caseflow mcp    # let your agents ask the same wiki mid-task

caseflow eval --handler my-team/triage     # after editing your rubric:
#   severity   92%  (11/12)                # blind replay of banked cases,
#   owner      83%  (10/12)                # scored per field
```

Enum fields are checked exactly; free-text fields (owner, summary) are judged
by the evaluator against your recorded analysis. The answers never enter the
replayed agent's context. Details: [eval-and-knowledge.md](eval-and-knowledge.md).

## 5. For your manager

```bash
caseflow status --html status.html      # self-contained page: queues, severity,
                                        # agreement rate, time-to-done, weekly intake
caseflow status --format jsonl          # {input, ground_truth} per banked case —
                                        # pipe into promptfoo/Braintrust/LangSmith
```
