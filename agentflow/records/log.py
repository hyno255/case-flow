"""A run in plain lines: one line per thing worth reading, as it happens.

The command line prints these lines while it watches a run, and the engine appends them to the run's log file,
`output/run.log`, which the run's Output tab lists. Nothing follows a run live but these two.
"""
import json
import time
from pathlib import Path

from agentflow.records.layout import OUTPUT

LOG = "run.log"


def line(event: dict, chars: int) -> str | None:
    """The line for one event, or None for an event that has none. `chars` is how much of a value a line shows."""
    t, who = event["type"], f"{event.get('item') or '':<9} {event.get('stage') or '':<8}"
    if t == "run_started":
        return f"run       started: {event['flow']}, {event['items']} items" + (", trial" if event.get("trial") else "")
    if t == "run_ended":
        return "run       ended"
    if t == "attempt_started":
        return f"{who} try {event['n']}  {event['by']}" + ("  (with feedback)" if event.get("feedback") else "")
    elif t == "tool_call":
        args = event["args"] if isinstance(event["args"], str) else json.dumps(event["args"])
        return f"{who}   {'reviewer ' if event.get('role') else ''}tool {event['tool']} {args[:chars]}"
    elif t == "cli_run":
        return f"{who}   the {event['harness']} CLI ended after {event['seconds']:.0f}s: {event['said'][:chars]}"
    elif t == "attempt_ended" and event.get("error"):
        return f"{who}   the try stopped: {event['error'][:chars]}"
    elif t == "verdict":
        return f"{who}   {event['by']}: {'pass' if event['passed'] else 'FAIL'}  {event['reason'][:chars]}"
    elif t == "agent_failed":
        return f"{who}   {event['by']} could not do it: {event['reason'][:chars]}"
    elif t == "item_retried":
        return f"{event.get('item') or '':<9} retried"
    elif t == "attempt_cancelled":
        return f"{who} try {event['n']} was cancelled: {event['script']} was stopped"
    elif t == "attempt_interrupted":
        return f"{who} try {event['n']} was interrupted: the process stopped"
    elif t == "run_interrupted":
        return f"run       interrupted: {event['reason']}"
    elif t == "run_retried":
        return f"run       retried: {', '.join(event['items']) or 'the stages done once for the run'}"
    elif t == "run_resumed":
        return f"run       resumed for {', '.join(event['items']) or 'no items'}"
    elif t == "item_started" and event.get("from_stage"):
        return f"{event['item']:<9} continues at {event['from_stage']}; earlier stages are kept"
    elif t == "item_ended":
        return f"{event['item']:<9} ended: {event['ended_as']}" + (f"  ({event['reason'][:chars]})" if event.get("reason") else "")
    return None


def writer(store, chars: int):
    """A listener for the store: appends every event's line to its run's log file."""
    files: dict[str, Path] = {}

    def write(event: dict) -> None:
        said = line(event, chars)
        if said is None:
            return
        if event["run"] not in files:
            files[event["run"]] = store.path(store.run(event["run"])) / OUTPUT / LOG
        with files[event["run"]].open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S', time.localtime(event['ts']))}  {said}\n")

    return write
