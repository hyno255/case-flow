"""The loader: reads the four plugin folders, checks them, and copies what a run uses.

Type and name come from the folder path. Unknown keys are errors, so the formats cannot grow by accident.

    flows/<name>/flow.yaml      source and where (what it accepts), schedule (when), stages, model, limits
                 FLOW.md        what the flow is for; every agent in it is told this
                 stages/<id>.md what one stage does: the default agent's instructions, an extra note for a named agent
                 scripts/       the flow's own scripts, named by `run:` and `check:`
    agents/<name>/AGENT.md      description, tools and model on top; the method underneath
    tools/<name>/tool.yaml      description, and either run + args + health (a script) or mcp (the command of an MCP server)
    sources/<name>/source.yaml  description, run (how and where it gets items), output and fields (what it hands over)

A source and a flow meet in the source's output: the source says which file it writes and which fields every item has;
a flow says which source it accepts, optionally which of its items, and when. The engine fetches, checks and routes.
"""
import hashlib
import re
import shutil
from pathlib import Path

import yaml

from .graph import Graph, compile_flow, stage_by
from .layout import PLUGINS
from .schedule import parse_schedule
from .settings import DEFAULTS, check_numbers

SOURCE_KEYS = {"description", "run", "output", "fields"}
TOOL_KEYS = {"description", "run", "args", "health", "mcp"}
AGENT_KEYS = {"description", "tools", "model"}
FLOW_KEYS = {"source", "where", "schedule", "stages", "model", "limits"}
OUTPUT_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.json")  # a source's output: one JSON file in the fetch's folder
STAGE_KEYS = {"id", "run", "agent", "model", "check", "done_when", "review", "tries", "next", "on_fail"}
STAGES = "stages"  # the folder of a flow that holds one file per stage


def accepts(where, item: dict) -> bool:
    """Whether a flow takes an item: every field its `where` names has the value given, or one of the values listed."""
    return all(item.get(k) in (want if isinstance(want, list) else [want]) for k, want in (where or {}).items())


def installed(settings) -> "Registry":
    """The plugin files as they are on disk now, checked against the models agentflow.yaml defines."""
    return Registry(settings.plugins, settings)


class Registry:
    def __init__(self, root: Path, settings=None):
        self.root = Path(root)
        self.settings = settings  # when given, model names are checked against it, and its limits apply

    def limits_for(self, flow: dict) -> dict:
        """The limits one flow runs under: the platform's (the packaged defaults when no settings were given), with the flow's own on top."""
        if self.settings:
            return self.settings.limits_for(flow)
        own = flow.get("limits")
        return {**DEFAULTS["limits"], **(own if isinstance(own, dict) else {})}

    def names(self, kind: str) -> list[str]:
        folder = self.root / kind
        return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []

    def dir(self, kind: str, name: str) -> Path:
        return self.root / kind / name

    # ---- one loader per type ----
    def source(self, name: str) -> dict:
        return yaml.safe_load((self.dir("sources", name) / "source.yaml").read_text()) or {}

    def tool(self, name: str) -> dict:
        return yaml.safe_load((self.dir("tools", name) / "tool.yaml").read_text()) or {}

    def agent(self, name: str) -> tuple[dict, str]:
        text = (self.dir("agents", name) / "AGENT.md").read_text()
        if not text.startswith("---"):
            return {}, text.strip()
        _, front, body = text.split("---", 2)
        return yaml.safe_load(front) or {}, body.strip()

    def flow(self, name: str) -> dict:
        return yaml.safe_load((self.dir("flows", name) / "flow.yaml").read_text()) or {}

    def graph(self, name: str) -> Graph:
        f = self.flow(name)
        return compile_flow(f, self.limits_for(f))[0]

    def flow_goal(self, name: str) -> str:
        """What the flow is for: FLOW.md, which every agent in the flow is told."""
        file = self.dir("flows", name) / "FLOW.md"
        return file.read_text().strip() if file.is_file() else ""

    def stage_file(self, flow: str, stage: str) -> Path:
        return self.dir("flows", flow) / STAGES / f"{stage}.md"

    def stage_method(self, flow: str, stage: str) -> str:
        """What one stage does, from its own file."""
        file = self.stage_file(flow, stage)
        return file.read_text().strip() if file.is_file() else ""

    def flow_models(self, name: str) -> list[tuple[str, object]]:
        """(where it is written, the model picked there) for every place this flow picks a model."""
        f = self.flow(name)
        picks = [("the flow", f["model"])] if "model" in f else []
        for s in f.get(STAGES) or []:
            if "model" in s:
                picks.append((f"stage {s.get('id')}", s["model"]))
            for key in ("agent", "review"):
                if key in s and (self.dir("agents", s[key]) / "AGENT.md").is_file() and "model" in self.agent(s[key])[0]:
                    picks.append((f"agent {s[key]}", self.agent(s[key])[0]["model"]))
        return picks

    def models_used(self, flow: str) -> list:
        """Every model an agent run of this flow would use; None stands for the default."""
        used = []
        for s in self.flow(flow).get(STAGES) or []:
            if "run" not in s:
                used.append(self.model_for(flow, s, s.get("agent")))
            if "review" in s or "done_when" in s:
                used.append(self.model_for(flow, {}, s.get("review")))
        return [m for i, m in enumerate(used) if m not in used[:i]]

    def model_for(self, flow: str, stage: dict, agent: str | None = None):
        """Which model one agent run uses: the stage's, else the agent's own, else the flow's, else the default (None)."""
        if "model" in stage:
            return stage["model"]
        if agent and "model" in self.agent(agent)[0]:
            return self.agent(agent)[0]["model"]
        return self.flow(flow).get("model")

    def tool_users(self) -> dict[str, list[str]]:
        """tool -> the agents that list it."""
        users: dict[str, list[str]] = {}
        for a in self.names("agents"):
            for t in (self.agent(a)[0].get("tools") or []) if (self.dir("agents", a) / "AGENT.md").is_file() else []:
                users.setdefault(t, []).append(a)
        return users

    def flows_for_source(self, source: str) -> list[str]:
        return [f for f in self.names("flows") if self.flow(f).get("source") == source]

    # ---- checks ----
    def check_source(self, name: str) -> list[str]:
        d = self.dir("sources", name)
        if not (d / "source.yaml").is_file():
            return [f"source '{name}' not found"]
        s = self.source(name)
        errs = [f"unknown key '{k}'" for k in s if k not in SOURCE_KEYS]
        errs += [f"missing '{k}'" for k in ("description", "run", "output") if k not in s]
        if "run" in s and not (d / str(s["run"])).is_file():
            errs.append(f"script {s['run']} not found")
        if "output" in s and not OUTPUT_FILE.fullmatch(str(s["output"])):
            errs.append("'output' names the JSON file the script writes into its output folder, such as tickets.json")
        if not (isinstance(s.get("fields", []), list) and all(isinstance(f, str) for f in s.get("fields", []))):
            errs.append("'fields' is a list of the field names every item has")
        return errs

    def check_tool(self, name: str) -> list[str]:
        d = self.dir("tools", name)
        if not (d / "tool.yaml").is_file():
            return [f"tool '{name}' not found"]
        t = self.tool(name)
        errs = [f"unknown key '{k}'" for k in t if k not in TOOL_KEYS]
        if "description" not in t:
            errs.append("missing 'description'")
        if ("run" in t) == ("mcp" in t):
            errs.append("give one of 'run' (a script) and 'mcp' (the command that starts an MCP server)")
        elif "run" in t and not (d / t["run"]).is_file():
            errs.append(f"script {t['run']} not found")
        elif "run" in t and "health" not in t:
            errs.append("missing 'health': a script that exits with 0 when the tool works")
        elif "run" in t and not (d / str(t["health"])).is_file():
            errs.append(f"health script {t['health']} not found")
        elif "mcp" in t and not (isinstance(t["mcp"], list) and t["mcp"] and all(isinstance(a, str) for a in t["mcp"])):
            errs.append("'mcp' must be a command as a list, such as [python3, ./server.py]")
        elif "mcp" in t and ("args" in t or "health" in t):
            errs.append("'args' and 'health' belong to a script; an MCP server names its own tools, and it is healthy when it starts and lists them")
        return errs

    def check_agent(self, name: str) -> list[str]:
        if not (self.dir("agents", name) / "AGENT.md").is_file():
            return [f"agent '{name}' not found"]
        meta, body = self.agent(name)
        errs = [f"unknown key '{k}'" for k in meta if k not in AGENT_KEYS]
        if "description" not in meta:
            errs.append("missing 'description'")
        if not body:
            errs.append("AGENT.md has no method")
        for t in meta.get("tools", []):
            errs += [f"tool {t}: {e}" for e in self.check_tool(t)]
        return errs

    def check_flow(self, name: str) -> list[str]:
        d = self.dir("flows", name)
        if not (d / "flow.yaml").is_file():
            return [f"flow '{name}' not found"]
        f = self.flow(name)
        errs = [f"unknown key '{k}'" for k in f if k not in FLOW_KEYS]
        errs += [f"missing '{k}'" for k in ("source", STAGES) if k not in f]
        if "source" in f:
            errs += [f"source {f['source']}: {e}" for e in self.check_source(f["source"])]
        where = f.get("where", {})
        if not isinstance(where, dict):
            errs.append("'where' is a mapping of field: value, or field: [values], that an item must match")
        elif not errs:  # the source is readable: a field the flow asks about must be one the source says it gives
            given = self.source(f["source"]).get("fields")
            errs += [f"where: the source {f['source']} lists no field '{k}'" for k in where if given is not None and k not in [*given, "id"]]
        if "schedule" in f:
            try:
                parse_schedule(str(f["schedule"]))
            except ValueError as e:
                errs.append(str(e))
        errs += check_numbers("limits", f.get("limits") or {})
        if not (d / "FLOW.md").is_file():
            errs.append("FLOW.md not found")
        graph, graph_errs = compile_flow(f, self.limits_for(f))  # ids, next, on_fail, tries: the flow as a graph
        errs += graph_errs
        for sid, s in graph.stages.items():
            errs += [f"stage {sid}: unknown key '{k}'" for k in s if k not in STAGE_KEYS]
            if "run" in s and "agent" in s:
                errs.append(f"stage {sid}: 'run' and 'agent' cannot both be set")
            if "run" in s and "model" in s:
                errs.append(f"stage {sid}: a script uses no model")
            if s.get("review") and s.get("review") == s.get("agent"):
                errs.append(f"stage {sid}: the reviewer cannot be the agent that does the stage")
            for key in ("run", "check"):
                if key in s and not (d / s[key]).is_file():
                    errs.append(f"stage {sid}: {key} script {s[key]} not found")
            for key in ("agent", "review"):
                if key in s:
                    errs += [f"stage {sid}: {key} {s[key]}: {e}" for e in self.check_agent(s[key])]
            if stage_by(s) == "default agent" and not self.stage_method(name, sid):
                errs.append(f"stage {sid}: the default agent does this stage, but {STAGES}/{sid}.md is missing or empty")
        if self.settings is not None:
            for where, spec in self.flow_models(name):
                errs += self.settings.check_model(spec, where)
        if self.settings is not None and not errs:  # the flow reads well: what a stage handed to a CLI cannot have
            on_cli = self.settings.harness_of
            if "model" in f and on_cli(f["model"]):
                errs.append(f"model: the flow's model also runs its reviewers, which stay in the engine; name the {on_cli(f['model'])} model on a stage or an agent")
            for sid, s in graph.stages.items():
                if "review" in s and on_cli(self.agent(s["review"])[0].get("model", f.get("model"))):
                    errs.append(f"stage {sid}: review {s['review']}: its model runs on a CLI; a reviewer runs in the engine")
                harness = None if "run" in s else on_cli(self.model_for(name, s, s.get("agent")))
                for t in (self.agent(s["agent"])[0].get("tools") or []) if harness and "agent" in s else []:
                    if "mcp" in self.tool(t):
                        errs.append(f"stage {sid}: its work goes to the {harness} CLI, which cannot use the tool {t}: it is an MCP server, not a command")
                    elif self.settings.granted(f"tools/{t}"):
                        errs.append(f"stage {sid}: its work goes to the {harness} CLI, which is not given the secrets the tool {t} was granted")
        return errs

    def check_all(self) -> list[tuple[str, str, list[str], str]]:
        """(type, name, errors, one-line summary) for everything in the plugins folder."""
        rows = []
        for n in self.names("sources"):
            s = self.source(n) if (self.dir("sources", n) / "source.yaml").is_file() else {}
            rows.append(("sources", n, self.check_source(n), f"writes {s.get('output', 'nothing')}"))
        users = self.tool_users()
        for n in self.names("tools"):
            rows.append(("tools", n, self.check_tool(n), "used by " + ", ".join(users[n]) if n in users else "not used"))
        for n in self.names("agents"):
            errs = self.check_agent(n)
            rows.append(("agents", n, errs, f"{len(self.agent(n)[0].get('tools') or [])} tools" if not errs else ""))
        for n in self.names("flows"):
            errs = self.check_flow(n)
            f = self.flow(n) if (self.dir("flows", n) / "flow.yaml").is_file() else {}
            stages = f.get(STAGES) or []
            kinds = [sum("run" in s for s in stages), sum("agent" in s for s in stages)]
            kinds.append(len(stages) - sum(kinds))
            summary = f"{len(stages)} stages: " + ", ".join(f"{k} {label}" for k, label in zip(kinds, ("script", "agent", "default agent")) if k)
            rows.append(("flows", n, errs, summary))
        return rows

    # ---- snapshot ----
    def snapshot(self, flow: str, into: Path) -> str:
        """Copy the files this flow can use into a run's folder and return their content hash.
        A run executes from the copy, so editing a plugin mid-run changes nothing for that run."""
        f = self.flow(flow)
        parts = [("flows", flow), ("sources", f["source"])]
        agents = {s[k] for s in f[STAGES] for k in ("agent", "review") if k in s}
        tools = {t for a in agents for t in (self.agent(a)[0].get("tools") or [])}
        parts += [("agents", a) for a in sorted(agents)] + [("tools", t) for t in sorted(tools)]
        h = hashlib.sha256()
        for kind, name in parts:
            for p in sorted(self.dir(kind, name).rglob("*")):
                if p.is_file():
                    h.update(str(p.relative_to(self.root)).encode() + b"\0" + p.read_bytes() + b"\0")
        for kind, name in parts:
            shutil.copytree(self.dir(kind, name), into / kind / name)
        return h.hexdigest()[:12]


def run_plugins(settings, store, run) -> Registry:
    """The plugin files a run uses: its own copy, made when it started. The limits are the platform's as they are now."""
    return Registry(store.path(run) / PLUGINS, settings)
