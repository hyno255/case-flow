# agentflow

A small engine that takes items from a source (tickets, alerts, findings) through a flow of stages, where each stage is done
by a script or an agent and then checked. It orchestrates and routes; everything specific lives in plugin folders.
Every run is traceable step by step and ends with a report.

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
(for example `nodejs npm` for MCP servers started with `npx`) and build again.

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
# plugins/flows/vuln-triage/flow.yaml
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
and a failing tool is named in the sidebar and on every flow whose agents use it.

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
