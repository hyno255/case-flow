"""Plain-language schedules: 'weekdays 07:00', 'daily 07:00', 'every 10 minutes', 'every minute', 'every hour'.
A schedule belongs to a flow: it says when the engine fetches the flow's source for it. Here: which flows are due,
and switching a flow's schedule on or off."""
import re
from datetime import datetime, timedelta


def parse_schedule(spec: str) -> tuple:
    s = spec.strip().lower()
    m = re.fullmatch(r"(weekdays|daily) (\d{1,2}):(\d{2})", s)
    if m and int(m[2]) < 24 and int(m[3]) < 60:
        return (m[1], int(m[2]), int(m[3]))
    m = re.fullmatch(r"every (\d+) minutes?", s)
    if m and int(m[1]) > 0:
        return ("every", int(m[1]))
    if s == "every minute":
        return ("every", 1)
    if s == "every hour":
        return ("every", 60)
    raise ValueError(f"schedule '{spec}' is not understood; use 'weekdays 07:00', 'daily 07:00' or 'every 10 minutes'")


def next_due(spec: str, after: datetime) -> datetime:
    """The first scheduled time strictly after `after`."""
    kind, *rest = parse_schedule(spec)
    if kind == "every":
        return after.replace(second=0, microsecond=0) + timedelta(minutes=rest[0])
    hour, minute = rest
    day = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day <= after:
        day += timedelta(days=1)
    while kind == "weekdays" and day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def register(settings, store, when: datetime | None = None) -> dict[str, tuple[str, str]]:
    """Bring the stored schedules in line with the flow files, and return flow -> (its schedule, its source) for every flow
    that has a schedule and passes its checks. A flow seen for the first time is switched off; a flow whose schedule was
    edited gets its next slot from the new one; a flow that is gone is forgotten."""
    from agentflow.plugins.registry import installed  # the registry checks schedules with parse_schedule, so it imports this module
    when = when or datetime.now()
    live, found = installed(settings), {}
    for flow in live.names("flows"):
        try:
            if not live.check_flow(flow) and live.flow(flow).get("schedule"):
                found[flow] = (str(live.flow(flow)["schedule"]), live.flow(flow)["source"])
        except Exception:  # a flow whose files cannot be read is not started; its page says why
            continue
    for flow, (spec, _) in found.items():
        row = store.one("SELECT * FROM schedules WHERE flow = ?", flow)
        if row is None:
            store.db.execute("INSERT INTO schedules (flow, spec, enabled, next_due) VALUES (?,?,0,?)", (flow, spec, next_due(spec, when).isoformat()))
        elif row["spec"] != spec:
            store.db.execute("UPDATE schedules SET spec = ?, next_due = ? WHERE flow = ?", (spec, next_due(spec, when).isoformat(), flow))
    names = live.names("flows")
    store.db.execute(f"DELETE FROM schedules WHERE flow NOT IN ({','.join('?' * len(names))})", names)
    return found


def tick(settings, store, when: datetime | None = None) -> list[tuple[str, str, str]]:
    """Which flows are due now: (flow, its source, the slot). Missed slots are coalesced into one.
    Flows that share a source and a slot are fetched for once; see runner.run_due."""
    when = when or datetime.now()
    due = []
    for flow, (spec, source) in register(settings, store, when).items():
        row = store.one("SELECT * FROM schedules WHERE flow = ?", flow)
        if row["enabled"] and datetime.fromisoformat(row["next_due"]) <= when:
            # one conditional update: only the tick that moves next_due forward gets to start the flow
            moved = store.db.execute("UPDATE schedules SET next_due = ? WHERE flow = ? AND next_due = ?",
                                     (next_due(spec, when).isoformat(), flow, row["next_due"])).rowcount
            if moved:
                due.append((flow, source, row["next_due"]))
    return due


def set_schedule(settings, store, flow: str, enabled: bool) -> str:
    known = register(settings, store)
    if flow not in known:
        raise ValueError(f"flow '{flow}' has no schedule, or does not pass its checks")
    nxt = next_due(known[flow][0], datetime.now()).isoformat()
    store.db.execute("UPDATE schedules SET enabled = ?, next_due = ? WHERE flow = ?", (int(enabled), nxt, flow))
    return nxt
