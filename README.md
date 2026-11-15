# agentflow

A small engine that takes items from a source (tickets, alerts, findings) through a flow of stages, where each stage is done
by a script or an agent and then checked. It orchestrates and routes; everything specific lives in plugin folders.
Every run can be traced down to each model call and ends with a report.

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

`--stand-in` uses a scripted model that needs no key and costs nothing. For a real model, put its key in `.env`
(for the example settings: `OPENAI_API_KEY=...`); `compose.yaml` passes that file to the container.

| Command | What it does |
|---|---|
| `check` | checks every plugin file and the settings, and runs every tool's health check |
| `run <flow> [--force]` | fetches the flow's source now and runs the flow |
| `try <flow> [--item ID]` | one item as a trial; it does not count as done |
| `schedule [on\|off <flow>]` | shows the flows' schedules, or switches one |
| `show [run] [item]` | recent runs, or what one run or item did and cost |
| `resume <run>`, `retry <run> <item>`, `cancel <run>` | what can be done with a run afterwards |
| `serve` | the web UI, the API, the schedules and the health checks |

## What you attach

`compose.yaml` mounts these into the container:

| On your machine | In the container | What it is |
|---|---|---|
| `agentflow.yaml` | `/work/agentflow.yaml` (read-only) | models, limits, and which secrets each plugin's scripts may see |
| `plugins/` | `/work/plugins` (read-only) | flows, agents, tools, sources |
| `data/` | `/work/data` | the database, one folder per fetch, one folder per run |
| any other folder | wherever you mount it | what your scripts need to read |
| `.env` | environment | model keys and the secrets named under `grants:` |

A program that a script, a tool or an MCP server calls must be in the image: add it to the `apt-get install` line of the `Dockerfile`
and build again. `nodejs` and `npm` are already there, with the two coding CLIs a stage's work can be handed to (see below).

The container is the boundary between scripts and your machine. Inside it a script is a plain child process: it can read
whatever is mounted and reach the network. It is given only the secrets its plugin was granted.
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

The engine's own harness runs a stage's agent in its process. A model entry with `harness:` hands the work of a stage to a
coding CLI on the same machine instead. The engine starts the CLI in the item's work folder with the instructions and the
prompt on its stdin, waits, and keeps what it printed. The check script and the reviewer then judge the files, as for any other try.

```yaml
# agentflow.yaml
models:
  luna:       {provider: openai, name: gpt-6-luna}                             # the engine's own harness
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
- A stage or an agent picks such a model. A flow's own `model:` and a reviewer's cannot, because reviewers run in the engine.
- The trace shows the CLI's run as one span with its seconds, tokens and cost. Everything the CLI printed is kept as
  `attempts/<stage>-<try>.<cli>.jsonl` in the item's folder. Claude Code reports its cost; Codex reports tokens, which are priced from the price list.
- The agent's script tools are named to the CLI as commands. A tool that is an MCP server, or one that was granted secrets,
  cannot be used on such a stage, and `check` says so.
- To set an item aside for a person, the CLI writes `needs-person.md`.
- The item's `minutes_per_item` is the CLI's time limit, and cancelling the run stops it.
- `--stand-in` starts a scripted CLI in its place: no sign-in, no cost.

`agentflow check` says whether each CLI a model entry names is installed. The image has Codex and Claude Code at fixed versions
(see the `Dockerfile`).

## Limits and timings

The code holds no limit of its own. Every one is a key in `agentflow.yaml`, and the Settings page lists them all
with a mark on those left at their default (`agentflow/defaults.yaml`).

```yaml
# agentflow.yaml
limits:                  # what one run may use; a flow.yaml may set its own `limits` with the same keys
  at_once: 4
  minutes_per_item: 10
  cost_per_run_usd: 2.00
  script_seconds: 120    # one script: a stage's, a check, a tool, a source's fetch
  model_calls: 20        # one agent run
  tries: 2               # a stage that names no `tries`
  max_tries: 5
  read_chars: 20000      # of a file given to an agent
  tool_chars: 8000       # of a tool's answer given to an agent
  reason_chars: 2000     # of a failed check's output, told to the next try
  error_chars: 500
  event_chars: 1500
engine:                  # the engine's own timings, in seconds
  health_every_seconds: 300
  stale_after_seconds: 60
show:                    # how much the pages and the command line show
  rows: 50
```

| Section | Keys |
|---|---|
| `limits` | as above |
| `engine` | `health_seconds`, `health_every_seconds`, `health_stale_seconds`, `stop_grace_seconds`, `poll_seconds`, `heartbeat_seconds`, `stale_after_seconds`, `schedule_every_seconds`, `watch_every_seconds`, `follow_seconds`, `keepalive_seconds`, `event_batch` |
| `show` | `rows`, `line_chars`, `label_chars`, `fact_chars`, `block_chars`, `prompt_chars`, `file_chars`, `raw_chars` |

`check` reports an unknown key or a value that is not a number above 0.

## Where things are

```
data/db/agentflow.db                   the store
data/fetches/<date>-<n>-<source>/      what one fetch wrote
data/runs/<date>-<run id>/plugins/     the plugin files as the run saw them
                          items/<id>/  each item's work folder, with events.jsonl
                          report/      the run's output: index.html and one page per item
```

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

```bash
.venv/bin/python -m unittest discover -s tests
```

The tests use the scripted model. `docs/triage-flow-architecture.html` describes how the engine is built.
