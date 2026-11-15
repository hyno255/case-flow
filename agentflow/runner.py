"""The run pipeline: fetch the flow's source, route the items the flow accepts to it, work them in parallel, render the report.
A run is always a run of one flow, started by a person or by the flow's own schedule. A source only says how items
are got and what it hands over; it starts nothing.
What happens to a run afterwards (resume, retry, cancel) is in recovery.py.
"""
import asyncio
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from .agents import build_model
from .layout import ITEMS, PLUGINS, new_fetch_folder, new_run_folder
from .plugins import Registry, accepts, installed, run_plugins
from .procs import run_proc
from .render import render_run
from .stages import Ctx, run_item
from .store import now

SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
HEARTBEAT = 5.0  # seconds between signs of life from a process doing a run


class FetchFailed(Exception):
    pass


async def fetch(settings, registry: Registry, source: str, out: Path) -> list[dict]:
    """Run the source's script and read what it handed over. The script is told its output folder in AGENTFLOW_OUT and writes
    the file source.yaml names as `output` there: a JSON list, every item with an 'id' that is safe as a folder name and
    with every field source.yaml lists under `fields`. Anything else is a failed fetch, so no flow gets half an item."""
    spec = registry.source(source)
    folder = registry.dir("sources", source)
    out.mkdir(parents=True)
    env = {**settings.secrets_for(f"sources/{source}"), "AGENTFLOW_OUT": str(out.resolve())}
    code, said = await run_proc([str((folder / spec["run"]).resolve())], cwd=folder, env=env)
    if code != 0:
        raise FetchFailed(f"{spec['run']} exited with code {code}: {said[-500:]}")
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


def fingerprint(item: dict) -> str:
    return hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:16]


# ---------- one run: create, work, finish ----------

def create_run(settings, store, live: Registry, flow: str, *, source, fetch_id, trial, fetched, **retry) -> tuple[str, Path]:
    for spec in live.models_used(flow):
        build_model(settings, spec)  # stop before any work if a model this flow uses is not usable
    run_id = ("t_" if trial else "r_") + uuid.uuid4().hex[:4]
    folder = new_run_folder(settings.data, run_id)  # everything this run writes goes in here
    (folder / ITEMS).mkdir(parents=True)
    digest = live.snapshot(flow, folder / PLUGINS)
    store.add_run(run_id, flow, source, fetch_id, digest, trial, folder, fetched, **retry)
    return run_id, folder


async def execute(settings, store, run_id: str, todo: list[tuple[dict, Path, str | None]]) -> str:
    """Work the given items of a run, then render its report and end it. `todo` is (item, work folder, stage to start at)."""
    run = store.run(run_id)
    snap = run_plugins(store, run)  # the plugin files exactly as the run first saw them
    flow, trial = run["flow"], bool(run["trial"])
    flow_def, graph = snap.flow(flow), snap.graph(flow)
    limits = settings.limits_for(flow_def)

    async def heart() -> None:
        while True:
            store.beat(run_id)
            await asyncio.sleep(HEARTBEAT)

    alive = asyncio.create_task(heart())
    gate = asyncio.Semaphore(limits["at_once"])

    async def one(item: dict, work: Path, start_at: str | None) -> None:
        async with gate:
            if not store.claim_item(run_id, item["id"]):
                return
            ctx = Ctx(settings, store, snap, flow, run_id, item, work, trial, limits, graph)
            if store.cancel_requested(run_id):
                store.end_item(run_id, item["id"], "cancelled", "the run was cancelled before this item started")
                return
            try:
                await run_item(ctx, start_at)
            except Exception as e:  # an engine fault ends this item, not the run
                store.end_item(run_id, item["id"], "failed", f"engine error: {type(e).__name__}: {str(e)[:300]}")

    try:
        await asyncio.gather(*(one(*t) for t in todo))
        await asyncio.to_thread(finish, store, run_id)  # rendering must not hold up other runs
    finally:
        alive.cancel()
    return run_id


def finish(store, run_id: str) -> None:
    """Render the report into the run folder and end the run. The report is the run's output; nothing is sent anywhere."""
    index, counts = render_run(store, run_id)
    store.emit(run_id, None, None, "report_rendered", path=str(index), **counts)
    store.end_run(run_id)
    store.emit(run_id, None, None, "run_ended")


async def run_flow(settings, store, live: Registry, flow: str, fetched: list[dict], *, source: str, fetch_id=None,
                   trial: bool = False, force: bool = False, fetch_seconds: float | None = None) -> str:
    """One run of one flow over the items of a fetch that the flow accepts. Returns the run id."""
    items = [i for i in fetched if accepts(live.flow(flow).get("where"), i)]
    run_id, folder = create_run(settings, store, live, flow, source=source, fetch_id=fetch_id, trial=trial, fetched=len(items))
    store.emit(run_id, None, None, "run_started", flow=flow, trial=trial, items=len(items), source=source, fetched=len(fetched),
               **({"fetch_seconds": fetch_seconds} if fetch_seconds is not None else {}))
    todo = []
    for item in items:
        fp = fingerprint(item)
        earlier = None if (trial or force) else store.earlier_item(flow, item["id"], fp)
        if earlier is not None:  # unchanged and already triaged: point at the earlier result, spend nothing
            store.add_item(run_id, flow, item["id"], fp, None, carried=earlier, payload=item)
            store.emit(run_id, item["id"], None, "item_carried_over", from_run=earlier["run_id"])
        else:
            work = folder / ITEMS / item["id"]
            work.mkdir()
            store.add_item(run_id, flow, item["id"], fp, work, payload=item)
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
    except FetchFailed as e:
        store.db.execute("UPDATE fetches SET ended = ?, outcome = 'failed', detail = ? WHERE id = ?", (now(), str(e), cur.lastrowid))
        raise
    store.db.execute("UPDATE fetches SET ended = ?, outcome = 'ok', item_count = ? WHERE id = ?", (now(), len(items), cur.lastrowid))
    return cur.lastrowid, items, round(time.time() - began, 3)


def checked(live: Registry, flow: str) -> str:
    """The flow's source, or ValueError when the flow's files do not pass their checks."""
    errs = live.check_flow(flow)
    if errs:
        raise ValueError("the flow does not pass its checks: " + "; ".join(errs))
    return live.flow(flow)["source"]


async def start_flow(settings, store, flow: str, *, force: bool = False) -> str:
    """A person starts a flow: fetch its source now and run this flow, and only this flow, over what it accepts.
    `force` works every item again, also the ones already triaged and unchanged. Returns the run id."""
    live = installed(settings)
    source = checked(live, flow)
    fetch_id, items, took = await fetch_once(settings, store, live, source)
    return await run_flow(settings, store, live, flow, items, source=source, fetch_id=fetch_id, force=force, fetch_seconds=took)


async def run_due(settings, store, source: str, flows: list[str], slot: str) -> list[str]:
    """The schedules of these flows came due in the same slot and they accept the same source: fetch it once and
    start one run per flow. A slot that was already fetched is not fetched again."""
    live = installed(settings)
    got = await fetch_once(settings, store, live, source, slot)
    if got is None:
        return []
    fetch_id, items, took = got
    return list(await asyncio.gather(*(run_flow(settings, store, live, flow, items, source=source, fetch_id=fetch_id, fetch_seconds=took)
                                       for flow in flows)))


async def try_flow(settings, store, flow: str, item_id: str | None = None) -> str:
    """A trial: one item the flow accepts (the named one, else the first), real stages, own folder, no granted secrets
    for its stages, and not counted as triaged."""
    live = installed(settings)
    source = checked(live, flow)
    fetch_id, fetched, took = await fetch_once(settings, store, live, source, by="trial")
    items = [i for i in fetched if accepts(live.flow(flow).get("where"), i) and item_id in (None, i["id"])][:1]
    if not items:
        raise FetchFailed(f"the source '{source}' has no item '{item_id}' that {flow} accepts" if item_id else f"the source '{source}' has no item that {flow} accepts")
    return await run_flow(settings, store, live, flow, items, source=source, fetch_id=fetch_id, trial=True, fetch_seconds=took)
