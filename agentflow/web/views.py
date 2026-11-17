"""What the run pages and the JSON API show: the list of runs, a run, and one item of a run.

Every function only reads the store and the run folders, so status is worked out when asked and the command
line, the API and the pages cannot disagree. A view is a dictionary; a page shows one tab of it.
"""
import json
import os
from collections import Counter

from agentflow.records import trace as tracing
from agentflow.plugins.graph import stage_by
from agentflow.records.layout import ENGINE_OWNED, OUTPUT
from agentflow.plugins.registry import installed, run_plugins
from agentflow.records.status import ENDINGS, item_status, label, run_status
from . import draw, show

RUN_TABS = ("items", "output", "trace", "flow")
ITEM_TABS = ("files", "trace")
WORKING = ("running", "stopping")
LIVE = (*WORKING, "interrupted")  # a run in one of these states can still change, so its pages are fetched again every few seconds


def stale(settings) -> float:
    return settings.internal["stale_after_seconds"]


def nav(settings, store, section: str) -> dict:
    """What the sidebar shows on every page."""
    running = sum(run_status(r, stale(settings))[0] in WORKING for r in store.q("SELECT * FROM runs WHERE ended IS NULL"))
    return {"section": section, "running": running}


def flow_of(settings, store, run) -> dict:
    """The flow as this run used it, from the run's own copy of the plugin files."""
    try:
        f = run_plugins(settings, store, run).flow(run["flow"])
    except Exception:  # the copy is gone or cannot be read: the pages still show what the store holds
        return {}
    return f if isinstance(f, dict) else {}


# ---------- the list of runs ----------

def item_counts(settings, run, rows) -> list[dict]:
    """How a run's items stand: {n, label, pill}, endings first in their usual order."""
    order = [label(ENDINGS, e) for e in ENDINGS]
    seen = Counter(item_status(run, it, stale(settings)) for it in rows)
    return [{"n": n, "label": text, "pill": pill}
            for (text, pill), n in sorted(seen.items(), key=lambda kv: order.index(kv[0][0]) if kv[0][0] in order else len(order))]


def started_by(store, run) -> str:
    """What started a run: a trial, the flow's schedule, or a person."""
    if run["trial"]:
        return "trial"
    return show.fetch_by(store.one("SELECT scheduled_for FROM fetches WHERE id = ?", run["fetch_id"])["scheduled_for"])


def run_row(settings, store, run) -> dict:
    status, pill = run_status(run, stale(settings))
    rows, u = store.items(run["id"]), store.usage(run["id"])
    return {"id": run["id"], "flow": run["flow"], "trial": bool(run["trial"]), "started": show.when(run["started"]), "by": started_by(store, run), "status": status, "pill": pill,
            "counts": item_counts(settings, run, rows), "n_items": len(rows), "tokens": u["tokens"], "cost": show.cost(u),
            "time": show.duration(show.seconds_between(run["started"], run["ended"])),
            "output": f"/runs/{run['id']}/output/index.html" if (store.path(run) / OUTPUT / "index.html").is_file() else ""}


def run_rows(settings, store, flow: str | None = None) -> list[dict]:
    where, args = ("WHERE flow = ?", [flow]) if flow else ("", [])
    return [run_row(settings, store, r)
            for r in store.q(f"SELECT * FROM runs {where} ORDER BY started DESC, rowid DESC LIMIT ?", *args, settings.internal["rows"])]


def runs_view(settings, store, errors: dict) -> dict:
    """Every run of every flow. `errors` holds why a flow could not start a run; a run is started from its flow's page."""
    failed = [{"href": "/" + key, "name": key.partition("/")[2], "error": text} for key, text in errors.items() if "/" in key]
    live = installed(settings)
    rows = run_rows(settings, store)
    return {"runs": rows, "failed": failed, "live": any(r["status"] in LIVE for r in rows), "flows": live.names("flows"), "sources": live.names("sources")}


# ---------- one run ----------

def item_row(settings, store, run, it) -> dict:
    last, _ = store.last_try(run["id"], it["item_id"])
    u = store.usage(run["id"], it["item_id"])
    status, pill = item_status(run, it, stale(settings))
    return {"id": it["item_id"], "title": show.item_title(json.loads(it["payload"]), settings.internal["label_chars"]), "status": status, "pill": pill,
            "stage": (last["stage"] + (f" · try {last['n']}" if last["n"] > 1 else "")) if last else "", "reason": it["reason"] or "",
            "tokens": u["tokens"], "cost": show.cost(u),
            "time": show.duration(show.seconds_between(it["claimed_at"], it["ended_at"])) if it["claimed_at"] else "",
            "folder": str(store.path(it)), "folder_short": os.path.relpath(store.path(it), store.path(run))}  # shown from the run folder; the whole path is the tooltip


def run_graph(settings, store, run, flow: dict, status: str, flow_exists: bool) -> tuple[dict, list[dict]]:
    """The flow's graph as this run saw it, with what the run did at each stage (passed, running, tries), and the same numbers as rows."""
    done = {r["stage"]: r for r in store.q("""SELECT stage, COUNT(*) tries, COUNT(DISTINCT CASE WHEN outcome = 'passed' THEN item_id END) passed
                                             FROM attempts WHERE run_id = ? GROUP BY stage""", run["id"])}
    at = Counter(r["stage"] for r in store.q("""SELECT a.stage FROM attempts a JOIN items i ON i.run_id = a.run_id AND i.item_id = a.item_id
                                               WHERE a.run_id = ? AND i.ended_at IS NULL AND i.claimed_at IS NOT NULL
                                                 AND a.id = (SELECT MAX(id) FROM attempts WHERE run_id = a.run_id AND item_id = a.item_id)""", run["id"]))
    doing = "interrupted" if status == "interrupted" else "running"
    stats = {}
    for sid in {*done, *at}:
        d, here = done.get(sid), at[sid]
        stats[sid] = {"state": ("cut" if doing == "interrupted" else "now") if here else "done" if d and d["passed"] else "",
                      "lines": [" · ".join(filter(None, [f"{d['passed']} passed" if d else "", f"{here} {doing}" if here else ""])), f"{d['tries']} {'try' if d['tries'] == 1 else 'tries'}" if d else ""]}
    files = output_files(store, run)
    if files:
        stats[":end"] = {"state": "done", "lines": [f"{len(files)} {'file' if len(files) == 1 else 'files'}"], "href": f"/runs/{run['id']}/output"}
    try:
        source = run_plugins(settings, store, run).source(flow.get("source") or "")
    except Exception:
        source = None
    rows = [{"id": s["id"], "by": f"agent {s['agent']}" if "agent" in s else f"script {s['run']}" if "run" in s else stage_by(s),
             "passed": done[s["id"]]["passed"] if s["id"] in done else 0, "tries": done[s["id"]]["tries"] if s["id"] in done else 0, "now": at[s["id"]]}
            for s in flow.get("stages") or [] if isinstance(s, dict) and "id" in s]
    return draw.graph(flow, settings.limits_for(flow), source, stats, base=f"/flows/{run['flow']}" if flow_exists else None), rows


def dress(tr: dict) -> dict:
    """Gives every span of a trace its cost as the tables word it."""
    for s in tracing.walk(tr["root"]):
        s["cost_cell"] = show.cost({"has_model_calls": s["model_calls"] or s["cli_runs"] or s["cost_unknown"], "cost_known": not s["cost_unknown"], "cost_usd": s["cost"]},
                                   "the cost was not recorded")
        if s["cost_note"]:  # one model call knows what kind of number its cost is
            s["cost_cell"]["tip"] = s["cost_note"]
    return tr


def output_files(store, run) -> list[dict]:
    """What the flow put in the run's output folder, each with the address that serves it."""
    out = store.path(run) / OUTPUT
    return [{"path": str(p.relative_to(out)), "href": f"/runs/{run['id']}/output/{p.relative_to(out)}", "size": p.stat().st_size}
            for p in sorted(out.rglob("*")) if p.is_file() and not p.is_symlink()] if out.is_dir() else []


def finish_error(store, run_id: str) -> str:
    """Why the flow's finish script failed in this run, if it did."""
    last = store.one("SELECT data FROM events WHERE run_id = ? AND type = 'finish_ended' ORDER BY seq DESC LIMIT 1", run_id)
    d = json.loads(last["data"]) if last else {}
    return f"The finish script {d['script']} exited with code {d['exit_code']}: {d.get('output') or 'it printed nothing'}" if d.get("exit_code") else ""


def run_view(settings, store, run_id: str, tab: str = "items", span: str = "", error: str = "") -> dict | None:
    """One tab of a run page. `error` is why a resume of this run could not go on."""
    run = store.run(run_id)
    if run is None or tab not in RUN_TABS:
        return None
    flow = flow_of(settings, store, run)
    v = run_row(settings, store, run)
    status = v["status"]
    v.update({"tab": tab, "live": status in LIVE, "can_resume": status == "interrupted", "can_cancel": status in ("running", "interrupted"),
              "source": run["source"], "cap": f"${settings.limits_for(flow)['cost_per_run_usd']:.2f}",
              "snapshot": run["snapshot"], "folder": str(store.path(run)),
              "flow_exists": (settings.plugins / "flows" / run["flow"] / "flow.yaml").is_file(),
              "error": error or (finish_error(store, run_id) if run["ended"] else "")})
    if tab == "items":
        v["items"] = [item_row(settings, store, run, it) for it in store.items(run_id)]
    elif tab == "output":
        files = output_files(store, run)
        v["output_folder"], v["files"], v["more"] = str(store.path(run) / OUTPUT), files[:settings.internal["rows"]], max(0, len(files) - settings.internal["rows"])
        v["finish"] = flow.get("finish") or ""
    elif tab == "trace":
        tr = tracing.build(store, run, settings.internal, live=status in WORKING, calls=False)
        for s in tr["root"]["children"]:
            if s["kind"] == "item":
                s["href"] = f"/runs/{run_id}/items/{s['name']}/trace"
        v["trace"], v["span"] = dress(tr), span or tr["root"]["id"]
    else:
        v["graph"], v["stages"] = run_graph(settings, store, run, flow, status, v["flow_exists"])
    return v


# ---------- one item of a run ----------

def item_view(settings, store, run_id: str, item_id: str, tab: str = "files", span: str = "", file: str = "") -> dict | None:
    """One tab of an item page."""
    run = store.run(run_id)
    it = store.item(run_id, item_id) if run else None
    if it is None or tab not in ITEM_TABS:
        return None
    row = item_row(settings, store, run, it)
    folder = store.path(it)
    status = "" if it["ended_at"] else run_status(run, stale(settings))[0]  # an ended item no longer changes with its run
    working = status in WORKING
    v = {**row, "run": run_id, "tab": tab, "ended_as": it["ended_as"], "live": status in LIVE}
    if tab == "trace":
        tr = tracing.build(store, run, settings.internal, item_id=item_id, live=working, folder=folder if folder.is_dir() else None)
        v["trace"], v["span"] = dress(tr), span or tr["root"]["id"]
    else:
        tree = show.file_tree(folder, file, ENGINE_OWNED)
        v["tree"] = tree
        v["file"] = show.file_preview(folder, file or show.first_file(tree), f"/runs/{run_id}/items/{item_id}/files", settings.internal["file_chars"])
    return v
