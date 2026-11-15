"""What is installed, before anything runs: flows, agents, tools, sources, and the platform's settings.

Read-only. These pages show the plugin files as they are on disk now; a run page shows the copy that run used.
A flow's Design tab is drawn from flow.yaml itself: every key as it is written, then what the engine fills in
where nothing is written, each marked with where it comes from.
"""
import json
import os
from pathlib import Path
from urllib.parse import quote

from ..graph import compile_flow, stage_by, stage_tries, validated
from ..health import EVERY
from ..layout import confined
from ..plugins import STAGES, installed
from ..render import md_to_html
from ..settings import MODEL_DEFAULTS
from . import draw, show, views

KINDS = ("flows", "agents", "tools", "sources")
TABS = {"flows": ("design", "files", "runs"), "agents": ("config", "method", "files"), "tools": ("config", "files"), "sources": ("config", "fetches", "files")}
STALE = 3 * EVERY  # seconds after which a health answer is too old to trust: the server that made it has stopped
MAIN = {"flows": "flow.yaml", "agents": "AGENT.md", "tools": "tool.yaml", "sources": "source.yaml"}  # the file a Files tab opens with
BUILT_IN = ("list_files", "read_file", "write_file", "ask_for_person")  # handed to every agent by the engine; a reviewer gets no write_file


def _safe(check, name: str) -> list[str]:
    try:
        return check(name)
    except Exception as e:  # a file that cannot be parsed is an error to show, not a page that fails
        return [f"{type(e).__name__}: {e}"]


def _load(fn, *args, default):
    """What a loader returns, or `default` when the file cannot be read or holds something else than expected."""
    try:
        got = fn(*args)
    except Exception:
        return default
    return got if isinstance(got, type(default)) or default is None else default


def row(k: str, v="", href: str = "", note: str = "", mark: str = "", links: list | None = None) -> dict:
    """One line of a key/value table. `mark` says where a value comes from when it is not written in the file shown."""
    return {"k": k, "v": v if isinstance(v, str) else json.dumps(v), "href": href, "note": note, "mark": mark, "links": links or []}


def links(names, prefix: str) -> list[dict]:
    """Names as links to their pages, or as plain names when `prefix` is empty."""
    return [{"text": str(n), "href": f"{prefix}{n}" if prefix else ""} for n in names or []]


def model_cell(settings, spec) -> dict:
    """{name, label} for a model as a plugin picks it: a name from agentflow.yaml, a mapping, or nothing for the default."""
    name = spec.get("name", "") if isinstance(spec, dict) else settings.default_model if spec is None else spec
    try:
        label = settings.model_label(spec)
    except TypeError:  # not something a model can be named by
        label = ""
    return {"name": str(name or ""), "label": label}


def grants_view(settings, key: str) -> list[dict]:
    """The secrets one plugin's scripts were granted. Names only: the values never reach a page."""
    return [{"name": n, "set": n in os.environ} for n in settings.granted(key)]


def files_tab(settings, kind: str, name: str, file: str) -> dict:
    folder = settings.plugins / kind / name
    tree = show.file_tree(folder, file)
    return {"tree": tree, "file": show.file_preview(folder, file or show.first_file(tree, (MAIN[kind],)), f"/plugins/{kind}/{name}/files")}


def file_link(kind: str, name: str, folder: Path, rel) -> str:
    """The Files tab with one file selected, or nothing when the folder has no such file."""
    try:
        path = Path(str(rel))
        return f"/{kind}/{name}/files?file={quote(str(path))}" if confined(folder, str(path)).is_file() else ""
    except ValueError:
        return ""


def uses(live) -> tuple[dict, dict]:
    """(agent -> [{flow, stage, as}], tool -> [agent]). The engine's own default agent and checker are not plugins and have no page."""
    agents: dict[str, list[dict]] = {}
    tools: dict[str, list[str]] = {}
    for f in live.names("flows"):
        for s in _load(live.flow, f, default={}).get(STAGES) or []:
            for key, role in (("agent", "executor"), ("review", "reviewer")):
                if isinstance(s, dict) and key in s:
                    agents.setdefault(str(s[key]), []).append({"flow": f, "stage": s.get("id"), "as": role})
    for a in live.names("agents"):
        for t in _load(live.agent, a, default=({}, ""))[0].get("tools") or []:
            tools.setdefault(t, []).append(a)
    return agents, tools


# ---------- the lists ----------

def schedule_cell(store, flow: str, f: dict) -> dict:
    """A flow's schedule as it is written, and whether it is switched on."""
    sched = store.one("SELECT * FROM schedules WHERE flow = ?", flow)
    on = bool(f.get("schedule") and sched and sched["enabled"])
    return {"spec": str(f.get("schedule") or ""), "enabled": on, "next_due": show.when(sched["next_due"]) if on else ""}


def flow_rows(settings, store, errors: dict | None = None) -> list[dict]:
    live, rows = installed(settings), []
    for n in live.names("flows"):
        f = _load(live.flow, n, default={})
        last = store.one("SELECT * FROM runs WHERE flow = ? ORDER BY started DESC, rowid DESC LIMIT 1", n)
        rows.append({"name": n, "source": str(f.get("source") or ""), "schedule": schedule_cell(store, n, f), "stages": len(f.get(STAGES) or []),
                     "model": model_cell(settings, f.get("model")), "model_default": "model" not in f,
                     "errors": _safe(live.check_flow, n), "error": (errors or {}).get(f"flows/{n}", ""),
                     "last_run": views.run_row(store, last) if last else None})
    return rows


def agent_rows(settings) -> list[dict]:
    live = installed(settings)
    used = uses(live)[0]
    rows = []
    for n in live.names("agents"):
        meta = _load(live.agent, n, default=({}, ""))[0]
        rows.append({"name": n, "description": meta.get("description", ""), "model": model_cell(settings, meta.get("model")), "model_default": "model" not in meta,
                     "tools": meta.get("tools") or [], "used": used.get(n, []), "errors": _safe(live.check_agent, n)})
    return rows


def health_cell(store, name: str) -> dict:
    """The last answer of a tool's health check: {state: ok | failing | not checked, detail, at}."""
    h = store.one("SELECT * FROM health WHERE tool = ?", name)
    if h is None:
        return {"state": "not checked", "pill": "wait", "detail": "checked when the server starts, every few minutes, and when asked", "at": ""}
    old = show.seconds_between(h["checked"], None) > STALE
    return {"state": "ok" if h["ok"] else "failing", "pill": "done" if h["ok"] else "bad", "detail": h["detail"] or "",
            "at": show.when(h["checked"]), "old": old}


def tool_rows(settings, store) -> list[dict]:
    live = installed(settings)
    used = uses(live)[1]
    rows = []
    for n in live.names("tools"):
        t = _load(live.tool, n, default={})
        rows.append({"name": n, "description": t.get("description", ""), "how": "MCP server" if "mcp" in t else "script", "args": t.get("args") or [],
                     "used": used.get(n, []), "errors": _safe(live.check_tool, n), "health": health_cell(store, n)})
    return rows


def last_fetch(store, source: str) -> dict | None:
    last = store.one("SELECT * FROM fetches WHERE source = ? ORDER BY id DESC LIMIT 1", source)
    return {"at": show.when(last["started"]), "outcome": last["outcome"] or "running", "detail": last["detail"] or "", "count": last["item_count"]} if last else None


def source_row(settings, store, live, name: str) -> dict:
    s = _load(live.source, name, default={})
    return {"name": name, "description": str(s.get("description") or ""), "output": str(s.get("output") or ""),
            "flows": _load(live.flows_for_source, name, default=[]), "errors": _safe(live.check_source, name), "last_fetch": last_fetch(store, name)}


def source_rows(settings, store) -> list[dict]:
    live = installed(settings)
    return [source_row(settings, store, live, n) for n in live.names("sources")]


def plugins_view(settings, store, errors: dict) -> dict:
    """Everything installed, for the API."""
    return {"flows": flow_rows(settings, store), "agents": agent_rows(settings), "tools": tool_rows(settings, store), "sources": source_rows(settings, store),
            "folder": str(settings.plugins)}


# ---------- one flow ----------

def failing_tools(store, live, f: dict) -> list[dict]:
    """The tools this flow's agents use whose last health check failed."""
    agents = {s[k] for s in f.get(STAGES) or [] if isinstance(s, dict) for k in ("agent", "review") if k in s}
    tools = sorted({t for a in agents for t in _load(live.agent, a, default=({}, ""))[0].get("tools") or []})
    return [{"name": t, "detail": h["detail"]} for t in tools if (h := health_cell(store, t))["state"] == "failing"]


def flow_design(settings, store, live, name: str, f: dict) -> dict:
    """The Design tab: the graph, the flow's own keys, and one block per stage."""
    g = compile_flow(f)[0]
    folder = live.dir("flows", name)
    agents = live.names("agents")

    def model_row(key: str, spec, mark: str = "") -> dict:
        m = model_cell(settings, spec)
        return row(key, m["name"], note=m["label"], mark=mark)

    def inherited(agent) -> str:
        """Where a stage's model comes from when the stage does not pick one."""
        if agent and "model" in _load(live.agent, agent, default=({}, ""))[0]:
            return "from agent"
        return "from flow" if "model" in f else "platform default"

    config = []
    for k, val in f.items():  # every key as written; limits are spelled out below
        if k == "source":
            config.append(row(k, val, f"/sources/{val}" if val in live.names("sources") else ""))
        elif k == "schedule":
            continue  # shown with its switch, above the keys
        elif k == "model":
            config.append(model_row(k, val))
        elif k not in (STAGES, "limits") or not isinstance(val, (dict, list)):
            config.append(row(k, val))
    if "model" not in f:
        config.append(model_row("model", None, "platform default"))
    own = f.get("limits") if isinstance(f.get("limits"), dict) else {}
    for k, val in {**settings.limits, **own}.items():
        config.append(row(f"limits.{k}", val, mark="" if k in own else "platform default"))

    stages = []
    for sid, s in g.stages.items():
        rows = []
        for k, val in s.items():
            if k in ("run", "check"):
                rows.append(row(k, val, file_link("flows", name, folder, val)))
            elif k in ("agent", "review"):
                rows.append(row(k, val, f"/agents/{val}" if val in agents else ""))
            elif k == "model":
                rows.append(model_row(k, val))
            elif k in ("next", "on_fail"):
                rows.append(row(k, val, f"#stage-{val}" if val in g.stages else ""))
            elif k != "id":
                rows.append(row(k, val))
        agent = s.get("agent")
        if "run" not in s:  # an agent does the stage: which one, with which tools, on which model
            if agent:
                rows.append(row("tools", links=links(_load(live.agent, agent, default=({}, ""))[0].get("tools"), "/tools/"), mark="from agent"))
            else:
                rows += [row("agent", "default agent", mark="default"), row("tools", links=links(BUILT_IN, ""), mark="built in")]
            if "model" not in s:
                rows.append(model_row("model", _load(live.model_for, name, s, agent, default=None), inherited(agent)))
        if "done_when" in s or "review" in s:
            if "review" not in s:
                rows.append(row("review", "checker", mark="default"))
            rows.append(model_row("review model", _load(live.model_for, name, {}, s.get("review"), default=None), inherited(s.get("review"))))
        if validated(s):
            if "tries" not in s:
                rows.append(row("tries", stage_tries(s), mark="default"))
            if "on_fail" not in s:
                rows.append(row("on_fail", sid, f"#stage-{sid}", mark="default"))
        if "next" not in s:
            rows.append(row("next", g.after(sid), f"#stage-{g.after(sid)}" if g.after(sid) in g.stages else "", mark="default"))
        md = _load(live.stage_method, name, sid, default="")
        stages.append({"id": sid, "by": "agent" if agent else stage_by(s), "rows": rows, "md": f"{STAGES}/{sid}.md" if md else "",
                       "md_href": file_link("flows", name, folder, f"{STAGES}/{sid}.md"), "md_html": md_to_html(md) if md else ""})

    goal = _load(live.flow_goal, name, default="")
    return {"graph": draw.graph(f, _load(live.source, str(f.get("source") or ""), default={})), "config": config, "stages": stages,
            "failing_tools": failing_tools(store, live, f),
            "goal_html": md_to_html(goal) if goal else "", "goal_href": file_link("flows", name, folder, "FLOW.md"),
            "yaml_href": file_link("flows", name, folder, "flow.yaml")}


def flow_view(settings, store, name: str, tab: str = "design", file: str = "", errors: dict | None = None) -> dict | None:
    live = installed(settings)
    if not (live.dir("flows", name) / "flow.yaml").is_file() or tab not in TABS["flows"]:
        return None
    f = _load(live.flow, name, default={})
    v = {"kind": "flows", "name": name, "tab": tab, "tabs": TABS["flows"], "errors": _safe(live.check_flow, name), "folder": str(live.dir("flows", name)),
         "source": str(f.get("source") or "") if f.get("source") in live.names("sources") else "",
         "error": (errors or {}).get(f"flows/{name}", ""), "seq": views.max_seq(store), "schedule": schedule_cell(store, name, f)}
    v["can_run"] = bool(v["source"]) and not v["errors"]
    if tab == "design":
        v.update(flow_design(settings, store, live, name, f))
    elif tab == "files":
        v.update(files_tab(settings, "flows", name, file))
    else:
        v["runs"] = views.run_rows(store, flow=name)
    return v


# ---------- one agent, tool or source ----------

def agent_view(settings, name: str, tab: str = "config", file: str = "") -> dict | None:
    live = installed(settings)
    folder = live.dir("agents", name)
    if not (folder / "AGENT.md").is_file() or tab not in TABS["agents"]:
        return None
    meta, body = _load(live.agent, name, default=({}, ""))
    v = {"kind": "agents", "name": name, "tab": tab, "tabs": TABS["agents"], "errors": _safe(live.check_agent, name), "folder": str(folder)}
    if tab == "config":
        m = model_cell(settings, meta.get("model"))
        v["config"] = [row("description", meta.get("description", "")),
                       row("model", m["name"], note=m["label"], mark="" if "model" in meta else "flow or platform default"),
                       row("tools", links=links(meta.get("tools"), "/tools/")),
                       row("built-in tools", links=links(BUILT_IN, ""), note="write_file only as executor", mark="built in"),
                       *[row(k, val) for k, val in meta.items() if k not in ("description", "model", "tools")]]
        v["used"] = uses(live)[0].get(name, [])
    elif tab == "method":
        v["method_html"], v["method_href"] = md_to_html(body), file_link("agents", name, folder, "AGENT.md")
    else:
        v.update(files_tab(settings, "agents", name, file))
    return v


def tool_view(settings, store, name: str, tab: str = "config", file: str = "") -> dict | None:
    live = installed(settings)
    folder = live.dir("tools", name)
    if not (folder / "tool.yaml").is_file() or tab not in TABS["tools"]:
        return None
    t = _load(live.tool, name, default={})
    v = {"kind": "tools", "name": name, "tab": tab, "tabs": TABS["tools"], "errors": _safe(live.check_tool, name), "folder": str(folder),
         "health": health_cell(store, name)}
    if tab == "config":
        by_agent, by_tool = uses(live)
        agents = by_tool.get(name, [])
        how = [row("mcp", " ".join(map(str, t["mcp"])) if isinstance(t["mcp"], list) else str(t["mcp"]), note="an MCP server, started for each agent run")] if "mcp" in t else \
              [row("args", links=links(t.get("args"), "")), row("run", t.get("run", ""), file_link("tools", name, folder, t.get("run", ""))),
               row("health", t.get("health", ""), file_link("tools", name, folder, t.get("health", "")))]
        v["config"] = [row("description", t.get("description", "")), *how,
                       *[row(k, val) for k, val in t.items() if k not in ("description", "args", "run", "health", "mcp")]]
        v["used"] = [{"agent": a, "stages": by_agent.get(a, [])} for a in agents]
        v["grants"] = grants_view(settings, f"tools/{name}")
    else:
        v.update(files_tab(settings, "tools", name, file))
    return v


def source_view(settings, store, name: str, tab: str = "config", file: str = "") -> dict | None:
    live = installed(settings)
    folder = live.dir("sources", name)
    if not (folder / "source.yaml").is_file() or tab not in TABS["sources"]:
        return None
    s = _load(live.source, name, default={})
    v = {"kind": "sources", "tab": tab, "tabs": TABS["sources"], "folder": str(folder), **source_row(settings, store, live, name)}
    if tab == "config":
        v["config"] = [row("description", s.get("description", "")), row("run", s.get("run", ""), file_link("sources", name, folder, s.get("run", ""))),
                       row("output", s.get("output", ""), note="a JSON list, written into each fetch's own folder"),
                       row("fields", links=links(s.get("fields") if isinstance(s.get("fields"), list) else [], "")),
                       *[row(k, val) for k, val in s.items() if k not in ("description", "run", "output", "fields")]]
        v["accepted"] = [{"flow": f, "where": _load(live.flow, f, default={}).get("where") or {}, "schedule": schedule_cell(store, f, _load(live.flow, f, default={}))}
                         for f in v["flows"]]
        v["grants"] = grants_view(settings, f"sources/{name}")
    elif tab == "fetches":
        v["fetches"] = [{"at": show.when(f["started"]), "by": show.fetch_by(f["scheduled_for"]),
                         "outcome": f["outcome"] or "running", "count": f["item_count"], "detail": f["detail"] or "",
                         "time": show.duration(show.seconds_between(f["started"], f["ended"])) if f["ended"] else "",
                         "folder": str(store.root / f["folder"]) if f["folder"] else "",
                         "runs": [{"id": r["id"], "flow": r["flow"]} for r in store.q("SELECT id, flow FROM runs WHERE fetch_id = ?", f["id"])]}
                        for f in store.q("SELECT * FROM fetches WHERE source = ? ORDER BY id DESC LIMIT 30", name)]
    else:
        v.update(files_tab(settings, "sources", name, file))
    return v


# ---------- the platform ----------

def settings_view(settings) -> dict:
    """agentflow.yaml as the engine reads it. A key is shown as the name of its variable and whether that is set, never as a value."""
    models = []
    for name, written in settings.models.items():
        m = {**MODEL_DEFAULTS, **(written if isinstance(written, dict) else {})}
        models.append({"name": name, "default": name == settings.default_model, "provider": m["provider"], "model": m["name"], "api": m["api"],
                       "reasoning": m["reasoning"], "key_env": m["api_key_env"], "key_set": bool(os.environ.get(m["api_key_env"]))})
    kinds = {k: installed(settings).names(k) for k in KINDS}
    grants = []
    for key in settings.grants:
        kind, _, name = str(key).partition("/")
        grants.append({"plugin": str(key), "href": f"/{key}" if name in kinds.get(kind, []) else "", "secrets": grants_view(settings, key)})
    return {"file": str(settings.home / "agentflow.yaml"), "plugins": str(settings.plugins), "data": str(settings.data), "errors": settings.check(),
            "models": models, "limits": [row(k, val) for k, val in settings.limits.items()], "grants": grants}
