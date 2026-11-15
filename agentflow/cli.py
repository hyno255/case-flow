"""The command line.

    agentflow check                              check every plugin file and the settings, and run every tool's health check
    agentflow try <flow> [--item ID]             one item, real stages; a trial does not count as done
    agentflow run <flow> [--force]               fetch the flow's source now and run the flow; --force works unchanged items again
    agentflow schedule [on|off <flow>]           show the flows' schedules, or switch one
    agentflow serve [--port 8321]                the web UI, the API and the schedules, until stopped
    agentflow show                               recent runs
    agentflow show <run> [item] [--follow]       what a run or one item did and cost; --follow prints its events until it ends
    agentflow resume <run>                       continue a run whose process stopped; finished stages are kept
    agentflow retry <run> <item>                 run one item again from the stage it stopped at, as a new run, with today's plugin files
    agentflow cancel <run>                       stop a run: running scripts now, agents after their try

try, run, resume, retry and serve take --stand-in: the scripted model, which needs no key and costs nothing.
"""
import argparse
import asyncio
import json
import time

from .agents import ModelNotReady
from .health import tool_health
from .layout import REPORT
from .plugins import installed
from .recovery import prepare_retry, request_cancel, resume_run, run_retry
from .runner import FetchFailed, start_flow, try_flow
from .schedule import register, set_schedule
from .settings import Settings
from .status import cost_text, item_status, run_status
from .store import Store


def money(u: dict) -> str:
    return cost_text(u, " estimated", "cost unknown")


def live_printer(event: dict) -> None:
    """One line per thing worth watching, as it happens."""
    t, who = event["type"], f"{event.get('item') or '':<9} {event.get('stage') or '':<8}"
    if t == "attempt_started":
        print(f"{who} try {event['n']}  {event['by']}" + ("  (with feedback)" if event.get("feedback") else ""), flush=True)
    elif t == "tool_call":
        args = event["args"] if isinstance(event["args"], str) else json.dumps(event["args"])
        print(f"{who}   {'reviewer ' if event.get('role') else ''}tool {event['tool']} {args[:70]}", flush=True)
    elif t == "verdict":
        print(f"{who}   {event['by']}: {'pass' if event['passed'] else 'FAIL'}  {event['reason'][:160]}", flush=True)
    elif t == "person_asked":
        print(f"{who}   asked for a person: {event['reason'][:200]}", flush=True)
    elif t == "attempt_cancelled":
        print(f"{who} try {event['n']} was cancelled: {event['script']} was stopped", flush=True)
    elif t == "attempt_interrupted":
        print(f"{who} try {event['n']} was interrupted: the process stopped", flush=True)
    elif t == "run_interrupted":
        print(f"run       interrupted: {event['reason']}", flush=True)
    elif t == "run_resumed":
        print(f"run       resumed for {', '.join(event['items']) or 'no items'}", flush=True)
    elif t == "item_started" and event.get("from_stage"):
        print(f"{event['item']:<9} continues at {event['from_stage']}; earlier stages are kept", flush=True)
    elif t == "item_carried_over":
        print(f"{who} carried over from {event['from_run']}", flush=True)
    elif t == "item_ended":
        print(f"{event['item']:<9} ended: {event['ended_as']}" + (f"  ({event['reason'][:160]})" if event.get("reason") else ""), flush=True)


def print_run(store: Store, run_id: str, item_id: str | None = None) -> int:
    run = store.need_run(run_id)
    u = store.usage(run_id)
    retry = f" · retry of {run['retry_item']} in {run['retry_of']} from {run['from_stage']}" if run["retry_of"] else ""
    print(f"\n{'trial' if run['trial'] else 'run'} {run_id} · {run['flow']} · {run_status(run)[0]}{retry} · plugins {run['snapshot']} · {u['tokens']:,} tokens · {money(u)}")
    for it in store.items(run_id):
        if item_id and it["item_id"] != item_id:
            continue
        iu = store.usage(run_id, it["item_id"])
        extra = f"from {it['carried_from']}" if it["carried_from"] else f"{iu['tool_calls']} tool calls · {iu['tokens']:,} tokens · {money(iu)} · {iu['seconds']:.0f}s"
        print(f"\n{it['item_id']:<9} {item_status(run, it)[0]:<15} {extra}")
        if it["reason"]:
            print(f"{'':<9} {it['reason'][:300]}")
        for s in store.q("SELECT stage, MIN(by) by, COUNT(*) tries FROM attempts WHERE run_id = ? AND item_id = ? GROUP BY stage ORDER BY MIN(id)", run_id, it["item_id"]):
            su = store.usage(run_id, it["item_id"], s["stage"])
            print(f"  {s['stage']:<8} {s['by']:<15} tries {s['tries']}  {su['tool_calls']:>2} tool calls  {su['tokens']:>7,} tokens  {money(su):<18} {su['seconds']:.1f}s")
    print(f"\nfolder    {store.path(run)}\nreport    {store.path(run) / REPORT / 'index.html'}")
    return 0


def print_runs(store: Store) -> int:
    for run in store.q("SELECT * FROM runs ORDER BY started DESC, rowid DESC LIMIT 20"):
        ends = store.q("SELECT COALESCE(ended_as,'not finished') e, COUNT(*) n FROM items WHERE run_id = ? GROUP BY e", run["id"])
        print(f"{run['id']}  {run['flow']:<16} {run['started']}  {'trial · ' if run['trial'] else ''}{run_status(run)[0]} · "
              f"{', '.join(f'{x['n']} {x['e']}' for x in ends)} · {money(store.usage(run['id']))}")
    return 0


def follow(store: Store, run_id: str) -> None:
    """Print a run's events from the first one, then as they come, until the run ends. The same events the pages stream."""
    store.need_run(run_id)
    seq = 0
    while True:
        events = store.events(run_id, after=seq)
        for e in events:
            seq = e["seq"]
            live_printer(e)
        if any(e["type"] == "run_ended" for e in events) or (not events and store.run(run_id)["ended"]):
            return
        time.sleep(0.5)


def cmd_check(a, settings: Settings) -> int:
    bad = 0
    for kind, name, errs, summary in installed(settings).check_all():
        bad += len(errs)
        print(f"{kind:<8} {name:<20} {'ok   ' if not errs else 'ERROR'}  {summary}")
        for e in errs:
            print(f"{'':<8} {e}")
    live = installed(settings)
    for name in live.names("tools"):
        ok, detail = asyncio.run(tool_health(settings, live, name))
        bad += not ok
        print(f"health   {name:<20} {'ok   ' if ok else 'FAIL '}  {detail}")
    print()
    for name in settings.models:
        m = settings.model(name)
        print(f"model    {name}{' (default)' if name == settings.default_model else ''}: {m['provider']} · {m['name']} · api {m['api']} · reasoning {m['reasoning']} · key in {m['api_key_env']}")
    for key in settings.grants:
        print(f"grant    {key}: secrets {', '.join(settings.granted(key)) or 'none'}")
    errs = settings.check()
    for e in errs:
        print(f"ERROR    agentflow.yaml: {e}")
    return 1 if bad or errs else 0



# ---------- one function per command ----------

STAND_IN = " · scripted stand-in model"


def flow_model(settings, flow: str) -> str:
    """The model a flow's stages use unless a stage or its agent names another."""
    try:
        return f"model: {settings.model_label(installed(settings).flow(flow).get('model'))}"
    except Exception:  # the flow cannot be read: its check says why
        return "model: not known"


def cmd_try(a, settings, store) -> int:
    print(f"trial · one item · {flow_model(settings, a.flow)}\n")
    return print_run(store, asyncio.run(try_flow(settings, store, a.flow, a.item)))


def cmd_run(a, settings, store) -> int:
    print(f"{a.flow} · {flow_model(settings, a.flow)}\n")
    return print_run(store, asyncio.run(start_flow(settings, store, a.flow, force=a.force)))


def cmd_resume(a, settings, store) -> int:
    print(f"resuming {a.run} · finished stages are kept{STAND_IN if settings.stand_in else ''}\n")
    return print_run(store, asyncio.run(resume_run(settings, store, a.run)))


def cmd_retry(a, settings, store) -> int:
    new = prepare_retry(settings, store, a.run, a.item)
    print(f"retry of {a.item} from stage {store.run(new)['from_stage']}, as run {new}{STAND_IN if settings.stand_in else ''}\n")
    return print_run(store, asyncio.run(run_retry(settings, store, new)))


def cmd_cancel(a, settings, store) -> int:
    how = request_cancel(store, a.run)
    print(f"{a.run}: " + ("asked to stop; running scripts are stopped now, and an agent finishes its try first" if how == "asked" else "closed; its process had already stopped"))
    return 0


def cmd_schedule(a, settings, store) -> int:
    if a.switch:
        nxt = set_schedule(settings, store, a.flow, a.switch == "on")
        print(f"{a.flow}: {a.switch}" + (f", next at {nxt}" if a.switch == "on" else ""))
    register(settings, store)  # a scheduled flow seen for the first time is listed, switched off
    for row in store.q("SELECT * FROM schedules ORDER BY flow"):
        print(f"{row['flow']:<20} {'on ' if row['enabled'] else 'off'}  next {row['next_due']}")
    return 0


def cmd_serve(a, settings, store) -> int:
    import uvicorn

    from .web import create_app
    if a.host not in ("127.0.0.1", "localhost"):
        print("there is no sign-in: anyone who can reach this address can start runs and read every work folder. In a container, publish the port on 127.0.0.1 only.")
    print(f"agentflow on http://{a.host}:{a.port}{STAND_IN if settings.stand_in else ''}")
    uvicorn.run(create_app(settings, store, hosts=("127.0.0.1", "localhost", a.host)), host=a.host, port=a.port, log_level="warning")
    return 0


def cmd_show(a, settings, store) -> int:
    if a.run is None:
        return print_runs(store)
    if a.follow:
        follow(store, a.run)
    return print_run(store, a.run, a.item)


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agentflow")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def command(name: str, fn, model: bool = False) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        p.set_defaults(fn=fn, watch=model and name != "serve")
        if model:
            p.add_argument("--stand-in", action="store_true", help="use the scripted model; needs no key and costs nothing")
        return p

    command("check", cmd_check)
    p = command("try", cmd_try, model=True)
    p.add_argument("flow")
    p.add_argument("--item")
    p = command("run", cmd_run, model=True)
    p.add_argument("flow")
    p.add_argument("--force", action="store_true", help="triage every item again, even unchanged ones")
    command("resume", cmd_resume, model=True).add_argument("run")
    p = command("retry", cmd_retry, model=True)
    p.add_argument("run")
    p.add_argument("item")
    command("cancel", cmd_cancel).add_argument("run")
    p = command("schedule", cmd_schedule)
    p.add_argument("switch", nargs="?", choices=["on", "off"])
    p.add_argument("flow", nargs="?")
    p = command("serve", cmd_serve, model=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8321)
    p = command("show", cmd_show)
    p.add_argument("run", nargs="?")
    p.add_argument("item", nargs="?")
    p.add_argument("--follow", action="store_true", help="print the run's events until it ends, then the summary")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    settings = Settings()
    if a.fn is cmd_check:
        return cmd_check(a, settings)  # needs no store, so it can run before anything was ever written
    store = Store(settings.data)
    settings.stand_in = getattr(a, "stand_in", False)
    if a.watch:
        store.listeners.append(live_printer)  # one line per thing worth watching, as the run goes
    try:
        return a.fn(a, settings, store)
    except (FetchFailed, ModelNotReady, ValueError) as e:
        print(f"stopped: {e}")
        return 2
    except KeyboardInterrupt:
        print("\nstopped")
        return 130

