"""What the run pages and the JSON API show: the list of runs, a run, and one item of a run.

Every function only reads the store and the run folders, so status is worked out when asked and the command
line, the API and the pages cannot disagree. A view is a dictionary; a page shows one tab of it.
"""
import json
import os
from collections import Counter

from .. import trace as tracing
from ..graph import stage_by
from ..layout import ENGINE_OWNED, REPORT
from ..plugins import installed, run_plugins
from ..recovery import retry_stage
from ..render import md_to_html
from ..status import ENDINGS, item_status, label, run_status
from . import draw, show

RUN_TABS = ("items", "trace", "log", "flow")
ITEM_TABS = ("output", "trace", "files", "log")
WORKING = ("running", "stopping")
LIVE = (*WORKING, "interrupted")  # a run in one of these states can still change, so its pages follow the event stream


def max_seq(store) -> int:
    return store.one("SELECT COALESCE(MAX(seq), 0) n FROM events")["n"]


def nav(settings, store, section: str) -> dict:
    """What the sidebar shows on every page."""
    running = sum(run_status(r)[0] in WORKING for r in store.q("SELECT * FROM runs WHERE ended IS NULL"))
    return {"section": section, "running": running}


def flow_of(store, run) -> dict:
    """The flow as this run used it, from the run's own copy of the plugin files."""
    try:
        f = run_plugins(store, run).flow(run["flow"])
    except Exception:  # the copy is gone or cannot be read: the pages still show what the store holds
        return {}
    return f if isinstance(f, dict) else {}


def stage_who(flow: dict) -> dict:
    """stage -> who does it, for log lines of events that do not say."""
    return {s["id"]: f"script {s['run']}" if "run" in s else stage_by(s) for s in flow.get("stages") or [] if isinstance(s, dict) and "id" in s}


# ---------- the list of runs ----------

def item_counts(run, rows) -> list[dict]:
    """How a run's items stand: {n, label, pill}, endings first in their usual order."""
    order = [label(ENDINGS, e) for e in ENDINGS]
    seen = Counter(item_status(run, it) for it in rows)
    return [{"n": n, "label": text, "pill": pill}
            for (text, pill), n in sorted(seen.items(), key=lambda kv: order.index(kv[0][0]) if kv[0][0] in order else len(order))]


def started_by(store, run) -> str:
    """What started a run: a trial, a retry, the flow's schedule, or a person."""
    if run["trial"] or run["retry_of"]:
        return "trial" if run["trial"] else "retry"
    fetch = store.one("SELECT scheduled_for FROM fetches WHERE id = ?", run["fetch_id"]) if run["fetch_id"] else None
    return show.fetch_by(fetch["scheduled_for"]) if fetch else "by hand"


def run_row(store, run) -> dict:
    status, pill = run_status(run)
    rows, u = store.items(run["id"]), store.usage(run["id"])
    return {"id": run["id"], "flow": run["flow"], "trial": bool(run["trial"]), "started": show.when(run["started"]), "by": started_by(store, run), "status": status, "pill": pill,
            "counts": item_counts(run, rows), "n_items": len(rows), "tokens": u["tokens"], "cost": show.cost(u),
            "time": show.duration(show.seconds_between(run["started"], run["ended"])),
            "report": f"/runs/{run['id']}/report/index.html" if (store.path(run) / REPORT / "index.html").is_file() else ""}


def run_rows(store, flow: str | None = None, limit: int = 50) -> list[dict]:
    where, args = ("WHERE flow = ?", [flow]) if flow else ("", [])
    return [run_row(store, r) for r in store.q(f"SELECT * FROM runs {where} ORDER BY started DESC, rowid DESC LIMIT ?", *args, limit)]


def runs_view(settings, store, errors: dict) -> dict:
    """Every run of every flow. `errors` holds why a flow could not start a run; a run is started from its flow's page."""
    failed = [{"href": "/" + key, "name": key.partition("/")[2], "error": text} for key, text in errors.items() if "/" in key]
    live = installed(settings)
    return {"runs": run_rows(store), "failed": failed, "seq": max_seq(store), "flows": live.names("flows"), "sources": live.names("sources")}


# ---------- one run ----------

def item_row(store, run, it) -> dict:
    last, _ = store.last_try(run["id"], it["item_id"])
    u = store.usage(run["id"], it["item_id"])
    status, pill = item_status(run, it)
    spent = show.cost(u) if not it["carried_from"] else {"text": show.DASH, "tip": "carried over: nothing was spent in this run"}
    return {"id": it["item_id"], "title": show.item_title(json.loads(it["payload"] or "{}")), "status": status, "pill": pill,
            "stage": (last["stage"] + (f" · try {last['n']}" if last["n"] > 1 else "")) if last else "", "reason": it["reason"] or "",
            "carried_from": it["carried_from"], "tokens": u["tokens"], "cost": spent,
            "time": show.duration(show.seconds_between(it["claimed_at"], it["ended_at"])) if it["claimed_at"] else "",
            "report": f"/runs/{run['id']}/report/{it['item_id']}.html" if (store.path(run) / REPORT / f"{it['item_id']}.html").is_file() else "",
            "folder": str(store.path(it)), "folder_short": os.path.relpath(store.path(it), store.path(run))}  # shown from the run folder, which the page's head names; copied in full


def run_graph(settings, store, run, flow: dict, status: str) -> tuple[dict, list[dict]]:
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
    if (store.path(run) / REPORT / "index.html").is_file():
        stats[":end"] = {"state": "done", "lines": ["written"], "href": f"/runs/{run['id']}/report/index.html"}
    try:
        source = run_plugins(store, run).source(flow.get("source") or "")
    except Exception:
        source = None
    exists = (settings.plugins / "flows" / run["flow"] / "flow.yaml").is_file()
    rows = [{"id": s["id"], "by": f"agent {s['agent']}" if "agent" in s else f"script {s['run']}" if "run" in s else stage_by(s),
             "passed": done[s["id"]]["passed"] if s["id"] in done else 0, "tries": done[s["id"]]["tries"] if s["id"] in done else 0, "now": at[s["id"]]}
            for s in flow.get("stages") or [] if isinstance(s, dict) and "id" in s]
    return draw.graph(flow, source, stats, base=f"/flows/{run['flow']}" if exists else None), rows


def dress(tr: dict) -> dict:
    """Gives every span of a trace its cost as the tables word it."""
    for s in tracing.walk(tr["root"]):
        s["cost_cell"] = show.cost({"has_model_calls": s["model_calls"] or s["cost_unknown"], "cost_known": not s["cost_unknown"], "cost_usd": s["cost"]},
                                   "the cost was not recorded")
        if s["cost_note"]:  # one model call knows what kind of number its cost is
            s["cost_cell"]["tip"] = s["cost_note"]
    return tr


def run_view(settings, store, run_id: str, tab: str = "items", span: str = "", error: str = "") -> dict | None:
    """One tab of a run page. `error` is why a resume or a retry of this run could not go on."""
    run = store.run(run_id)
    if run is None or tab not in RUN_TABS:
        return None
    flow = flow_of(store, run)
    v = run_row(store, run)
    status = v["status"]
    v.update({"tab": tab, "live": status in LIVE, "can_resume": status == "interrupted", "can_cancel": status in ("running", "interrupted"),
              "source": run["source"], "cap": f"${settings.limits_for(flow)['cost_per_run_usd']:.2f}",
              "snapshot": run["snapshot"], "folder": str(store.path(run)),
              "flow_exists": (settings.plugins / "flows" / run["flow"] / "flow.yaml").is_file(),
              "retry_of": run["retry_of"], "retry_item": run["retry_item"], "from_stage": run["from_stage"], "error": error, "seq": max_seq(store)})
    if tab == "items":
        v["items"] = [item_row(store, run, it) for it in store.items(run_id)]
        v["retries"] = [{"run": r["id"], "item": r["retry_item"], "from_stage": r["from_stage"]}
                        for r in store.q("SELECT id, retry_item, from_stage FROM runs WHERE retry_of = ? ORDER BY started", run_id)]
    elif tab == "trace":
        tr = tracing.build(store, run, live=status in WORKING, calls=False)
        for s in tr["root"]["children"]:
            if s["kind"] == "item":
                s["href"] = f"/runs/{run_id}/items/{s['name']}/trace"
        v["trace"], v["span"] = dress(tr), span or tr["root"]["id"]
    elif tab == "log":
        v["item_ids"], v["who"] = [it["item_id"] for it in store.items(run_id)], stage_who(flow)
    else:
        v["graph"], v["stages"] = run_graph(settings, store, run, flow, status)
    return v


# ---------- one item of a run ----------

def retry_from(settings, store, run, it) -> str:
    """The stage a retry of this item would start at; "" when it cannot be retried."""
    if not it["ended_at"] or it["carried_from"]:
        return ""
    try:
        graph = installed(settings).graph(run["flow"])
    except Exception:  # the flow is gone or cannot be read
        return ""
    return retry_stage(graph, store, it) if graph.stages else ""


def item_view(settings, store, run_id: str, item_id: str, tab: str = "output", span: str = "", file: str = "") -> dict | None:
    """One tab of an item page. A carried-over item shows the work of the run that did it."""
    run = store.run(run_id)
    it = store.item(run_id, item_id) if run else None
    if it is None or tab not in ITEM_TABS:
        return None
    origin = (store.run(it["carried_from"]) if it["carried_from"] else None) or run
    row = item_row(store, run, it)
    work = item_row(store, origin, store.item(origin["id"], item_id) or it) if origin is not run else row  # the numbers of the run that did the work
    folder = store.path(it)
    working = run_status(origin)[0] in WORKING and not it["ended_at"]
    v = {"run": run_id, "origin": origin["id"], "id": item_id, "tab": tab, "title": row["title"], "status": row["status"], "pill": row["pill"],
         "ended_as": it["ended_as"], "reason": it["reason"] or "", "carried_from": it["carried_from"],
         "carried": {"status": work["status"], "pill": work["pill"]} if it["carried_from"] else None,
         "stage": work["stage"], "tokens": work["tokens"], "cost": work["cost"], "time": work["time"], "folder": str(folder), "report": row["report"],
         "retry_from": retry_from(settings, store, run, it), "retry_of": origin["retry_of"], "from_stage": origin["from_stage"],
         "retries": [{"run": r["id"], "from_stage": r["from_stage"]}
                     for r in store.q("SELECT id, from_stage FROM runs WHERE retry_of = ? AND retry_item = ? ORDER BY started", origin["id"], item_id)],
         "live": run_status(origin)[0] in LIVE and not it["ended_at"], "seq": max_seq(store)}
    if tab == "output":
        report = folder / "report.md"
        v["report_md"] = str(report) if report.is_file() else ""
        v["report_html"] = md_to_html(report.read_text(errors="replace")) if report.is_file() else ""
    elif tab == "trace":
        tr = tracing.build(store, origin, item_id=item_id, live=working, folder=folder if folder.is_dir() else None)
        v["trace"], v["span"] = dress(tr), span or tr["root"]["id"]
    elif tab == "files":
        tree = show.file_tree(folder, file, ENGINE_OWNED)
        v["tree"] = tree
        v["file"] = show.file_preview(folder, file or show.first_file(tree, ("report.md",)), f"/runs/{run_id}/items/{item_id}/files")
    else:
        v["who"] = stage_who(flow_of(store, origin))
    return v
