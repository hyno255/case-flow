# agentflow

A small engine that takes items from a source (tickets, alerts, findings) through a flow of stages, where each stage is done
by a script or an agent and then checked. It orchestrates and routes; everything specific lives in plugin folders.
Every run can be traced down to each model call. What a run puts out, a report for example, is written by the flow's own script.

The engine runs in one Docker image. Nothing of yours is inside it: settings, plugins, data and any other folder are attached when it starts.

## Run it

```bash
docker compose up --build
```

The web UI is on http://localhost:8321. Any command of the CLI runs the same way:

```bash
docker compose run --rm agentflow check
```

```bash
docker compose run --rm agentflow run vuln-triage --stand-in
```

`--stand-in` needs no key and costs nothing: the script the flow names as `stand_in` in its `flow.yaml` answers in the model's place
(the example's is `plugins/flows/vuln-triage/scripts/stand_in.py`, which also says what it is asked and what it prints). For a real model, put its key in `.env`
(for the example settings: `OPENAI_API_KEY=...`); `compose.yaml` passes that file to the container.

| Command | What it does |
|---|---|
| `check` | checks every plugin file and the settings, and runs every tool's health check |
| `run <flow>` | fetches the flow's source now and runs the flow over every item it accepts |
| `try <flow> [--item ID]` | one item as a trial; the flow's scripts are told it is one |
| `schedule [on\|off <flow>]` | shows the flows' schedules, or switches one |
| `show [run] [item]` | recent runs, or what one run or item did and cost |
| `resume <run>`, `cancel <run>` | what can be done with a run afterwards |
| `history [flow] [--item ID]` | for a flow's own scripts: how items ended in earlier runs and where their files are, as JSON |
| `serve` | the web UI, the API, the schedules and the health checks |

## What you attach

`compose.yaml` mounts these into the container:

| On your machine | In the container | What it is |
|---|---|---|
| `agentflow.yaml` | `/work/agentflow.yaml` (read-only) | models and limits |
| `plugins/` | `/work/plugins` (read-only) | flows, agents, tools, sources |
| `data/` | `/work/data` | the database, one folder per fetch, one folder per run |
| any other folder | wherever you mount it | what your scripts need to read |
| `.env` | environment | model keys |

A program that a script, a tool or an MCP server calls must be in the image: add it to the `apt-get install` line of the `Dockerfile`
and build again. `nodejs` and `npm` are already there, with the two coding CLIs a stage's work can be handed to (see below).

The container is the boundary between scripts and your machine. Inside it a script is a plain child process: it can read
whatever is mounted and reach the network. It sees what the engine tells it and none of the engine's keys.
There is no sign-in, so the port is published on `127.0.0.1` only.

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

### What a run puts out

Every run has an `output/` folder, and the flow fills it. The engine lists and serves it, and keeps one file of its own there, `run.log`:

```yaml
# a flow.yaml
finish: scripts/report.py   # optional: runs once, when every item of the run has ended
```

- The `finish` script works in the run's output folder. On its input it gets the run and how each item ended, as JSON:
  `{"run", "flow", "trial", "started", "items": [{"id", "ended_as", "reason", "folder", "item"}]}`.
- Every script of a run is told `AGENTFLOW_OUTPUT`, the same folder, so a stage script can write there too.
- The run's page lists the folder under Output, and serves each file at `/runs/<run>/output/<file>`. The pages are the flow's, not the engine's: they are served sandboxed.
- A `finish` script that fails does not fail the run; the run's page says why. A cancelled run skips it.
- `run.log` has one line for each thing the run did: a stage started, a tool called, a try rejected and why, an item ended.
  It is written as the run goes, so opening it again shows how far the run is. The pages of a run that is still going are fetched again every few seconds; nothing is streamed.

The example flow's `scripts/report.py` writes an index and one page per ticket this way.

### Looking at earlier runs

The engine never looks back. A flow that wants to, asks:

```bash
agentflow history vuln-triage --item SEC-1042
```

```json
[{"run": "r_85d6", "started": "2026-11-14T22:45:10-08:00", "trial": false, "item": "SEC-1042", "ended_as": "done", "reason": "",
  "folder": "/work/data/runs/2026-11-14-r_85d6/items/SEC-1042", "files": ["fix.md", "origin.md", "report.md"],
  "output": "/work/data/runs/2026-11-14-r_85d6/output"}]
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

- `harness` is `codex` or `claude`. With `api_key_env` the CLI is handed that key; without it the CLI uses its own sign-in,
  which a container does not have.
- A flow, a stage, an agent or a reviewer picks such a model, and so can `default_model`. A reviewer on a CLI works read-only
  and gives its verdict in the CLI's structured output (`--json-schema` for Claude Code, `--output-schema` for Codex).
- The trace shows the CLI's run as one span with its seconds, tokens and cost. Everything the CLI printed is kept as
  `attempts/<stage>-<try>.<cli>.jsonl` in the item's folder. Claude Code reports its cost; Codex reports tokens, which are priced from the price list.
- The agent's tools reach the CLI in the CLI's own way. A script tool is named to it as a command. A tool that is an MCP server
  is registered with it for that run: `--mcp-config` for Claude Code, `-c mcp_servers.<name>…` for Codex. The MCP servers the
  CLI has from its own configuration stay, so both kinds work side by side.
- To set an item aside for a person, the CLI writes `needs-person.md`.
- The item's `minutes_per_item` is the CLI's time limit, and cancelling the run stops it.
- `--stand-in` starts a stand-in CLI in its place, which does what the flow's `stand_in` script says: no sign-in, no cost.

`agentflow check` says whether each CLI a model entry names is installed. The image has Codex and Claude Code at fixed versions
(see the `Dockerfile`).

## When a try goes wrong

A try that goes wrong is not the end of the item. The agent, or the next try, is told what happened and acts on it:

| What happened | What the engine does |
|---|---|
| A tool fails or runs out of time | The error is the tool's answer. The agent reads it and goes another way, in the same try. |
| The model cannot be reached | The model client tries the request again by itself. If that does not help, the try stops. |
| The answer is cut off at the output limit, the run is out of `model_calls`, a CLI exits with an error | The try stops. |
| A try stopped | It counts as a rejected try. The next try gets the reason as feedback, the same way as a reviewer's. |
| The stage is out of `tries` | The item is set aside for a person, with the last reason. |

A stage script that fails still fails the item: a script has nobody to tell.

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
  tries: 2               # a stage that names no `tries`
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
python3 -m venv .venv && .venv/bin/pip install -e .
```

```bash
.venv/bin/python -m unittest discover -s tests
```

The tests use the scripted model. `docs/triage-flow-architecture.html` describes how the engine is built.
