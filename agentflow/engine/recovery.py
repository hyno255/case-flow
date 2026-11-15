"""What happens to a run after it started: the watchdog records that its process is gone, and a person can
resume it or cancel it. Both stay inside that run: no run reads or continues another run's work.

Nothing here resumes or cleans up by itself. The watchdog only records; a person (or a command) decides.
"""
import json
import time

from agentflow.workers.agents import check_ready
from agentflow.plugins.registry import run_plugins
from agentflow.engine.runner import execute, finish


def sweep(store, stale_after: float) -> list[str]:
    """The watchdog: record runs whose process is gone, which is how a run silent for `stale_after` seconds is read
    (engine.stale_after_seconds in the settings). It records and nothing more: no resume, no clean-up.
    One conditional update per run, so two watchdogs cannot both record the same interruption."""
    marked = []
    limit = time.time() - stale_after
    for run in store.q("SELECT id FROM runs WHERE ended IS NULL AND interrupted_at IS NULL AND beat < ?", limit):
        won = store.db.execute("UPDATE runs SET interrupted_at = ? WHERE id = ? AND ended IS NULL AND interrupted_at IS NULL AND beat < ?",
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

def claim_resume(store, run_id: str, stale_after: float) -> None:
    """Take over an interrupted run. Raises ValueError if it is finished or still alive. One conditional
    update decides it, so two people pressing Resume cannot both get the run."""
    if store.need_run(run_id)["ended"]:
        raise ValueError(f"run {run_id} has finished; there is nothing to resume")
    sweep(store, stale_after)
    won = store.db.execute("UPDATE runs SET beat = ?, interrupted_at = NULL WHERE id = ? AND ended IS NULL AND beat < ?",
                           (time.time(), run_id, time.time() - stale_after)).rowcount
    if not won:
        raise ValueError(f"run {run_id} is still running; it gave a sign of life less than {stale_after:.0f} seconds ago")


async def resume_run(settings, store, run_id: str, *, claimed: bool = False, stale_after: float | None = None) -> str:
    """Continue an interrupted run. An item that had ended is left alone. An unfinished item is put back on the
    flow's graph where its latest try left it, in its own folder, with the reasons from any earlier rejected tries."""
    if not claimed:
        claim_resume(store, run_id, settings.engine["stale_after_seconds"] if stale_after is None else stale_after)
    run = store.run(run_id)
    snap = run_plugins(settings, store, run)
    for spec in snap.models_used(run["flow"]):
        check_ready(settings, spec)  # stop before any work if a model this run uses is not usable
    graph = snap.graph(run["flow"])
    todo = []
    for it in store.items(run_id, open_only=True):
        start = graph.position(*store.last_try(run_id, it["item_id"]))
        todo.append((json.loads(it["payload"]), store.path(it), start if start != graph.start else None))
    store.db.execute("UPDATE items SET claimed_at = NULL WHERE run_id = ? AND ended_at IS NULL", (run_id,))
    store.emit(run_id, None, None, "run_resumed", items=[t[0]["id"] for t in todo])
    return await execute(settings, store, run_id, todo)


def request_cancel(store, run_id: str, stale_after: float) -> str:
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
