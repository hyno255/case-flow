"""One item on the flow's graph: do a stage, validate it, and follow the edge its outcome picks.

Per try: do -> check script -> reviewer -> decide. The first fail ends the try. An accepted stage follows its
pass edge. A rejected stage follows its fail edge, to itself or to an earlier stage, which runs again with the
reason; the item then works its way forward again. Out of tries, a validator that errors, or a request for a
person all set the item aside. Nothing passes by default.
"""
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from .agents import CHECKER, DEFAULT_AGENT, Verdict, file_tools, person_tool, run_agent, plugin_tool
from .graph import END, Graph, stage_by, stage_tries
from .layout import ATTEMPTS, VERDICTS, prompt_file, verdict_file, visible_files
from .procs import DONE_EARLY, STOPPED, run_proc


@dataclass
class Ctx:
    settings: object
    store: object
    registry: object  # the snapshot this run executes from
    flow: str
    run_id: str
    item: dict
    work: Path
    trial: bool
    limits: dict
    graph: Graph | None = None  # the flow compiled; made from the registry when not given

    def __post_init__(self):
        self.graph = self.graph or self.registry.graph(self.flow)
        for part in (ATTEMPTS, VERDICTS):  # the engine's folders exist before a script runs, so no script can make them its own
            (self.work / part).mkdir(parents=True, exist_ok=True)

    @property
    def item_id(self) -> str:
        return self.item["id"]

    @property
    def flow_dir(self) -> Path:
        return self.registry.dir("flows", self.flow)

    def env(self, plugin: str) -> dict:
        """What a script is told: where the flow's files are, which item, whether this is a trial,
        and only the secrets it was granted. Trials withhold granted secrets, so a script cannot write outward."""
        env = {"AGENTFLOW_FLOW_DIR": str(self.flow_dir.resolve()), "AGENTFLOW_ITEM_ID": self.item_id}
        if self.trial:
            env["AGENTFLOW_TRIAL"] = "1"
        else:
            env.update(self.settings.secrets_for(plugin))
        return env

    def cancelled(self) -> bool:
        return self.store.cancel_requested(self.run_id)

    async def script(self, name: str, stdin: str) -> tuple[int, str, float]:
        """Run one of the flow's own scripts for this item: (exit code, output, seconds). A cancelled run stops it."""
        plugin, began = f"flows/{self.flow}", time.time()
        code, out = await run_proc([str((self.flow_dir / name).resolve())], self.work, stdin, self.env(plugin), stop=self.cancelled)
        return code, out, round(time.time() - began, 3)

    def tool(self, name: str):
        return plugin_tool(self.registry, name, self.work, self.env(f"tools/{name}"))

    def emit(self, stage: str | None, type_: str, **data) -> None:
        self.store.emit(self.run_id, self.item_id, stage, type_, folder=self.work, **data)

    def tries_of(self, stage: str) -> list:
        return self.store.q("SELECT n, outcome, started FROM attempts WHERE run_id = ? AND item_id = ? AND stage = ? ORDER BY n", self.run_id, self.item_id, stage)

    def feedback(self, stage: str) -> list[str]:
        """The reasons a stage is told when it runs again: its own tries that were not accepted, and the
        rejections of later stages that sent the item back here since this stage last ran. Read from the
        store, so a resumed item is told the same as one that never stopped."""
        last_ran = max((a["started"] for a in self.tries_of(stage)), default=0)
        senders, seen, out = self.graph.senders(stage), set(), []
        for v in self.store.q("""SELECT v.stage, v.n, v.by, v.reason, a.started FROM verdicts v
                                 JOIN attempts a ON a.run_id = v.run_id AND a.item_id = v.item_id AND a.stage = v.stage AND a.n = v.n
                                 WHERE v.run_id = ? AND v.item_id = ? AND v.passed = 0 AND a.outcome = 'rejected' ORDER BY v.id""", self.run_id, self.item_id):
            if (v["stage"], v["n"]) in seen:
                continue  # the first failing verdict of a try is its reason
            seen.add((v["stage"], v["n"]))
            if v["stage"] == stage:
                out.append(f"- attempt {v['n']}, {v['by']}: {v['reason']}")
            elif v["stage"] in senders and v["started"] > last_ran:
                out.append(f"- the later stage '{v['stage']}' was not accepted, {v['by']}: {v['reason']}")
        return out


def decide(tries: int, attempt: int, verdicts: list[dict]) -> str:
    """Pure: what happens after one round. Returns 'pass', 'again' or 'person'."""
    if any(v.get("needs_person") or v.get("error") for v in verdicts):
        return "person"
    if all(v["passed"] for v in verdicts):
        return "pass"
    return "again" if attempt < tries else "person"


End = tuple[str, str] | None  # (outcome, reason) when the stage ends here; None when the round goes on


async def _by_script(ctx: Ctx, stage: dict, n: int, attempt: int) -> End:
    """Do the stage by running its script."""
    sid = stage["id"]
    code, out, seconds = await ctx.script(stage["run"], json.dumps(ctx.item))
    if code == STOPPED:
        ctx.store.end_attempt(attempt, "cancelled")
        ctx.emit(sid, "attempt_cancelled", n=n, script=stage["run"])
        return "cancelled", f"the run was cancelled while {stage['run']} was running; the script was stopped"
    ctx.store.end_attempt(attempt, "done" if code in (0, DONE_EARLY) else "error")
    ctx.emit(sid, "attempt_ended", n=n, exit_code=code, script=stage["run"], seconds=seconds, output=out[-1500:])
    if code == DONE_EARLY:
        return "done_early", out[-500:] or f"the {sid} script ended this item early"
    if code != 0:
        return "failed", f"{stage['run']} exited with code {code}: {out[-500:]}"
    return None


async def _by_agent(ctx: Ctx, stage: dict, n: int, attempt: int, feedback: list[str]) -> End:
    """Do the stage with its agent, or with the default agent following the stage's own file."""
    sid, by = stage["id"], stage_by(stage)
    goal, note = ctx.registry.flow_goal(ctx.flow), ctx.registry.stage_method(ctx.flow, sid)
    if "agent" in stage:
        meta, method = ctx.registry.agent(stage["agent"])
        instructions = (f"{method}\n\nIf you cannot decide from the files, call ask_for_person and stop.\n\n## The flow you are part of\n{goal}"
                        + (f"\n\n## This stage: {sid}\n{note}" if note else ""))
        extra = [ctx.tool(t) for t in meta.get("tools") or []]
    else:
        instructions = f"{DEFAULT_AGENT}\n\n## The flow you are part of\n{goal}\n\n## Your stage: {sid}\n{note}"
        extra = []
    prompt = f"Do the stage '{sid}' for item {ctx.item_id}. Your work folder holds: {', '.join(visible_files(ctx.work))}."
    if "done_when" in stage:  # the executor is told up front what the reviewer will judge
        prompt += f"\nThe stage is accepted when: {stage['done_when']}"
    if feedback:
        prompt += "\n\nEarlier attempts failed validation. Fix these:\n" + "\n".join(feedback)
    (ctx.work / prompt_file(sid, n)).write_text(prompt)  # evidence of what the try was told
    asked: list[str] = []
    tools = file_tools(ctx.work, can_write=True) + extra + [person_tool(asked)]
    try:
        _, stats = await run_agent(ctx.settings, instructions, prompt, tools, model=ctx.registry.model_for(ctx.flow, stage, stage.get("agent")),
                                   on_event=lambda t, **d: ctx.emit(sid, t, n=n, agent=by, **d))
    except Exception as e:  # a model or network error: the item fails and the next run takes it again
        ctx.store.end_attempt(attempt, "error")
        ctx.emit(sid, "attempt_ended", n=n, error=str(e)[:500])
        return "failed", f"{by} could not finish {sid}: {type(e).__name__}: {str(e)[:300]}"
    ctx.store.end_attempt(attempt, "done", stats)
    ctx.emit(sid, "attempt_ended", n=n, **stats)
    if asked:
        ctx.store.set_attempt_outcome(attempt, "asked_for_person")
        ctx.emit(sid, "person_asked", n=n, by=by, reason=asked[0])
        return "needs_person", asked[0]
    return None


async def _validate(ctx: Ctx, stage: dict, n: int, attempt: int) -> tuple[list[dict], End]:
    """The check script first, then the reviewer; the first fail ends the round. Returns the verdicts of this round."""
    sid = stage["id"]
    verdicts: list[dict] = []

    def record(v: dict) -> None:  # stored and announced the moment it is known
        verdicts.append(v)
        ctx.store.add_verdict(ctx.run_id, ctx.item_id, sid, n, v)
        ctx.emit(sid, "verdict", n=n, **v)

    if "check" in stage:
        code, out, seconds = await ctx.script(stage["check"], "")
        if code == STOPPED:
            ctx.store.set_attempt_outcome(attempt, "cancelled")
            ctx.emit(sid, "attempt_cancelled", n=n, script=stage["check"])
            return verdicts, ("cancelled", f"the run was cancelled while {stage['check']} was checking {sid}; the script was stopped")
        record({"by": f"check {stage['check']}", "passed": code == 0, "reason": out[-2000:], "seconds": seconds})
    if all(v["passed"] for v in verdicts) and ("done_when" in stage or "review" in stage):
        if "review" in stage:
            meta, method = ctx.registry.agent(stage["review"])
            who, instructions = stage["review"], method
            extra = [ctx.tool(t) for t in meta.get("tools") or []]
        else:
            who, instructions, extra = "checker", CHECKER, []
        ask = (f"Stage '{sid}' for item {ctx.item_id}. Done when: {stage.get('done_when', 'your method is satisfied')}\n"
               f"The work folder holds: {', '.join(visible_files(ctx.work))}.")
        try:
            v, stats = await run_agent(ctx.settings, instructions, ask, file_tools(ctx.work, can_write=False) + extra, output_type=Verdict,
                                       model=ctx.registry.model_for(ctx.flow, {}, stage.get("review")),  # the reviewer's own model, else the flow's
                                       on_event=lambda t, **d: ctx.emit(sid, t, n=n, agent=who, role="reviewer", **d))
            record({"by": who, "passed": v.passed, "reason": v.reason, "needs_person": v.needs_person, **stats})
        except Exception as e:  # a validator that crashes never passes by default
            record({"by": who, "passed": False, "reason": f"reviewer error: {type(e).__name__}: {str(e)[:300]}", "error": True})
    if verdicts:  # every round is kept, pass or fail
        (ctx.work / verdict_file(sid, n)).write_text(
            f"Stage {sid}, attempt {n}\n\n" + "\n".join(f"- {v['by']}: {'pass' if v['passed'] else 'fail'}. {v['reason']}" for v in verdicts) + "\n")
    return verdicts, None


async def run_stage(ctx: Ctx, stage: dict) -> tuple[str, str]:
    """One visit of an item to a stage. Returns (outcome, reason). Outcome: passed, back (not accepted, and the
    fail edge leads to another stage), done_early, needs_person, failed, over_budget or cancelled."""
    sid, tries, by = stage["id"], stage_tries(stage), stage_by(stage)
    back = ctx.graph.after(sid, "fail")
    while True:
        # Only a try that validation rejected counts against `tries`; one cut short by a stopped process does not.
        prior = ctx.tries_of(sid)
        rejected = sum(a["outcome"] == "rejected" for a in prior)
        if rejected >= tries:
            return "needs_person", f"{sid}: not accepted after {rejected} of {tries} tries"
        n = max((a["n"] for a in prior), default=0) + 1
        feedback = ctx.feedback(sid) if "run" not in stage else []  # a script cannot read reasons; it is simply run again
        spent = ctx.store.usage(ctx.run_id)["cost_usd"]
        if spent >= ctx.limits["cost_per_run_usd"]:
            return "over_budget", f"the run has spent ${spent:.4f}, at or over its cap of ${ctx.limits['cost_per_run_usd']:.2f}"
        attempt = ctx.store.start_attempt(ctx.run_id, ctx.item_id, sid, n, by)
        model = "" if "run" in stage else ctx.settings.model_label(ctx.registry.model_for(ctx.flow, stage, stage.get("agent")))
        ctx.emit(sid, "attempt_started", n=n, by=by, model=model, feedback=bool(feedback))

        end = await (_by_script(ctx, stage, n, attempt) if "run" in stage else _by_agent(ctx, stage, n, attempt, feedback))
        if end is None:
            verdicts, end = await _validate(ctx, stage, n, attempt)
        if end is not None:
            return end

        outcome = decide(tries, rejected + 1, verdicts)
        if outcome == "pass":
            ctx.store.set_attempt_outcome(attempt, "passed")
            ctx.emit(sid, "stage_passed", tries=n)
            return "passed", verdicts[-1]["reason"] if verdicts else ""
        ctx.store.set_attempt_outcome(attempt, "rejected")
        failing = next(v for v in verdicts if not v["passed"])
        if outcome == "person":
            why = "the reviewer asked for a person" if failing.get("needs_person") else \
                  "the validator failed to run" if failing.get("error") else f"not accepted after {rejected + 1} of {tries} tries"
            return "needs_person", f"{sid}: {why}. Last reason from {failing['by']}: {failing['reason']}"
        if back != sid:  # the fail edge leads to an earlier stage: the item goes there and works its way back
            return "back", f"{failing['by']}: {failing['reason']}"


async def run_item(ctx: Ctx, start_at: str | None = None) -> tuple[str, str]:
    """One item along the flow's graph, from `start_at` if given (a resume or a retry), else from the first stage.
    Returns (ending, reason); the ending is one of ENDINGS."""
    for sub in (ATTEMPTS, VERDICTS):
        (ctx.work / sub).mkdir(parents=True, exist_ok=True)
    if start_at is None:
        ctx.emit(None, "item_started")
    else:
        ctx.emit(None, "item_started", from_stage=start_at)

    async def walk() -> tuple[str, str]:
        at = start_at or ctx.graph.start
        while at != END:
            if ctx.cancelled():  # between stages; inside one, a running script is stopped and an agent's try finishes
                return "cancelled", f"the run was cancelled before stage {at}"
            outcome, reason = await run_stage(ctx, ctx.graph.stages[at])
            if outcome == "passed":
                at = ctx.graph.after(at)
            elif outcome == "back":
                to = ctx.graph.after(at, "fail")
                ctx.emit(at, "sent_back", to=to, reason=reason[:500])
                at = to
            elif outcome == "done_early":
                return "done", f"ended early at {at}: {reason}"
            else:
                return outcome, reason
        return "done", ""

    minutes = ctx.limits["minutes_per_item"]
    try:
        ending, reason = await asyncio.wait_for(walk(), timeout=minutes * 60)
    except asyncio.TimeoutError:
        ending, reason = "timed_out", f"not finished after {minutes} minutes"
        ctx.store.close_open_attempts(ctx.run_id, "timed_out", ctx.item_id)
    ctx.store.end_item(ctx.run_id, ctx.item_id, ending, reason)
    return ending, reason
