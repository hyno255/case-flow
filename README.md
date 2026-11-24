# agentflow

A small engine that takes items from a source (tickets, alerts, findings) through a flow of stages, where each stage is done
by a script or an agent and then checked. It orchestrates and routes; everything specific lives in plugin folders.
Every run can be traced down to each model call. What a run puts out, a report for example, is written by the flow's own script.

The engine is a Python program that runs on your machine, as you. It starts scripts and coding CLIs as plain child
processes, so a stage can be done by the Codex or Claude Code you already have installed and signed in to.

## Run it

It needs Python 3.12 or newer.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e .
```

```bash
.venv/bin/agentflow serve
```

The web UI is on http://localhost:8321. The other commands are run the same way (or activate the environment and type `agentflow`):

```bash
.venv/bin/agentflow check
```

```bash
.venv/bin/agentflow run vuln-triage --stand-in
```

`--stand-in` needs no key and costs nothing: the script the flow names as `stand_in` in its `flow.yaml` answers in the model's place
(the example's is `plugins/flows/vuln-triage/scripts/stand_in.py`, which also says what it is asked and what it prints). For a real model, put its key in `.env`
(for the example settings: `OPENAI_API_KEY=...`); the engine reads that file when it starts.

| Command | What it does |
|---|---|
| `check` | checks every plugin file and the settings, and runs every tool's health check |
| `run <flow>` | fetches the flow's source now and runs the flow over every item it accepts |
| `try <flow> [--item ID]` | one item as a trial; the flow's scripts are told it is one |
| `schedule [on\|off <flow>]` | shows the flows' schedules, or switches one |
| `show [run] [item]` | recent runs, or what one run or item did and cost |
| `resume <run>`, `cancel <run>`, `retry <run> [--item ID]` | what can be done with a run afterwards |
| `history [flow] [--item ID]` | for a flow's own scripts: how items ended in earlier runs and where their files are, as JSON |
| `serve` | the web UI, the API, the schedules and the health checks |

## The home folder

The engine works in one folder, its home: the folder you start it in, or the one `AGENTFLOW_HOME` names.

| In the home folder | What it is |
|---|---|
| `agentflow.yaml` | models and limits |
| `plugins/` | flows, agents, tools, sources |
| `data/` | the database, one folder per fetch, one folder per run; the engine writes nowhere else |
| `.env` | model keys |

A program that a script, a tool or an MCP server calls is found on your `PATH`, like any program you run yourself.

Nothing stands between a script and your machine. A script is a child process of the engine, run as you: it can read and
write what you can, and reach the network. What the engine controls is narrower:

- A script's environment holds only what the engine tells it, and none of the engine's keys.
- An agent on the engine's own runtime has no shell, and its file tools stay inside the item's work folder.
- A coding CLI keeps its own sandbox: Codex is started with writes limited to the work folder, Claude Code with edits accepted in it and no shell command but the tools it was given.
- There is no sign-in, so `serve` listens on `127.0.0.1` unless `--host` says otherwise.

Run flows whose scripts you trust, as you would any script.

## Plugins

One folder per plugin, four kinds:

```
plugins/
  sources/<name>/source.yaml   how items are got, and what is handed over
  flows/<name>/flow.yaml       which source it accepts, when, and its stages
               FLOW.md         what the flow is for; every agent in it is told this
               stages/<id>.md  what one stage does
               scripts/        the flow's own scripts
  agents/<name>/AGENT.md       description, model and tools on top; the method underneath
  tools/<name>/tool.yaml       a script with arguments and a health check, or the command of an MCP server
```

### A source hands over, a flow accepts, the engine routes

A source says how it gets items and what it hands over. It has no schedule and starts nothing.

```yaml
# plugins/sources/security-tickets/source.yaml
description: Open security tickets from the tracker.
run: ./fetch.sh          # how and where the items are got
output: tickets.json     # the file it writes into its output folder: a JSON list, each item with an "id"
fields: [service, package, fixed_in, severity, summary]   # what every item has
```

The script is told its output folder in `AGENTFLOW_OUT`. Every fetch keeps its own folder under `data/fetches/`.
A fetch that does not write the file, or whose items lack a listed field, fails, and no flow gets half an item.

A flow says which source it accepts, optionally which of its items, and when.

```yaml
# a flow.yaml
source: security-tickets
schedule: weekdays 07:00         # when the engine fetches the source for this flow; without it the flow starts by hand
where: {severity: [high, medium]}  # optional: only the items that match
stages:
  - id: gather
    run: scripts/gather.sh
  - id: analyse
    agent: origin-analyst
    check: scripts/chain-cites-files.py
    done_when: The origin is stated, and every link in the chain cites a file in the work folder.
```

Flows that accept the same source and come due at the same time share one fetch. A schedule is off until it is switched on,
on the flow's page or with `schedule on <flow>`.

A run works every item the flow accepts, each time, from its first stage; no run reads or continues another run's work,
so an item that failed is worked again by the next run. A flow that should
leave out what it has already handled does that itself: its source's script hands over only what is new, `where` narrows
the items, or a first stage script exits with 78 to end an item at once.

### Stages done once for the run

A stage is done for each item. With `once: true` it is done once for the run instead, when every item has got to it or
ended. The items are joined there, and from then on the run is one unit of work:

```yaml
# a flow.yaml
stages:
  - id: report                 # per item, like every stage above it
    done_when: report.md covers origin, chain, fix and evidence.
  - id: summary                # once: an agent reads what every item's stages wrote
    once: true
    done_when: summary.md names every ticket and how it ended.
  - id: publish                # once: a script writes the run's pages
    once: true
    run: scripts/report.py
```

- A `once` stage is a stage like any other: a script, an agent or the default agent does it, and it can have `check`,
  `done_when`, `review`, `tries`, `next` and `on_fail`. The engine has no step of its own at the end of a run; what
  follows the items, and in which order, is the flow's.
- It is handed the run and how each item ended, whatever the ending; what to make of an item that failed is the stage's business.
  A script gets it on its input, as JSON: `{"run", "flow", "trial", "started", "items": [{"id", "ended_as", "reason", "folder", "item"}]}`.
  An agent gets it in its prompt, and can read (not change) what each item's stages wrote, as `../<item id>/<file>`.
- The stages work in a folder of their own beside the items', `items/_all/`, and show on the run's page as one more row,
  `_all`, with its files, its trace, and its own ending. If one of them fails, `agentflow retry <run> --item _all` does them again.
- Every stage after the first `once` stage is `once` too, and its `on_fail` can name only another such stage.
- A cancelled run does not do them.

### What a run puts out

Every run has an `output/` folder, and the flow fills it. The engine lists and serves it, and keeps one file of its own there, `run.log`.

- Every script of a run is told `AGENTFLOW_OUTPUT`, that folder. The example flow's last stage, `publish`, is a `once`
  stage whose script writes an index and one page per ticket there.
- The run's page lists the folder under Output, and serves each file at `/runs/<run>/output/<file>`. The pages are the flow's, not the engine's: they are served sandboxed.
- `run.log` has one line for each thing the run did: a stage started, a tool called, a try rejected and why, an item ended.
  It is written as the run goes, so opening it again shows how far the run is. The pages of a run that is still going are fetched again every few seconds; nothing is streamed.

### Looking at earlier runs

The engine never looks back. A flow that wants to, asks:

```bash
agentflow history vuln-triage --item SEC-1042
```

```json
[{"run": "r_85d6", "started": "2026-11-14T22:45:10-08:00", "trial": false, "item": "SEC-1042", "ended_as": "done", "reason": "",
  "folder": "/Users/you/agentflow/data/runs/2026-11-14-r_85d6/items/SEC-1042", "files": ["fix.md", "origin.md", "report.md"],
  "output": "/Users/you/agentflow/data/runs/2026-11-14-r_85d6/output"}]
```

- Any script can call it: a source's fetch script to hand over only what is new, a stage script to end an item early.
  Inside a run the flow is known and the asking run is left out, so `agentflow history --item "$AGENTFLOW_ITEM_ID"` is enough.
- `agentflow history --run RUN --item ID --file PATH` prints one file of that item's work folder.
- For an agent, the tool plugins `history` and `history-file` wrap the same two calls. An agent sees earlier runs only
  when its `tools:` lists them.

### Tools and their health

```yaml
# plugins/tools/sbom-query/tool.yaml
description: Find a package in a saved package list. Returns version and layer.
run: ./run.py
args: [sbom_file, package]
health: ./health.sh      # exits with 0 when the tool works; its last line is shown
```

```yaml
# an MCP server: started for each agent run; healthy when it starts and lists its tools
description: Notes kept beside the item.
mcp: [python3, ./server.py]
```

The server checks every tool when it starts, every five minutes and on request. The Tools page shows the result,
and a failing tool is named on every flow whose agents use it.

## A stage done by Codex or Claude Code

A flow only names its steps. Who does a step, the agent or its reviewer, is a choice of model: an entry with `provider:` runs
in the engine's own runtime, an entry with `harness:` hands the step to a coding CLI on the same machine. The engine starts
the CLI in the item's work folder with the instructions and the prompt on its stdin, waits, and keeps what it printed.

```yaml
# agentflow.yaml
models:
  luna:       {provider: openai, name: gpt-6-luna}                             # the engine's own runtime
  luna-codex: {harness: codex, name: gpt-6-luna, api_key_env: OPENAI_API_KEY}  # Codex, handed this key
  sonnet-cc:  {harness: claude, name: sonnet}                                  # Claude Code, on its own sign-in
```

```yaml
# flow.yaml
  - id: analyse
    agent: origin-analyst
    model: luna-codex          # this stage's work is done by Codex
```

- `harness` is `codex` or `claude`. With `api_key_env` the CLI is handed that key; without it the CLI uses its own sign-in on this machine,
  the one `codex login` or `claude` set up. The engine hands the CLI your home folder's location and nothing else of its environment.
- A flow, a stage, an agent or a reviewer picks such a model, and so can `default_model`. A reviewer on a CLI works read-only
  and gives its verdict in the CLI's structured output (`--json-schema` for Claude Code, `--output-schema` for Codex).
- The trace shows the CLI's run as one span with its seconds, tokens and cost. Everything the CLI printed is kept as
  `attempts/<stage>-<try>.<cli>.jsonl` in the item's folder. Claude Code reports its cost; Codex reports tokens, which are priced from the price list.
- The agent's tools reach the CLI in the CLI's own way. A script tool is named to it as a command. A tool that is an MCP server
  is registered with it for that run: `--mcp-config` for Claude Code, `-c mcp_servers.<name>…` for Codex. The MCP servers the
  CLI has from its own configuration stay, so both kinds work side by side.
- The CLI ends with the same outcome as the engine's own runtime, `done` or `failed` with a reason, in its structured output.
- The item's `minutes_per_item` is the CLI's time limit, and cancelling the run stops it.
- `--stand-in` starts a stand-in CLI in its place, which does what the flow's `stand_in` script says: no sign-in, no cost.

The engine starts the `codex` or `claude` it finds on your `PATH`. `agentflow check` says whether each CLI a model entry names is installed, and its version.

## How an agent ends a stage, and who decides on another try

An agent has no tool for giving up and nobody to ask. It ends every stage with one outcome and a reason:

```json
{"outcome": "done", "reason": "origin.md written: app dependency, three links"}
{"outcome": "failed", "reason": "libexample-png is in neither dependency-tree.json nor image-sbom.json. Check whether the scanner flagged the right service."}
```

- `done`: the check script and the reviewer judge the work.
- `failed`: the item ends as failed with that reason. No further try is spent on it.

An item ends as done or as failed; there is no third ending.

Whether work is done again is the agent's decision, not the engine's:

| What happened | What the engine does |
|---|---|
| A tool fails or runs out of time | The error is the tool's answer. The agent reads it and goes another way, in the same try. |
| The model cannot be reached | The model client tries the request again by itself. If that does not help, the try stops. |
| A check or a reviewer does not accept the work | The reason goes back into the conversation the agent already has. It fixes the work and ends with `done`, or ends with `failed`. |
| A try stopped: the answer cut off, out of `model_calls`, a CLI error | A fresh try is told why, and decides the same way. |
| The stage reached `tries` | The item ends as failed. `tries` is only a ceiling, like a limit on turns. |

A stage script that fails still fails the item: a script has nobody to tell. An agent on Codex or Claude Code gets a
fresh run with the reasons instead of the same conversation.

### After an item failed

```bash
agentflow retry <run>
```

This works the items of a finished run that did not end as done once more; `--item ID` names one, and the run's page has
the same as a button. Each item goes on at the stage where it stopped, in its own folder, and is told what its earlier
tries said. Its count of tries starts again. Items that ended as done are left alone, and the flow's `once` stages are done again.

### What an agent is told

In this order, the general first:

1. The base instructions: how to work, what to do when something goes wrong, how to finish. The engine's are in
   `agentflow/workers/prompts/agent.md`. A flow replaces them with its own file: `instructions: BASE.md` in `flow.yaml`.
2. The agent's method, from its `AGENT.md`.
3. What the flow is for, from `FLOW.md`.
4. What the stage does, from `stages/<id>.md`.
5. The task: the item, the files in its folder, what the stage is accepted on, and the reasons of earlier tries.

An agent's context is kept in bounds: once a request reads more than `context_tokens`, the answers of its older tool calls
are cleared (the calls stay, so it can ask again). Codex and Claude Code compact their own context.

The cost of a run is the sum of what its requests reported. When a try was cut short before it reported, the figure is
still shown as a plain amount, and its tooltip says that one try is missing.

## Limits

Only the limits of a run and of the agent loop are settings. What happens inside a flow is the flow's own business.

```yaml
# agentflow.yaml
limits:                  # a flow.yaml may set its own `limits` with the same keys
  at_once: 4             # items worked on at the same time
  minutes_per_item: 10   # one item, all its stages together
  cost_per_run_usd: 2.00 # the run stops starting stages at this cost
  script_seconds: 120    # one script or one tool call
  model_calls: 20        # model requests in one agent run
  context_tokens: 100000 # past this, an agent's older tool results are cleared
  tries: 5               # the most tries of one stage; the agent doing it may end it earlier
```

The Settings page lists these seven with a mark on those left at their default. `check` reports an unknown key or a value
that is not a number above 0. The engine's own mechanics (heartbeats, how much text a page shows) are in the `internal`
section of `agentflow/defaults.yaml` so that the code holds no number; they are not settings.

## Where things are

```
data/db/agentflow.db                   the store
data/fetches/<date>-<n>-<source>/      what one fetch wrote
data/runs/<date>-<run id>/plugins/     the plugin files as the run saw them
                          items/<id>/  each item's work folder, with events.jsonl
                          output/      what the flow's scripts put out for this run, and run.log
```

## Develop

```bash
.venv/bin/python -m unittest discover -s tests
```

The tests use the scripted model. `docs/triage-flow-architecture.html` describes how the engine is built.
