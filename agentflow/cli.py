"""The command line.

    agentflow check                              check every plugin file and the settings, and run every tool's health check
    agentflow try <flow> [--item ID]             one item, real stages, as a trial
    agentflow run <flow>                         fetch the flow's source now and run the flow over every item it accepts
    agentflow schedule [on|off <flow>]           show the flows' schedules, or switch one
    agentflow serve [--port 8321]                the web UI, the API and the schedules, until stopped
    agentflow show                               recent runs
    agentflow show <run> [item] [--follow]       what a run or one item did and cost; --follow prints its events until it ends
    agentflow resume <run>                       continue a run whose process stopped; finished stages are kept
    agentflow cancel <run>                       stop a run: running scripts now, agents after their try
    agentflow history [flow] [--item ID]         for a flow's own scripts: how items ended in earlier runs and where their files are, as JSON
    agentflow history --run RUN --item ID --file PATH    one file of an item's work folder in an earlier run

try, run, resume and serve take --stand-in: the scripted model, which needs no key and costs nothing.
"""
import argparse
import asyncio
import json
import os
import time

from agentflow.records import log
from agentflow.records.history import earlier, earlier_file
from agentflow.records.layout import OUTPUT
from agentflow.plugins.registry import installed
from agentflow.settings import Settings
from agentflow.records.status import cost_text, item_status, run_status
from agentflow.records.store import Store


def money(u: dict) -> str:
    return cost_text(u, " estimated", "cost unknown")


def printer(settings):
    """Prints the line of each event that has one, as it happens."""
    def show(event: dict) -> None:
        said = log.line(event, settings.internal["line_chars"])
        if said is not None:
            print(said, flush=True)
    return show


def print_run(settings, store: Store, run_id: str, item_id: str | None = None) -> int:
    run, stale, line = store.need_run(run_id), settings.internal["stale_after_seconds"], settings.internal["line_chars"]
    u = store.usage(run_id)
    print(f"\n{'trial' if run['trial'] else 'run'} {run_id} · {run['flow']} · {run_status(run, stale)[0]} · plugins {run['snapshot']} · {u['tokens']:,} tokens · {money(u)}")
    for it in store.items(run_id):
        if item_id and it["item_id"] != item_id:
            continue
        iu = store.usage(run_id, it["item_id"])
        extra = f"{iu['tool_calls']} tool calls · {iu['tokens']:,} tokens · {money(iu)} · {iu['seconds']:.0f}s"
        print(f"\n{it['item_id']:<9} {item_status(run, it, stale)[0]:<15} {extra}")
        if it["reason"]:
            print(f"{'':<9} {it['reason'][:line]}")
        for s in store.q("SELECT stage, MIN(by) by, COUNT(*) tries FROM attempts WHERE run_id = ? AND item_id = ? GROUP BY stage ORDER BY MIN(id)", run_id, it["item_id"]):
            su = store.usage(run_id, it["item_id"], s["stage"])
            print(f"  {s['stage']:<8} {s['by']:<15} tries {s['tries']}  {su['tool_calls']:>2} tool calls  {su['tokens']:>7,} tokens  {money(su):<18} {su['seconds']:.1f}s")
    print(f"\nfolder    {store.path(run)}\noutput    {store.path(run) / OUTPUT}")
    return 0


def print_runs(settings, store: Store) -> int:
    for run in store.q("SELECT * FROM runs ORDER BY started DESC, rowid DESC LIMIT ?", settings.internal["rows"]):
        ends = store.q("SELECT COALESCE(ended_as,'not finished') e, COUNT(*) n FROM items WHERE run_id = ? GROUP BY e", run["id"])
        print(f"{run['id']}  {run['flow']:<16} {run['started']}  {'trial · ' if run['trial'] else ''}{run_status(run, settings.internal['stale_after_seconds'])[0]} · "
              f"{', '.join(f'{x['n']} {x['e']}' for x in ends)} · {money(store.usage(run['id']))}")
    return 0


def follow(settings, store: Store, run_id: str) -> None:
    """Print a run's events from the first one, then as they come, until the run ends. The same events the pages stream."""
    store.need_run(run_id)
    seq, show = 0, printer(settings)
    while True:
        events = store.events(run_id, after=seq)
        for e in events:
            seq = e["seq"]
            show(e)
        if any(e["type"] == "run_ended" for e in events) or (not events and store.run(run_id)["ended"]):
            return
        time.sleep(settings.internal["follow_seconds"])


def cmd_check(a, settings: Settings) -> int:
    from agentflow.workers.harness import CLIS, cli_version
    from agentflow.plugins.health import tool_health
    bad, live = 0, installed(settings)
    for kind, name, errs, summary in live.check_all():
        bad += len(errs)
        print(f"{kind:<8} {name:<20} {'ok   ' if not errs else 'ERROR'}  {summary}")
        for e in errs:
            print(f"{'':<8} {e}")
    for name in live.names("tools"):
        ok, detail = asyncio.run(tool_health(settings, live, name))
        bad += not ok
        print(f"health   {name:<20} {'ok   ' if ok else 'FAIL '}  {detail}")
    print()
    for name in settings.models:
        m = settings.model(name)
        how = (f"{m['harness']} CLI · {m.get('name')} · {'key in ' + m['api_key_env'] if m.get('api_key_env') else 'its own sign-in'}" if "harness" in m else
               f"{m['provider']} · {m['name']} · api {m['api']} · reasoning {m['reasoning']} · key in {m['api_key_env']}")
        print(f"model    {name}{' (default)' if name == settings.default_model else ''}: {how}")
    for name in sorted({h for h in map(settings.harness_of, settings.models) if h in CLIS}):  # each CLI a model entry names: is it here?
        ok, detail = asyncio.run(cli_version(settings, name))
        bad += not ok
        print(f"harness  {name:<20} {'ok   ' if ok else 'FAIL '}  {detail}")
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


# The commands below load the engine themselves, so the ones a script calls often (history, show) start fast.

def cmd_try(a, settings, store) -> int:
    from agentflow.engine.runner import try_flow
    print(f"trial · one item · {flow_model(settings, a.flow)}\n")
    return print_run(settings, store, asyncio.run(try_flow(settings, store, a.flow, a.item)))


def cmd_run(a, settings, store) -> int:
    from agentflow.engine.runner import start_flow
    print(f"{a.flow} · {flow_model(settings, a.flow)}\n")
    return print_run(settings, store, asyncio.run(start_flow(settings, store, a.flow)))


def cmd_resume(a, settings, store) -> int:
    from agentflow.engine.recovery import resume_run
    print(f"resuming {a.run} · finished stages are kept{STAND_IN if settings.stand_in else ''}\n")
    return print_run(settings, store, asyncio.run(resume_run(settings, store, a.run)))


def cmd_cancel(a, settings, store) -> int:
    from agentflow.engine.recovery import request_cancel
    how = request_cancel(store, a.run, settings.internal["stale_after_seconds"])
    print(f"{a.run}: " + ("asked to stop; running scripts are stopped now, and an agent finishes its try first" if how == "asked" else "closed; its process had already stopped"))
    return 0


def cmd_schedule(a, settings, store) -> int:
    from agentflow.engine.schedule import register, set_schedule
    if a.switch:
        nxt = set_schedule(settings, store, a.flow, a.switch == "on")
        print(f"{a.flow}: {a.switch}" + (f", next at {nxt}" if a.switch == "on" else ""))
    register(settings, store)  # a scheduled flow seen for the first time is listed, switched off
    for row in store.q("SELECT * FROM schedules ORDER BY flow"):
        print(f"{row['flow']:<20} {'on ' if row['enabled'] else 'off'}  next {row['next_due']}")
    return 0


def cmd_serve(a, settings, store) -> int:
    import uvicorn

    from agentflow.web import create_app
    if a.host not in ("127.0.0.1", "localhost"):
        print("there is no sign-in: anyone who can reach this address can start runs and read every work folder. In a container, publish the port on 127.0.0.1 only.")
    print(f"agentflow on http://{a.host}:{a.port}{STAND_IN if settings.stand_in else ''}")
    uvicorn.run(create_app(settings, store, hosts=("127.0.0.1", "localhost", a.host)), host=a.host, port=a.port, log_level="warning")
    return 0


def cmd_show(a, settings, store) -> int:
    if a.run is None:
        return print_runs(settings, store)
    if a.follow:
        follow(settings, store, a.run)
    return print_run(settings, store, a.run, a.item)


def cmd_history(a, settings, store) -> int:
    """Called by a flow's script, which the engine tells its flow and its run; the asking run is left out of the answer."""
    if a.file:
        if not (a.run and a.item):
            raise ValueError("--file needs --run and --item")
        print(earlier_file(store, a.run, a.item, a.file, settings.internal["read_chars"]))
        return 0
    flow = a.flow or os.environ.get("AGENTFLOW_FLOW")
    if not flow:
        raise ValueError("name the flow: agentflow history <flow>")
    print(json.dumps(earlier(store, flow, a.item, settings.internal["rows"], not_run=os.environ.get("AGENTFLOW_RUN")), indent=1))
    return 0


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
    command("run", cmd_run, model=True).add_argument("flow")
    command("resume", cmd_resume, model=True).add_argument("run")
    command("cancel", cmd_cancel).add_argument("run")
    p = command("schedule", cmd_schedule)
    p.add_argument("switch", nargs="?", choices=["on", "off"])
    p.add_argument("flow", nargs="?")
    p = command("serve", cmd_serve, model=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8321)
    p = command("history", cmd_history)
    p.add_argument("flow", nargs="?")
    p.add_argument("--item")
    p.add_argument("--run")
    p.add_argument("--file")
    p = command("show", cmd_show)
    p.add_argument("run", nargs="?")
    p.add_argument("item", nargs="?")
    p.add_argument("--follow", action="store_true", help="print the run's events until it ends, then the summary")
    return ap


def stops() -> tuple:
    """The errors that end a command with one line. Looked up only when a command fails."""
    from agentflow.workers.agents import ModelNotReady
    from agentflow.engine.runner import FetchFailed
    return FetchFailed, ModelNotReady, ValueError


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    settings = Settings()
    if a.fn is cmd_check:
        return cmd_check(a, settings)  # needs no store, so it can run before anything was ever written
    store = Store(settings.data)
    store.listeners.append(log.writer(store, settings.internal["line_chars"]))  # every run keeps its lines in its output folder
    settings.stand_in = getattr(a, "stand_in", False)
    if a.watch:
        store.listeners.append(printer(settings))  # one line per thing worth watching, as the run goes
    try:
        return a.fn(a, settings, store)
    except stops() as e:
        print(f"stopped: {e}")
        return 2
    except KeyboardInterrupt:
        print("\nstopped")
        return 130

