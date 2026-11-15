"""The run pipeline: fetch the flow's source, route the items the flow accepts to it, work them in parallel, run the flow's finish script.
A run is always a run of one flow, started by a person or by the flow's own schedule. A source only says how items
are got and what it hands over; it starts nothing.
What happens to a run afterwards (resume, cancel) is in recovery.py.
"""
import asyncio
import json
import re
import time
import uuid
from pathlib import Path

from agentflow.workers.agents import check_ready
from agentflow.records.layout import ITEMS, OUTPUT, PLUGINS, new_fetch_folder, new_run_folder
from agentflow.plugins.registry import Registry, accepts, installed, run_plugins
from agentflow.workers.procs import run_proc
from agentflow.engine.stages import Ctx, run_item, run_script, run_told
from agentflow.records.store import now

SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class FetchFailed(Exception):
    pass


async def fetch(settings, registry: Registry, source: str, out: Path) -> list[dict]:
    """Run the source's script and read what it handed over. The script is told its output folder in AGENTFLOW_OUT and writes
    the file source.yaml names as `output` there: a JSON list, every item with an 'id' that is safe as a folder name and
    with every field source.yaml lists under `fields`. Anything else is a failed fetch, so no flow gets half an item."""
    spec = registry.source(source)
    folder = registry.dir("sources", source)
    out.mkdir(parents=True)
    env = {"AGENTFLOW_HOME": str(settings.home), "AGENTFLOW_OUT": str(out.resolve())}
    code, said = await run_proc([str((folder / spec["run"]).resolve())], cwd=folder, env=env, **settings.proc())
    if code != 0:
        raise FetchFailed(f"{spec['run']} exited with code {code}: {said[-settings.limits['error_chars']:]}")
    if not (out / spec["output"]).is_file():
        raise FetchFailed(f"{spec['run']} did not write {spec['output']} into its output folder (AGENTFLOW_OUT)")
    try:
        items = json.loads((out / spec["output"]).read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise FetchFailed(f"{spec['output']} is not JSON: {e}")
    if not isinstance(items, list) or not all(isinstance(i, dict) and isinstance(i.get("id"), str) for i in items):
        raise FetchFailed(f"{spec['output']} must hold a JSON list of objects, each with a text 'id'")
    bad = [i["id"] for i in items if not SAFE_ID.fullmatch(i["id"])]
    if bad:
        raise FetchFailed(f"item ids must be letters, digits, '.', '_' or '-': {bad[:3]}")
    if len({i["id"] for i in items}) != len(items):
        raise FetchFailed(f"{spec['output']} holds the same id twice")
    for field in spec.get("fields") or []:
        lacking = [i["id"] for i in items if field not in i]
        if lacking:
            raise FetchFailed(f"source.yaml lists the field '{field}', but {len(lacking)} items do not have it: {lacking[:3]}")
    return items


# ---------- one run: create, work, finish ----------

def create_run(settings, store, live: Registry, flow: str, *, source, fetch_id, trial) -> tuple[str, Path]:
    if settings.stand_in and "stand_in" not in live.flow(flow):
        raise ValueError(f"{flow} has no stand-in: name the script that answers in the model's place as `stand_in` in its flow.yaml")
    for spec in live.models_used(flow):
        check_ready(settings, spec)  # stop before any work if a model this flow uses is not usable
    run_id = None
    while run_id is None or store.run(run_id):  # short ids are easy to read and say; one that is taken is drawn again
        run_id = ("t_" if trial else "r_") + uuid.uuid4().hex[:4]
    folder = new_run_folder(settings.data, run_id)  # everything this run writes goes in here
    (folder / ITEMS).mkdir(parents=True)
    (folder / OUTPUT).mkdir()  # the flow's scripts fill it; the engine writes nothing there
    digest = live.snapshot(flow, folder / PLUGINS)
    store.add_run(run_id, flow, source, fetch_id, digest, trial, folder)
    return run_id, folder


async def execute(settings, store, run_id: str, todo: list[tuple[dict, Path, str | None]]) -> str:
    """Work the given items of a run, then run the flow's finish script and end the run. `todo` is (item, work folder, stage to start at)."""
    run = store.run(run_id)
    snap = run_plugins(settings, store, run)  # the plugin files exactly as the run first saw them
    flow, trial = run["flow"], bool(run["trial"])
    flow_def, graph = snap.flow(flow), snap.graph(flow)
    limits = settings.limits_for(flow_def)

    async def heart() -> None:
        while True:
            store.beat(run_id)
            await asyncio.sleep(settings.engine["heartbeat_seconds"])

    alive = asyncio.create_task(heart())
    gate = asyncio.Semaphore(limits["at_once"])

    async def one(item: dict, work: Path, start_at: str | None) -> None:
        async with gate:
            if not store.claim_item(run_id, item["id"]):
                return
            ctx = Ctx(settings, store, snap, flow, run_id, item, work, trial, limits, graph, store.path(run) / OUTPUT)
            if store.cancel_requested(run_id):
                store.end_item(run_id, item["id"], "cancelled", "the run was cancelled before this item started")
                return
            try:
                await run_item(ctx, start_at)
            except Exception as e:  # an engine fault ends this item, not the run
                store.end_item(run_id, item["id"], "failed", f"engine error: {type(e).__name__}: {str(e)[:limits['error_chars']]}")

    try:
        await asyncio.gather(*(one(*t) for t in todo))
        await run_finish(settings, store, snap, run, limits)
        finish(store, run_id)  # on the loop's own thread: the store's one connection is never used by two threads
    finally:
        alive.cancel()
    return run_id


async def run_finish(settings, store, snap: Registry, run, limits: dict) -> None:
    """The flow's own `finish` script, once, when every item of the run has ended. It is how a flow makes what the run
    puts out: a report, a summary, a message. It works in the run's output folder, and its input is the run and how
    each item ended, as JSON. What it writes, and whether it writes anything, is the flow's business. A cancelled run skips it."""
    script = snap.flow(run["flow"]).get("finish")
    if not script or store.cancel_requested(run["id"]):
        return
    flow_dir, out, trial = snap.dir("flows", run["flow"]), store.path(run) / OUTPUT, bool(run["trial"])
    told = {"run": run["id"], "flow": run["flow"], "trial": trial, "started": run["started"],
            "items": [{"id": it["item_id"], "ended_as": it["ended_as"], "reason": it["reason"], "folder": str(store.path(it).resolve()),
                       "item": json.loads(it["payload"])} for it in store.items(run["id"])]}
    code, said, seconds = await run_script(settings, store, run["id"], flow_dir / script, out, json.dumps(told),
                                           run_told(settings, flow_dir, run["flow"], run["id"], out, trial), limits)
    store.emit(run["id"], None, None, "finish_ended", script=script, exit_code=code, seconds=seconds, output=said[-limits["event_chars"]:])


def finish(store, run_id: str) -> None:
    """End the run."""
    store.end_run(run_id)
    store.emit(run_id, None, None, "run_ended")


async def run_flow(settings, store, live: Registry, flow: str, fetched: list[dict], *, source: str, fetch_id: int, fetch_seconds: float,
                   trial: bool = False) -> str:
    """One run of one flow over the items of a fetch that the flow accepts: every one of them is worked. What a run
    should leave out is the flow's own business: what its source hands over, and its `where`. Returns the run id."""
    where = live.flow(flow).get("where")
    items = [i for i in fetched if accepts(where, i)]
    run_id, folder = create_run(settings, store, live, flow, source=source, fetch_id=fetch_id, trial=trial)
    store.emit(run_id, None, None, "run_started", flow=flow, trial=trial, items=len(items), source=source, fetch_seconds=fetch_seconds)
    todo = []
    for item in items:
        work = folder / ITEMS / item["id"]
        work.mkdir()
        store.add_item(run_id, flow, item["id"], work, item)
        todo.append((item, work, None))
    return await execute(settings, store, run_id, todo)


async def fetch_once(settings, store, live: Registry, source: str, slot: str | None = None, by: str = "by hand") -> tuple[int, list[dict], float] | None:
    """One recorded fetch of a source, kept in its own folder: (fetch id, items, seconds). None when this schedule slot was already fetched."""
    slot = slot or f"{by} {now()} {uuid.uuid4().hex[:4]}"
    cur = store.db.execute("INSERT OR IGNORE INTO fetches (source, scheduled_for, started) VALUES (?,?,?)", (source, slot, now()))
    if cur.rowcount == 0:
        return None
    out = new_fetch_folder(settings.data, source, cur.lastrowid)
    store.db.execute("UPDATE fetches SET folder = ? WHERE id = ?", (str(out.relative_to(store.root)), cur.lastrowid))
    began = time.time()
    try:
        items = await fetch(settings, live, source, out)
    except BaseException as e:  # also a fetch cut short by a stop: its row must not stay open
        why = str(e) if isinstance(e, FetchFailed) else f"interrupted: {type(e).__name__}"
        store.db.execute("UPDATE fetches SET ended = ?, outcome = 'failed', detail = ? WHERE id = ?", (now(), why, cur.lastrowid))
        raise
    store.db.execute("UPDATE fetches SET ended = ?, outcome = 'ok', item_count = ? WHERE id = ?", (now(), len(items), cur.lastrowid))
    return cur.lastrowid, items, round(time.time() - began, 3)


def checked(live: Registry, flow: str) -> str:
    """The flow's source, or ValueError when the flow's files do not pass their checks."""
    errs = live.check_flow(flow)
    if errs:
        raise ValueError("the flow does not pass its checks: " + "; ".join(errs))
    return live.flow(flow)["source"]


async def start_flow(settings, store, flow: str) -> str:
    """A person starts a flow: fetch its source now and run this flow, and only this flow, over what it accepts.
    Returns the run id."""
    live = installed(settings)
    source = checked(live, flow)
    fetch_id, items, took = await fetch_once(settings, store, live, source)
    return await run_flow(settings, store, live, flow, items, source=source, fetch_id=fetch_id, fetch_seconds=took)


async def run_due(settings, store, source: str, flows: list[str], slot: str) -> list:
    """The schedules of these flows came due in the same slot and they accept the same source: fetch it once and
    start one run per flow. A slot that was already fetched is not fetched again. Returns, in the order of `flows`,
    each flow's run id, or the exception that kept that flow from starting; the others run all the same."""
    live = installed(settings)
    got = await fetch_once(settings, store, live, source, slot)
    if got is None:
        return []
    fetch_id, items, took = got
    return list(await asyncio.gather(*(run_flow(settings, store, live, flow, items, source=source, fetch_id=fetch_id, fetch_seconds=took)
                                       for flow in flows), return_exceptions=True))


async def try_flow(settings, store, flow: str, item_id: str | None = None) -> str:
    """A trial: one item the flow accepts (the named one, else the first), real stages, own folder.
    Its scripts are told it is a trial."""
    live = installed(settings)
    source = checked(live, flow)
    fetch_id, fetched, took = await fetch_once(settings, store, live, source, by="trial")
    where = live.flow(flow).get("where")
    items = [i for i in fetched if accepts(where, i) and item_id in (None, i["id"])][:1]
    if not items:
        raise FetchFailed(f"the source '{source}' has no item '{item_id}' that {flow} accepts" if item_id else f"the source '{source}' has no item that {flow} accepts")
    return await run_flow(settings, store, live, flow, items, source=source, fetch_id=fetch_id, trial=True, fetch_seconds=took)
