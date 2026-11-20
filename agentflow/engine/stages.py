"""One item on the flow's graph: do a stage, validate it, and follow the edge its outcome picks.

Per try: do -> check script -> reviewer -> decide. The one who does a stage ends with its own outcome: done or failed.
Done work is validated, and the first fail ends the try. Work that was not accepted goes back to the one who did it,
in the same conversation, and it decides: fix it, or say that it has failed. An accepted stage follows its pass edge.
The engine only keeps a ceiling on the rounds (`tries`). Nothing passes by default.
"""
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from agentflow.workers.agents import AgentStopped, Outcome, Verdict, base_prompt, file_tools, run_agent, plugin_tool
from agentflow.plugins.graph import END, Graph, stage_by
from agentflow.workers.harness import CliStopped, Job, cli_note, run_cli, tool_commands, tool_servers
from agentflow.records.layout import ATTEMPTS, VERDICTS, cli_file, prompt_file, verdict_file, visible_files
from agentflow.workers.procs import DONE_EARLY, STOPPED, run_proc


def run_told(settings, flow_dir: Path, flow: str, run_id: str, output: Path, trial: bool) -> dict:
    """What every script of a run is told: the engine's home, the flow and where its files are, the run, where the run's
    output goes, and whether this is a trial."""
    return {"AGENTFLOW_HOME": str(settings.home), "AGENTFLOW_FLOW": flow, "AGENTFLOW_FLOW_DIR": str(flow_dir.resolve()), "AGENTFLOW_RUN": run_id,
            "AGENTFLOW_OUTPUT": str(output.resolve()), **({"AGENTFLOW_TRIAL": "1"} if trial else {})}


async def run_script(settings, store, run_id: str, file: Path, cwd: Path, stdin: str, env: dict, limits: dict) -> tuple[int, str, float]:
    """Run one of a flow's own scripts: (exit code, output, seconds). A cancelled run stops it."""
    began = time.time()
    code, out = await run_proc([str(file.resolve())], cwd, stdin, env, stop=lambda: store.cancel_requested(run_id), **settings.proc(limits))
    return code, out, round(time.time() - began, 3)


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
    graph: Graph  # the flow compiled
    output: Path  # the run's output folder, which the flow's scripts may write

    def __post_init__(self):
        for part in (ATTEMPTS, VERDICTS):  # the engine's folders exist before a script runs, so no script can make them its own
            (self.work / part).mkdir(parents=True, exist_ok=True)

    @property
    def item_id(self) -> str:
        return self.item["id"]

    @property
    def flow_dir(self) -> Path:
        return self.registry.dir("flows", self.flow)

    def told(self) -> dict:
        """What every script of an item, and a CLI doing a stage, is told: what the run's scripts are told, and which item."""
        return {**run_told(self.settings, self.flow_dir, self.flow, self.run_id, self.output, self.trial), "AGENTFLOW_ITEM_ID": self.item_id}

    def cancelled(self) -> bool:
        return self.store.cancel_requested(self.run_id)

    async def script(self, name: str, stdin: str) -> tuple[int, str, float]:
        """Run one of the flow's own scripts for this item, in its work folder."""
        return await run_script(self.settings, self.store, self.run_id, self.flow_dir / name, self.work, stdin, self.told(), self.limits)

    def stand_in(self) -> Path | None:
        """The flow's stand-in script, which answers in the model's place when the engine runs on the stand-in."""
        return self.flow_dir / self.registry.flow(self.flow)["stand_in"] if self.settings.stand_in else None

    def tool(self, name: str):
        return plugin_tool(self.settings, self.registry, name, self.work, self.told(), self.limits)

    def emit(self, stage: str | None, type_: str, **data) -> None:
        self.store.emit(self.run_id, self.item_id, stage, type_, folder=self.work, **data)

    def retried(self) -> float:
        """When a person last had this item retried, or 0. Tries from before that do not count against a stage's ceiling."""
        return self.store.one("SELECT COALESCE(MAX(ts), 0) t FROM events WHERE run_id = ? AND item_id = ? AND type = 'item_retried'", self.run_id, self.item_id)["t"]

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
                                 WHERE v.run_id = ? AND v.item_id = ? AND v.passed = 0 AND a.outcome IN ('rejected', 'failed') ORDER BY v.id""", self.run_id, self.item_id):
            if (v["stage"], v["n"]) in seen:
                continue  # the first failing verdict of a try is its reason
            seen.add((v["stage"], v["n"]))
            if v["stage"] == stage:
                out.append(f"- attempt {v['n']}, {v['by']}: {v['reason']}")
            elif v["stage"] in senders and v["started"] > last_ran:
                out.append(f"- the later stage '{v['stage']}' was not accepted, {v['by']}: {v['reason']}")
        return out


def decide(tries: int, attempt: int, verdicts: list[dict]) -> str:
    """Pure: what happens after one round. Returns 'pass', 'again' or 'failed'."""
    if any(v.get("error") for v in verdicts):
        return "failed"
    if all(v["passed"] for v in verdicts):
        return "pass"
    return "again" if attempt < tries else "failed"


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
    ctx.emit(sid, "attempt_ended", n=n, exit_code=code, script=stage["run"], seconds=seconds, output=out[-ctx.settings.internal["event_chars"]:])
    if code == DONE_EARLY:
        return "done_early", out[-ctx.settings.internal["error_chars"]:] or f"the {sid} script ended this item early"
    if code != 0:
        return "failed", f"{stage['run']} exited with code {code}: {out[-ctx.settings.internal['error_chars']:]}"
    return None


def _schema(shape) -> dict:
    """The JSON schema a CLI is handed for its answer: every field required, nothing else allowed."""
    schema = shape.model_json_schema()
    return {"type": "object", "additionalProperties": False, "required": list(schema["properties"]),
            "properties": {k: {f: v[f] for f in ("type", "enum") if f in v} for k, v in schema["properties"].items()}}


async def _agent_run(ctx: Ctx, sid: str, n: int, instructions: str, prompt: str, tools: list[str], model, report, review: bool, talk: list | None = None):
    """One agent run of a stage, on the runtime its model names: the engine's own, or a coding CLI. The same for the
    one who does the stage and the one who reviews it. The first answers with an Outcome and may write; a reviewer reads
    only and answers with a Verdict. `talk` is the conversation of the one who does the stage, which goes on from try
    to try on the engine's own runtime. Returns (answer, what it used)."""
    shape = Verdict if review else Outcome
    harness = ctx.settings.harness_of(model)
    if not harness:
        given = file_tools(ctx.work, not review, ctx.settings) + [ctx.tool(t) for t in tools]
        return await run_agent(ctx.settings, instructions, prompt, given, output_type=shape, model=model, limits=ctx.limits,
                               on_event=report, stand_in=ctx.stand_in(), talk=talk)
    part = f"{sid}-review" if review else sid
    commands, servers = tool_commands(ctx.registry, tools), tool_servers(ctx.registry, tools, ctx.told())
    schema = ctx.work / cli_file(part, n, harness, "schema.json")
    schema.write_text(json.dumps(_schema(shape)))
    text, stats = await run_cli(ctx.settings, ctx.settings.model(model), f"{instructions}\n\n{cli_note(commands, servers, review)}", prompt, ctx.work,
                                ctx.work / cli_file(part, n, harness, "jsonl"), ctx.work / cli_file(part, n, harness, "md"), ctx.told(), ctx.limits,
                                Job(tuple(program for _, _, program, _ in commands), servers, schema, not review), stop=ctx.cancelled, on_event=report,
                                stand_in=ctx.stand_in())
    try:
        return shape.model_validate_json(text[text.index("{"):text.rindex("}") + 1]), stats  # the JSON object in what it said
    except ValueError as e:
        raise AgentStopped(f"the {harness} CLI did not end with the answer it was asked for: {text[:ctx.settings.internal['error_chars']]}", stats) from e


def instructions_for(ctx: Ctx, stage: dict) -> tuple[str, list[str]]:
    """(what the one who does a stage is told before the task, its tools). In layers, the general first: the base
    instructions (the engine's, or the flow's own file in their place), the agent's method, what the flow is for, the stage."""
    sid = stage["id"]
    layers = [ctx.registry.flow_instructions(ctx.flow) or base_prompt("agent")]
    names = []
    if "agent" in stage:
        meta, method = ctx.registry.agent(stage["agent"])
        layers.append(f"## Your method\n{method}")
        names = meta.get("tools") or []
    layers.append(f"## The flow you are part of\n{ctx.registry.flow_goal(ctx.flow)}")
    note = ctx.registry.stage_method(ctx.flow, sid)
    if note:
        layers.append(f"## This stage: {sid}\n{note}")
    return "\n\n".join(layers), names


async def _by_agent(ctx: Ctx, stage: dict, n: int, attempt: int, feedback: list[str], talk: list) -> End:
    """Do the stage with its agent, or with the default agent following the stage's own file, on the runtime its model names.
    With a conversation to go on from (`talk`), the agent is only told why its work was not accepted; else it gets the whole task."""
    sid, by = stage["id"], stage_by(stage)
    instructions, names = instructions_for(ctx, stage)
    decide_ = "Decide whether you can fix this. If you can, fix it and end with done. If you cannot, end with failed and say why."
    if talk:
        prompt = f"Stage '{sid}' for item {ctx.item_id}: your work was not accepted.\n{feedback[-1]}\n\n{decide_}"
    else:
        prompt = f"Do the stage '{sid}' for item {ctx.item_id}. Your work folder holds: {', '.join(visible_files(ctx.work))}."
        if "done_when" in stage:  # the executor is told up front what the reviewer will judge
            prompt += f"\nThe stage is accepted when: {stage['done_when']}"
        if feedback:
            prompt += "\n\nEarlier attempts at this stage did not get through:\n" + "\n".join(feedback) + f"\n\n{decide_}"
    (ctx.work / prompt_file(sid, n)).write_text(prompt)  # evidence of what the try was told
    model = ctx.registry.model_for(ctx.flow, stage, stage.get("agent"))
    harness = ctx.settings.harness_of(model)

    def report(type_: str, **data) -> None:
        ctx.emit(sid, type_, n=n, agent=by, **data)

    try:
        said, stats = await _agent_run(ctx, sid, n, instructions, prompt, names, model, report, False, None if harness else talk)
    except CliStopped:
        ctx.store.end_attempt(attempt, "cancelled")
        ctx.emit(sid, "attempt_cancelled", n=n, script=f"the {harness} CLI")
        return "cancelled", f"the run was cancelled while the {harness} CLI was working on {sid}; it was stopped"
    except Exception as e:  # the try stopped without an answer. It does not end the item: the next try is told why
        stats = getattr(e, "stats", None) or {"model_calls": 1}  # what it had used by then; not known when it could not say
        why = str(e)[:ctx.settings.internal["reason_chars"]]
        talk.clear()  # a conversation that broke off is not gone on from
        ctx.store.end_attempt(attempt, "error", stats)
        ctx.emit(sid, "attempt_ended", n=n, error=why, **stats)
        return "error", f"the try stopped without an answer: {why}"
    ctx.store.end_attempt(attempt, "done", stats)
    ctx.emit(sid, "attempt_ended", n=n, **stats)
    if said.outcome == "failed":  # its own decision: the stage cannot be done. Kept as a reason, so a later retry is told
        ctx.store.set_attempt_outcome(attempt, "failed")
        ctx.store.add_verdict(ctx.run_id, ctx.item_id, sid, n, {"by": by, "passed": False, "reason": said.reason})
        ctx.emit(sid, "agent_failed", n=n, by=by, reason=said.reason)
        return "failed", f"{sid}: {by} could not do it: {said.reason}"
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
        record({"by": f"check {stage['check']}", "passed": code == 0, "reason": out[-ctx.settings.internal["reason_chars"]:], "seconds": seconds})
    if all(v["passed"] for v in verdicts) and ("done_when" in stage or "review" in stage):
        if "review" in stage:
            meta, method = ctx.registry.agent(stage["review"])
            who, instructions, names = stage["review"], method, meta.get("tools") or []
        else:
            who, instructions, names = "checker", base_prompt("reviewer"), []
        ask = (f"Stage '{sid}' for item {ctx.item_id}. Done when: {stage.get('done_when', 'your method is satisfied')}\n"
               f"The work folder holds: {', '.join(visible_files(ctx.work))}.")
        try:
            v, stats = await _agent_run(ctx, sid, n, instructions, ask, names, ctx.registry.model_for(ctx.flow, {}, stage.get("review")),  # the reviewer's own model, else the flow's
                                        lambda t, **d: ctx.emit(sid, t, n=n, agent=who, role="reviewer", **d), True)
            record({"by": who, "passed": v.passed, "reason": v.reason, **stats})
        except CliStopped as e:
            ctx.store.set_attempt_outcome(attempt, "cancelled")
            ctx.emit(sid, "attempt_cancelled", n=n, script=f"the {e} CLI")
            return verdicts, ("cancelled", f"the run was cancelled while the {e} CLI was reviewing {sid}; it was stopped")
        except Exception as e:  # a validator that crashes never passes by default
            record({"by": who, "passed": False, "reason": f"reviewer error: {type(e).__name__}: {str(e)[:ctx.settings.internal['error_chars']]}", "error": True})
    if verdicts:  # every round is kept, pass or fail
        (ctx.work / verdict_file(sid, n)).write_text(
            f"Stage {sid}, attempt {n}\n\n" + "\n".join(f"- {v['by']}: {'pass' if v['passed'] else 'fail'}. {v['reason']}" for v in verdicts) + "\n")
    return verdicts, None


async def run_stage(ctx: Ctx, stage: dict) -> tuple[str, str]:
    """One visit of an item to a stage. Returns (outcome, reason). Outcome: passed, back (not accepted, and the
    fail edge leads to another stage), done_early, failed, over_budget or cancelled.
    Whether work that was not accepted is done again is for the one who did it to decide: it is told why, in the
    conversation it already has, and fixes it or ends with failed. A try that stopped with an error (the model could not
    be reached, an answer was cut off, a CLI failed) is told to a fresh try the same way. `tries` is only the ceiling."""
    sid, by = stage["id"], stage_by(stage)
    tries = ctx.graph.tries(sid)
    back = ctx.graph.after(sid, "fail")
    since = ctx.retried()
    talk: list = []  # the conversation of the agent doing this stage, from try to try
    while True:
        # Only a try that validation rejected counts against `tries`; one cut short by a stopped process does not.
        prior = ctx.tries_of(sid)
        rejected = sum(a["outcome"] == "rejected" and a["started"] > since for a in prior)
        if rejected >= tries:
            return "failed", f"{sid}: not accepted after {rejected} of {tries} tries"
        n = max((a["n"] for a in prior), default=0) + 1
        feedback = ctx.feedback(sid) if "run" not in stage else []  # a script cannot read reasons; it is simply run again
        spent = ctx.store.usage(ctx.run_id)["cost_usd"]
        if spent >= ctx.limits["cost_per_run_usd"]:
            return "over_budget", f"the run has spent ${spent:.4f}, at or over its cap of ${ctx.limits['cost_per_run_usd']:.2f}"
        attempt = ctx.store.start_attempt(ctx.run_id, ctx.item_id, sid, n, by)
        model = "" if "run" in stage else ctx.settings.model_label(ctx.registry.model_for(ctx.flow, stage, stage.get("agent")))
        ctx.emit(sid, "attempt_started", n=n, by=by, model=model, feedback=bool(feedback))

        end = await (_by_script(ctx, stage, n, attempt) if "run" in stage else _by_agent(ctx, stage, n, attempt, feedback, talk))
        if end is not None and end[0] == "error":  # counts as a try that was not accepted, with the engine's reason for the next one
            verdicts, end = [{"by": "engine", "passed": False, "reason": end[1]}], None
            ctx.store.add_verdict(ctx.run_id, ctx.item_id, sid, n, verdicts[0])
            ctx.emit(sid, "verdict", n=n, **verdicts[0])
        elif end is None:
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
        if outcome == "failed":
            why = "the validator failed to run" if failing.get("error") else f"not accepted after {rejected + 1} of {tries} tries"
            return "failed", f"{sid}: {why}. Last reason from {failing['by']}: {failing['reason']}"
        if back != sid:  # the fail edge leads to an earlier stage: the item goes there and works its way back
            return "back", f"{failing['by']}: {failing['reason']}"


async def run_item(ctx: Ctx, start_at: str | None = None) -> tuple[str, str]:
    """One item along the flow's graph, from `start_at` if given (a resume), else from the first stage.
    Returns (ending, reason); the ending is one of ENDINGS."""
    if start_at is None:
        ctx.emit(None, "item_started")
    else:
        ctx.emit(None, "item_started", from_stage=start_at)

    async def walk() -> tuple[str, str]:
        at = start_at or ctx.graph.start
        while at != END:
            if ctx.cancelled():  # between stages; inside one, a running script or CLI is stopped and an agent's try finishes
                return "cancelled", f"the run was cancelled before stage {at}"
            outcome, reason = await run_stage(ctx, ctx.graph.stages[at])
            if outcome == "passed":
                at = ctx.graph.after(at)
            elif outcome == "back":
                to = ctx.graph.after(at, "fail")
                ctx.emit(at, "sent_back", to=to, reason=reason[:ctx.settings.internal["error_chars"]])
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
