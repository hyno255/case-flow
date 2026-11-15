"""What happens to a run after it started: the watchdog records that its process is gone, and a person can
resume it, retry one item from a stage, or cancel it.

Nothing here resumes or cleans up by itself. The watchdog only records; a person (or a command) decides.
"""
import json
import shutil
import time
from pathlib import Path

from .agents import build_model
from .layout import ENGINE_OWNED, ITEMS
from .plugins import installed, run_plugins
from .runner import create_run, execute, finish
from .status import STALE_AFTER


def sweep(store, stale_after: float = STALE_AFTER) -> list[str]:
    """The watchdog: record runs whose process is gone. It records and nothing more: no resume, no clean-up.
    One conditional update per run, so two watchdogs cannot both record the same interruption."""
    marked = []
    limit = time.time() - stale_after
    for run in store.q("SELECT id FROM runs WHERE ended IS NULL AND interrupted_at IS NULL AND (beat IS NULL OR beat < ?)", limit):
        won = store.db.execute("UPDATE runs SET interrupted_at = ? WHERE id = ? AND ended IS NULL AND interrupted_at IS NULL AND (beat IS NULL OR beat < ?)",
                               (time.time(), run["id"], limit)).rowcount
        if won:
            cut = store.q("""SELECT a.item_id, a.stage, a.n FROM attempts a JOIN items i ON i.run_id = a.run_id AND i.item_id = a.item_id
                             WHERE a.run_id = ? AND i.ended_at IS NULL AND (a.ended IS NULL OR a.outcome = 'done')""", run["id"])
            store.close_open_attempts(run["id"], "interrupted")
            for a in cut:
                folder = store.path(store.item(run["id"], a["item_id"]))
                store.emit(run["id"], a["item_id"], a["stage"], "attempt_interrupted", folder=folder if folder.is_dir() else None, n=a["n"])
            store.emit(run["id"], None, None, "run_interrupted", reason="the process doing this run stopped")
            marked.append(run["id"])
    return marked


# ---------- what a person can do to a run ----------

def claim_resume(store, run_id: str, stale_after: float = STALE_AFTER) -> None:
    """Take over an interrupted run. Raises ValueError if it is finished or still alive. One conditional
    update decides it, so two people pressing Resume cannot both get the run."""
    if store.need_run(run_id)["ended"]:
        raise ValueError(f"run {run_id} has finished; there is nothing to resume")
    sweep(store, stale_after)
    won = store.db.execute("UPDATE runs SET beat = ?, interrupted_at = NULL WHERE id = ? AND ended IS NULL AND (beat IS NULL OR beat < ?)",
                           (time.time(), run_id, time.time() - stale_after)).rowcount
    if not won:
        raise ValueError(f"run {run_id} is still running; it gave a sign of life less than {stale_after:.0f} seconds ago")


async def resume_run(settings, store, run_id: str, *, claimed: bool = False, stale_after: float = STALE_AFTER) -> str:
    """Continue an interrupted run. An item that had ended is left alone. An unfinished item is put back on the
    flow's graph where its latest try left it, in its own folder, with the reasons from any earlier rejected tries."""
    if not claimed:
        claim_resume(store, run_id, stale_after)
    build_model(settings)
    run = store.run(run_id)
    graph = run_plugins(store, run).graph(run["flow"])
    todo = []
    for it in store.items(run_id, open_only=True):
        start = graph.position(*store.last_try(run_id, it["item_id"]))
        todo.append((json.loads(it["payload"]), store.path(it), start if start != graph.start else None))
    store.db.execute("UPDATE items SET claimed_at = NULL WHERE run_id = ? AND ended_at IS NULL", (run_id,))
    store.emit(run_id, None, None, "run_resumed", items=[t[0]["id"] for t in todo])
    return await execute(settings, store, run_id, todo)


def prepare_retry(settings, store, run_id: str, item_id: str) -> str:
    """A person re-runs one item after fixing a method or a script. The retry is a new run of that one item with
    the plugin files as they are now. It starts at the stage the item stopped at (the first stage, if it was
    done): its folder starts as a copy of the old one and the stages before that one are kept, not done again.
    The old run is not changed. Returns the new run id."""
    old = store.run(run_id)
    it = store.item(run_id, item_id) if old else None
    if it is None:
        raise ValueError(f"run '{run_id}' has no item '{item_id}'")
    if it["carried_from"]:
        raise ValueError(f"{item_id} was carried over from {it['carried_from']}; retry it there")
    if not it["ended_at"]:
        raise ValueError(f"{item_id} has not ended in {run_id}; resume or cancel the run first")
    live = installed(settings)
    errs = live.check_flow(old["flow"])
    if errs:
        raise ValueError("the flow does not pass its checks: " + "; ".join(errs))
    graph = live.graph(old["flow"])
    last, _ = store.last_try(run_id, item_id)
    from_stage = graph.start if it["ended_as"] == "done" else graph.position(last)  # a new run: the tries start afresh
    from_stage = graph.start if from_stage == "end" else from_stage
    kept = graph.before(from_stage)

    new_id, folder = create_run(settings, store, live, old["flow"], source=old["source"], fetch_id=None, trial=bool(old["trial"]), fetched=1,
                                 retry_of=run_id, retry_item=item_id, from_stage=from_stage)
    work, old_work = folder / ITEMS / item_id, store.path(it)
    if kept:  # the hand-over from the kept stages; the engine's own records of the old run stay with the old run
        shutil.copytree(old_work, work, ignore=lambda d, names: [n for n in names if Path(d) == old_work and n in ENGINE_OWNED])
    else:
        work.mkdir()
    store.add_item(new_id, old["flow"], item_id, it["fingerprint"], work, payload=json.loads(it["payload"]))
    store.emit(new_id, None, None, "run_started", flow=old["flow"], trial=bool(old["trial"]), items=1, retry_of=run_id, from_stage=from_stage)
    return new_id


async def run_retry(settings, store, new_run: str) -> str:
    run = store.run(new_run)
    (it,) = store.items(new_run)
    first = run_plugins(store, run).graph(run["flow"]).start
    return await execute(settings, store, new_run, [(json.loads(it["payload"]), store.path(it), run["from_stage"] if run["from_stage"] != first else None)])


def request_cancel(settings, store, run_id: str, stale_after: float = STALE_AFTER) -> str:
    """Ask a run to stop. A live run stops its running scripts at once and each item before its next stage; an agent finishes its try. A run whose process is gone is closed here.
    Folders are kept either way. Returns 'asked' or 'closed'."""
    if store.need_run(run_id)["ended"]:
        raise ValueError(f"run {run_id} has already finished")
    store.db.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ? AND ended IS NULL", (run_id,))
    store.emit(run_id, None, None, "cancel_requested")
    try:
        claim_resume(store, run_id, stale_after)  # only succeeds when no process is doing the run
    except ValueError:
        return "asked"
    for it in store.items(run_id, open_only=True):
        store.end_item(run_id, it["item_id"], "cancelled", "the run was cancelled after its process had stopped")
    finish(store, run_id)
    return "closed"
