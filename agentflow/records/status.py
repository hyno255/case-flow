"""The state of a run, an item and a try, and the words for each.

Worked out from stored facts in this one place, so the command line, the pages and the trace
cannot say different things about the same run.
"""
import time

# name: (label, pill on a page, bar in the trace)
ENDINGS = {  # how an item can end
    "done": ("done", "done", "ok"), "needs_person": ("needs a person", "hold", "hold"), "failed": ("failed", "bad", "bad"),
    "timed_out": ("timed out", "bad", "bad"), "over_budget": ("stopped at cost cap", "bad", "bad"),
    "cancelled": ("cancelled", "wait", "cut")}
OUTCOMES = {  # how a try can end; "done" means the stage's work is done and its validation is not
    "passed": ("passed", "done", "ok"), "rejected": ("not accepted", "hold", "fail"), "asked_for_person": ("asked for a person", "hold", "hold"),
    "error": ("failed", "bad", "bad"), "interrupted": ("interrupted", "bad", "cut"), "timed_out": ("timed out", "bad", "cut"),
    "cancelled": ("cancelled", "wait", "cut"), "done": ("validating", "run", "")}


def label(table: dict, name: str | None) -> str:
    return table[name][0] if name in table else (name or "").replace("_", " ")


def is_stale(run, stale_after: float) -> bool:
    """True when a run has not ended and its process has given no sign of life for `stale_after` seconds
    (engine.stale_after_seconds in the settings; generous on purpose)."""
    return run["ended"] is None and time.time() - run["beat"] > stale_after


def run_status(run, stale_after: float) -> tuple[str, str]:
    """(label, pill). From three facts: an ending, a cancel request, and the last sign of life."""
    if run["ended"]:
        return ("cancelled", "wait") if run["cancel_requested"] else ("finished", "done")
    if is_stale(run, stale_after):
        return "interrupted", "bad"
    return ("stopping", "hold") if run["cancel_requested"] else ("running", "run")


def item_status(run, item, stale_after: float) -> tuple[str, str]:
    """(label, pill) for one item of a run."""
    if item["ended_as"]:
        return label(ENDINGS, item["ended_as"]), ENDINGS.get(item["ended_as"], ("", "wait"))[1]
    if is_stale(run, stale_after):
        return "interrupted", "bad"
    return ("running", "run") if item["claimed_at"] else ("queued", "wait")


def cost_text(u: dict, est: str = "", unknown: str = "unknown") -> str:
    """The cost of a usage total from Store.usage. Unknown is not zero: a try that was cut short used the model, but how much is not known."""
    if not u["has_model_calls"]:
        return "no model"
    if u["cost_known"]:
        return f"${u['cost_usd']:.4f}{est}" if u["cost_usd"] else "$0"  # nothing was charged, which is known, not estimated
    return f"${u['cost_usd']:.4f}{est}, without a try that was cut short" if u["cost_usd"] else unknown
