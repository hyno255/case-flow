"""A run as a tree of timed spans, built from the event log: run > item > stage > try > part > call.

Read-only. A part is one piece of a try: the script or the agent doing the stage, the check script, or the
reviewer. A call is one model request or one tool call. Every span says who did it and on which model, when it
began and ended, what it used, and what it was given and answered, so a page can draw the tree beside a detail pane.
"""
import json
import time
from datetime import datetime
from pathlib import Path

from .layout import prompt_file
from .procs import DONE_EARLY
from .status import ENDINGS, OUTCOMES, label

TRY_STATE = {name: bar for name, (_, _, bar) in OUTCOMES.items() if bar}   # a try with one of these outcomes is over
ITEM_STATE = {name: bar for name, (_, _, bar) in ENDINGS.items() if bar}
FIXED = ("seq", "ts", "run", "item", "stage", "type")  # the fields every event has; the rest is what the event carries
USE = ("tokens_in", "tokens_out", "cost", "model_calls", "tool_calls")  # added up from the calls into every span above them
PARTS = ("agent", "script", "check", "review")
RUN_MARKS = {"report_rendered": "report written", "run_interrupted": "interrupted", "run_resumed": "resumed", "cancel_requested": "cancel asked for"}


def epoch(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso).timestamp() if iso else None


def seconds_text(s: float) -> str:
    if s < 0.0005:
        return "0s"
    if s < 1:
        return f"{s * 1000:.0f}ms"
    if s < 10:
        return f"{s:.2f}s"
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s) // 60}m {int(s) % 60:02d}s"


def _span(kind: str, name: str, start: float, end: float | None = None, **extra) -> dict:
    return {"kind": kind, "name": name, "who": "", "model": "", "start": start, "end": end, "state": "", "note": "", "href": "",
            "facts": [], "blocks": [], "children": [], "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "cost_unknown": False, "cost_note": "",
            "model_calls": 0, "tool_calls": 0, **extra}


def _text(value, cap: int) -> str:
    """What a call was given or answered, as text. JSON is laid out so it can be read."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value[:1] in "{[" else value
        except ValueError:
            pass
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    return text if len(text) <= cap else text[:cap] + " …"


def build(store, run, show: dict, item_id: str | None = None, live: bool = False, calls: bool = True, folder: Path | None = None) -> dict:
    """The span tree for a run, or for one item of it. `show` is the settings' `show` section: how much text a span carries. `live` says the run's process is still working, so open spans
    end now. With calls=False the tree stops at the parts of a try, which keeps a run page of many items small.
    `folder` is the item's work folder: with it, an agent's span carries the prompt its try was told."""
    block = show["block_chars"]
    events = store.events(run["id"], item_id, run_level=True)
    outcomes = {(a["item_id"], a["stage"], a["n"]): a["outcome"] for a in store.q("SELECT item_id, stage, n, outcome FROM attempts WHERE run_id = ?", run["id"])}
    now = time.time()
    items: dict[str, dict] = {}
    marks: list[dict] = []
    last = epoch(run["started"]) or now
    began, ended, fetched = None, None, None  # from the run's own events, which are finer than the stored times

    for d in events:
        ts, t = d["ts"], d["type"]
        last = max(last, ts)
        if d["item"] is None:
            if t == "run_started":
                began = ts
                if d.get("fetch_seconds") is not None:
                    fetched = _span("fetch", d.get("source") or run["source"] or "source", ts - d["fetch_seconds"], ts, state="ok", who="script", note=f"{d.get('items')} items")
            elif t == "run_ended":
                ended = ts
            elif item_id is None:
                m = _span("mark", RUN_MARKS.get(t, t.replace("_", " ")), ts, ts)
                m["facts"] = [(k, _text(v, show["fact_chars"])) for k, v in d.items() if k not in FIXED and v not in ("", None)]
                marks.append(m)
            continue
        it = items.setdefault(d["item"], _span("item", d["item"], ts, stages={}, last=ts, ended_as=None))
        it["last"] = ts
        if t == "item_ended":
            it["end"], it["ended_as"] = ts, d.get("ended_as")
            if d.get("reason"):
                it["blocks"].append(("reason", d["reason"]))
        elif t == "item_carried_over":
            it["end"], it["note"], it["state"] = ts, f"carried over from {d.get('from_run')}", "skip"
        elif t == "item_started" and d.get("from_stage"):
            it["facts"].append(("continues at", d["from_stage"]))
        if d["stage"] is None:
            continue
        st = it["stages"].setdefault(d["stage"], _span("stage", d["stage"], ts, tries={}))
        if t == "sent_back":  # the fail edge led to an earlier stage
            m = _span("mark", f"sent back to {d.get('to')}", ts, ts, state="fail")
            m["blocks"].append(("reason", d.get("reason") or ""))
            st["children"].append(m)
            continue
        n = d.get("n")
        if n is None:
            continue
        tr = st["tries"].setdefault(n, _span("try", f"try {n}", ts, last=ts, open={}, review=None, execute=None))
        tr["last"] = ts

        def part(role):
            """The part of the try a call belongs to. A reviewer's calls arrive before its verdict."""
            key = "review" if role else "execute"
            if tr[key] is None:
                tr[key] = _span("review" if role else "agent", key, ts, who=d.get("agent") or ("" if role else tr["who"]))
                tr["children"].append(tr[key])
            return tr[key]

        if t == "attempt_started":
            by = d.get("by") or ""
            tr["start"], tr["who"], tr["model"] = ts, by, d.get("model") or ""
            ex = tr["execute"] = _span("script", "script", ts, who="script") if by == "script" else _span("agent", "execute", ts, who=by, model=tr["model"])
            tr["children"].append(ex)
            if d.get("feedback"):
                tr["facts"].append(("told", "the reasons from earlier tries"))
            if ex["kind"] == "agent":
                rel = prompt_file(d["stage"], n)
                told = folder / rel if folder else None
                if told and told.is_file():
                    ex["blocks"].append((f"prompt · {rel}", told.read_text(errors="replace")[:show["prompt_chars"]]))
                else:
                    ex["facts"].append(("prompt", rel))
        elif t == "tool_call":
            c = _span("tool", d.get("tool", "tool"), ts, who=d.get("agent") or "", tool_calls=1)
            c["blocks"].append(("arguments", _text(d.get("args", ""), block)))
            part(d.get("role"))["children"].append(c)
            tr["open"].setdefault(d["call"], []).append(c)
        elif t == "tool_result":
            waiting = tr["open"].get(d["call"]) or []
            if waiting:
                c = waiting.pop(0)
                c["end"] = ts
                c["facts"].append(("answer", f"{d.get('chars', 0):,} characters"))
                if d.get("preview"):
                    c["blocks"].append(("answer", _text(d["preview"], block)))
        elif t == "model_call":
            at, price = d.get("started"), d.get("cost_usd")
            c = _span("model", "model call", at or ts, (at + d.get("seconds", 0)) if at else ts, who=d.get("agent") or "", model=d.get("model") or "",
                      tokens_in=d.get("tokens_in") or 0, tokens_out=d.get("tokens_out") or 0, model_calls=1, cost=price or 0.0, cost_unknown=price is None,
                      cost_note="not recorded" if price is None else "estimated" if price else "free", note="" if at else "time not recorded")
            part(d.get("role"))["children"].append(c)
        elif t == "attempt_ended":
            ex = part(None)
            ex["end"] = ts
            if "exit_code" in d:
                ex["name"] = d.get("script") or ex["name"]
                ex["state"] = "ok" if d["exit_code"] in (0, DONE_EARLY) else "bad"
                ex["facts"].append(("exit code", str(d["exit_code"])))
                if d.get("output"):
                    ex["blocks"].append(("output", _text(d["output"], block)))
            elif d.get("error"):
                ex["state"] = "bad"
                ex["blocks"].append(("error", _text(d["error"], block)))
            else:
                ex["state"] = "ok"
        elif t == "verdict":
            by = d.get("by", "")
            if by.startswith("check "):
                at = ts - d["seconds"]
                ph = _span("check", by[6:], at, ts, who="script")
                tr["children"].append(ph)
            else:
                ph = part("reviewer")
                tr["review"] = None  # a later verdict in this try is its own part
                ph["who"], ph["end"] = by, ts
                if "seconds" in d:
                    ph["start"] = min(ph["start"], ts - d["seconds"])
            ph["state"] = "ok" if d.get("passed") else "fail"
            ph["note"] = "pass" if d.get("passed") else "asked for a person" if d.get("needs_person") else "fail"
            ph["facts"].append(("verdict", ph["note"]))
            if d.get("reason"):
                ph["blocks"].append(("output" if ph["kind"] == "check" else "reason", _text(d["reason"], block)))
        elif t == "person_asked":
            tr["blocks"].append(("asked for a person", _text(d.get("reason", ""), block)))
        elif t in ("attempt_interrupted", "attempt_cancelled"):
            tr["facts"].append(("cut short", "the process stopped" if t == "attempt_interrupted" else f"{d.get('script', 'a script')} was stopped: the run was cancelled"))

    def close(s: dict, fallback: float, working: bool) -> None:
        """Ends that were never recorded: now for a part that is still working, else the last thing known of it."""
        for c in s["children"]:
            close(c, fallback, working)
        if s["end"] is None:
            s["end"] = now if working else max([fallback, s["start"]] + [c["end"] for c in s["children"]])
            s["state"] = s["state"] or ("run" if working else "cut")
        s["end"] = max(s["end"], s["start"])

    def add_up(s: dict) -> None:
        for c in s["children"]:
            add_up(c)
            for k in USE:
                s[k] += c[k]
            s["cost_unknown"] = s["cost_unknown"] or c["cost_unknown"]

    for iid, it in items.items():
        for sid, st in it.pop("stages").items():
            tries = []
            for n, tr in sorted(st.pop("tries").items()):
                outcome = outcomes.get((iid, sid, n))
                working = live and outcome not in TRY_STATE  # a try with an outcome is over, even while its run goes on
                close(tr, tr.pop("last"), working)
                tr["end"] = max([tr["end"]] + [c["end"] for c in tr["children"]])
                tr["state"] = TRY_STATE.get(outcome, "run" if working else "cut")
                tr["note"] = label(OUTCOMES, outcome) if outcome in OUTCOMES else "running" if working else "not finished"
                ex = tr.pop("execute")
                if ex and ex["kind"] == "agent" and outcome in ("interrupted", "timed_out", "error") and not any(c["kind"] == "model" for c in ex["children"]):
                    ex["cost_unknown"] = True  # the model was used, but the try was cut before its usage was recorded
                tr.pop("open"), tr.pop("review")
                for ph in tr["children"]:  # model calls are recorded when the try ends; put every call where it happened
                    ph["children"].sort(key=lambda c: c["start"])
                    ph["who"] = ph["who"] or ("reviewer" if ph["kind"] == "review" else tr["who"])
                    ph["model"] = ph["model"] or next((c["model"] for c in ph["children"] if c["model"]), "")
                    for c in ph["children"]:
                        c["who"] = c["who"] or ph["who"]
                tr["model"] = tr["model"] or (ex["model"] if ex else "")
                tries.append(tr)
            st["who"] = tries[0]["who"] if tries else ""
            st["children"] = sorted(tries + st["children"], key=lambda s: s["start"])  # a "sent back" mark sits after the try it follows
            st["start"] = min([st["start"]] + [t["start"] for t in tries])
            st["end"] = max(t["end"] for t in st["children"]) if st["children"] else st["start"]
            st["state"] = tries[-1]["state"] if tries else ""
            st["note"] = f"{len(tries)} tries" if len(tries) > 1 else ""
            it["children"].append(st)
        close(it, it.pop("last"), live)
        ended_as = it.pop("ended_as")
        it["state"] = it["state"] or ITEM_STATE.get(ended_as, it["state"] or ("run" if live else "cut"))
        it["note"] = it["note"] or (label(ENDINGS, ended_as) if ended_as else "running" if live else "not finished")

    root = _span("run", run["id"], began or epoch(run["started"]) or last, (ended or epoch(run["ended"])) if run["ended"] else None)
    if fetched and item_id is None:  # the source script ran just before the run began
        root["children"].append(fetched)
        root["start"] = fetched["start"]
    root["children"] += sorted(items.values(), key=lambda s: (s["start"], s["name"])) + marks
    root["start"] = min([root["start"]] + [c["start"] for c in root["children"]])
    close(root, last, live)
    root["end"] = max([root["end"]] + [c["end"] for c in root["children"]])
    add_up(root)
    if item_id is not None and item_id in items:  # an item page shows the item as the top of the tree
        root = items[item_id]

    t0, total = root["start"], max(root["end"] - root["start"], 0.001)

    def finish(s: dict, depth: int, path: str) -> None:
        s["id"], s["depth"] = path, depth
        s["seconds"] = s["end"] - s["start"]
        s["left"] = round(min(max((s["start"] - t0) / total, 0), 1) * 100, 3)
        s["width"] = round(min(max(s["seconds"] / total, 0), 1 - s["left"] / 100) * 100, 3)
        s["time"] = "" if s["kind"] == "mark" else seconds_text(s["seconds"])
        s["tokens"] = s["tokens_in"] + s["tokens_out"]
        if not calls and s["kind"] in PARTS:
            counted = [f"{s[k]} {word}" for k, word in (("model_calls", "model calls"), ("tool_calls", "tool calls")) if s[k]]
            s["note"] = " · ".join(filter(None, [s["note"], *counted]))
            s["children"] = []
        for i, c in enumerate(s["children"]):
            finish(c, depth + 1, f"{path}/{c['name'] if c['kind'] in ('item', 'stage', 'try') else i}")

    finish(root, 0, root["name"])
    return {"root": root, "ticks": [seconds_text(total * p / 100) for p in (0, 25, 50, 75, 100)]}


def walk(span: dict):
    """Every span of a tree, the top first."""
    yield span
    for child in span["children"]:
        yield from walk(child)
