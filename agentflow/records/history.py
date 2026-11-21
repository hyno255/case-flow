"""What earlier runs did, for a flow that asks. The engine never looks back by itself: a run works every item it is given.

A flow's own script calls `agentflow history`. An agent gets the same through the tool plugins `history` and `history-file`.
"""
from agentflow.records.layout import ALL, OUTPUT, confined, visible_files


def earlier(store, flow: str, item_id: str | None, limit: int, not_run: str | None = None) -> list[dict]:
    """How items of a flow ended in its runs, newest run first: one row per item of a run, with where its files are.
    `item_id` narrows it to one item; `not_run` leaves out the run that is asking."""
    rows = []
    for r in store.q("""SELECT i.*, r.started, r.trial, r.folder AS run_folder FROM items i JOIN runs r ON r.id = i.run_id
                        WHERE i.flow = ? AND i.item_id != ? AND i.ended_at IS NOT NULL AND (? IS NULL OR i.item_id = ?) AND (? IS NULL OR i.run_id != ?)
                        ORDER BY r.started DESC, r.rowid DESC, i.item_id LIMIT ?""", flow, ALL, item_id, item_id, not_run, not_run, limit):
        folder = store.path(r)
        rows.append({"run": r["run_id"], "started": r["started"], "trial": bool(r["trial"]), "item": r["item_id"], "ended_as": r["ended_as"], "reason": r["reason"],
                     "folder": str(folder.resolve()), "files": visible_files(folder),
                     "output": str((store.root / r["run_folder"] / OUTPUT).resolve())})
    return rows


def earlier_file(store, run_id: str, item_id: str, path: str, chars: int) -> str:
    """One file of an item's work folder in an earlier run: its first `chars` characters."""
    it = store.item(run_id, item_id)
    if it is None:
        raise ValueError(f"run '{run_id}' has no item '{item_id}'")
    target = confined(store.path(it), path)
    if not target.is_file():
        raise ValueError(f"{item_id} in {run_id} has no file '{path}'")
    return target.read_text(errors="replace")[:chars]
