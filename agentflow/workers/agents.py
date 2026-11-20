"""One agent run: the model, the tools it is handed, and what it cost.

This is the only module that knows about model providers. Agents, tools and flows never do.
"""
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
from fastmcp.client.transports import StdioTransport  # noqa: E402
from pydantic_ai import Agent, FunctionToolCallEvent, FunctionToolResultEvent, Tool, capture_run_messages  # noqa: E402
from pydantic_ai.capabilities import AbstractCapability, ProcessHistory  # noqa: E402
from pydantic_ai.exceptions import ModelAPIError, OutputTokenLimitExceeded, UsageLimitExceeded  # noqa: E402
from pydantic_ai.mcp import MCPToolset  # noqa: E402
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolReturnPart  # noqa: E402
from pydantic_ai.usage import RunUsage, UsageLimits  # noqa: E402

from agentflow.workers.harness import mcp_argv, not_ready  # noqa: E402
from agentflow.records.layout import confined, visible_files  # noqa: E402
from agentflow.workers.procs import run_proc  # noqa: E402

def base_prompt(name: str) -> str:
    """The engine's own instructions for the one who does a stage (`agent`) or reviews it (`reviewer`). A flow may replace the first with its own file."""
    return (Path(__file__).parent / "prompts" / f"{name}.md").read_text().strip()


class Outcome(BaseModel):
    """How the one who does a stage ends: it decides itself whether the stage is done or has failed."""
    outcome: Literal["done", "failed"]
    reason: str


class Verdict(BaseModel):
    passed: bool
    reason: str


class ModelNotReady(Exception):
    pass


class AgentStopped(Exception):
    """An agent run that ended without an answer. `stats` is what it had used by then, so a stopped run still has its cost."""

    def __init__(self, reason: str, stats: dict):
        super().__init__(reason)
        self.stats = stats


# ---------- the agent loop: what goes wrong inside it goes back to the agent ----------

CLEARED = "[this earlier tool result was cleared to keep the conversation within the model's context; call the tool again if you need it]"


class ToolErrors(AbstractCapability):
    """A tool that fails does not end the run: the agent is told what went wrong, as the tool's answer, and decides what to do."""

    async def on_tool_execute_error(self, ctx, *, call, tool_def, args, error):
        return f"error: the tool {call.tool_name} failed: {type(error).__name__}: {error}. Use another way, or go on without it."


def keep_in_context(limit: int, keep: int):
    """A history processor: once the last request read more than `limit` tokens, the results of all but the latest `keep`
    tool calls are cleared. The calls stay, so the agent still knows what it did and can ask again."""
    def process(messages: list) -> list:
        last = next((m for m in reversed(messages) if isinstance(m, ModelResponse)), None)
        if last is not None and last.usage.input_tokens > limit:
            results = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
            for part in results[:-keep]:
                part.content = CLEARED
        return messages
    return process


def why_stopped(e: Exception, limits: dict) -> str:
    """Why an agent run ended without an answer, in words the next try can act on."""
    if isinstance(e, OutputTokenLimitExceeded):
        return "the model's answer was cut off at its output limit. Say less in one answer, and write a long file in several smaller parts."
    if isinstance(e, UsageLimitExceeded):
        return f"the run used all of its {limits['model_calls']} model requests without finishing. Do the stage in fewer steps."
    if isinstance(e, ModelAPIError):
        return f"the model could not be reached or refused the request, after the client's own retries: {e}"
    return f"{type(e).__name__}: {e}"


# ---------- tools an agent may be handed ----------

def file_tools(work: Path, can_write: bool, settings) -> list[Tool]:
    def list_files() -> list[str]:
        """List the files in the work folder."""
        return visible_files(work)

    def read_file(path: str) -> str:
        """Read a text file from the work folder."""
        try:
            return confined(work, path).read_text()[:settings.internal["read_chars"]]
        except (ValueError, OSError) as e:
            return f"error: {e}"

    def write_file(path: str, content: str) -> str:
        """Write a text file in the work folder."""
        try:
            target = confined(work, path, writing=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            return f"wrote {len(content)} characters to {path}"
        except (ValueError, OSError) as e:
            return f"error: {e}"

    return [Tool(list_files), Tool(read_file)] + ([Tool(write_file)] if can_write else [])


def plugin_tool(settings, registry, name: str, work: Path, env: dict, limits: dict | None = None):
    """A tool plugin as an agent gets it: one function from its script, or every tool of its MCP server.
    `limits` are the flow's; without them the platform's apply."""
    limits = limits or settings.limits
    spec, folder = registry.tool(name), registry.dir("tools", name)
    if "mcp" in spec:
        return McpTool(name, mcp_argv(registry, name), work, env)
    args = spec.get("args") or []
    exe = str((folder / spec["run"]).resolve())

    async def call(**kw) -> str:  # stopped with the try, when the item runs out of time or the engine stops
        code, out = await run_proc([exe] + [str(kw[a]) for a in args], cwd=work, env=env, **settings.proc(limits))
        return out[-settings.internal["tool_chars"]:] if code == 0 else f"tool failed with exit code {code}: {out[-settings.internal['reason_chars']:]}"

    schema = {"type": "object", "properties": {a: {"type": "string"} for a in args}, "required": args, "additionalProperties": False}
    return Tool.from_schema(call, name=name, description=spec["description"], json_schema=schema)


@dataclass
class McpTool:
    """A tool plugin that is an MCP server spoken to over its stdin and stdout. It is started in the item's folder
    when an agent run begins and stopped when the run ends. The agent gets every tool the server offers, named <plugin>_<tool>."""
    name: str
    argv: list[str]
    work: Path
    env: dict

    def _server(self, **kw):
        env = {"PATH": os.environ.get("PATH", ""), "LANG": os.environ.get("LANG", "C.UTF-8"), **self.env}
        return MCPToolset(StdioTransport(command=self.argv[0], args=self.argv[1:], env=env, cwd=str(self.work), keep_alive=False), id=self.name, **kw)

    def toolset(self):
        """The server's tools for one agent run."""
        return self._server().prefixed(self.name)

    async def offered(self, seconds: float) -> list[str]:
        """Start the server, ask which tools it has, stop it. This is its health check; `seconds` is how long the server may take to start."""
        async with self._server(init_timeout=seconds) as server:
            return [t.name for t in await server.list_tools()]


# ---------- the model ----------

def build_model(settings, spec=None, stand_in=None):
    """The model for one agent run. `spec` is what the stage, the agent or the flow picked: a name from
    agentflow.yaml, a mapping, or nothing for the default. Each model reads its own key from its own variable.
    On the stand-in, the flow's own `stand_in` script answers in the model's place."""
    if settings.stand_in:
        from agentflow.workers.stand_in import build
        return build(stand_in)
    try:
        m = settings.model(spec)
    except ValueError as e:
        raise ModelNotReady(str(e))
    if "harness" in m:  # only a stage's work can go to a CLI; `check` says so before a run
        raise ModelNotReady(f"the model {spec or 'default'} runs on the {m['harness']} CLI, and this part of a stage runs in the engine")
    if not m.get("name"):
        raise ModelNotReady(f"the model {spec or 'default'} has no name in agentflow.yaml")
    key = os.environ.get(m["api_key_env"])
    if not key:
        raise ModelNotReady(f"{m['api_key_env']} is not set. Export it or put it in .env, or run with --stand-in.")
    from pydantic_ai.models.openai import (OpenAIChatModel, OpenAIChatModelSettings, OpenAIResponsesModel,
                                           OpenAIResponsesModelSettings)
    if m["provider"] == "openai":
        from pydantic_ai.providers.openai import OpenAIProvider
        provider = OpenAIProvider(api_key=key, base_url=m.get("base_url"))
    elif m["provider"] == "litellm":
        from pydantic_ai.providers.litellm import LiteLLMProvider
        provider = LiteLLMProvider(api_key=key, api_base=m.get("base_url"))
    else:
        raise ModelNotReady(f"unknown provider '{m['provider']}'; use openai or litellm")
    effort = m["reasoning"]
    if m["api"] == "responses":  # some models only allow tools with reasoning on this interface
        return OpenAIResponsesModel(m["name"], provider=provider,
                                    settings=OpenAIResponsesModelSettings(openai_reasoning_effort=effort, openai_store=False))
    return OpenAIChatModel(m["name"], provider=provider, settings=OpenAIChatModelSettings(openai_reasoning_effort=effort))


def check_ready(settings, spec=None) -> None:
    """Raises ModelNotReady when a model a flow uses cannot be used now: its key is not set, or the CLI it names is not installed.
    A run asks this for every model it will use, before it does any work."""
    if settings.harness_of(spec) and not settings.stand_in:
        why = not_ready(settings.model(spec))
        if why:
            raise ModelNotReady(why)
    else:
        build_model(settings, spec)


async def run_agent(settings, instructions: str, prompt: str, tools: list, output_type=str, on_event=lambda *a, **k: None, model=None,
                    limits: dict | None = None, stand_in=None, talk: list | None = None):
    """One agent run on the model picked for it. Tool calls are reported through on_event while the agent is still working.
    `limits` are the flow's; without them the platform's apply. `talk` is the conversation so far, when this run goes on
    from an earlier one of the same agent; it is replaced with the conversation as it stands after this run."""
    limits = limits or settings.limits
    free = settings.stand_in  # the stand-in costs nothing, which is known, not unknown

    def price(usage) -> float | None:
        return 0.0 if free else float(usage.cost) if usage.cost is not None else None

    requests: list[tuple[float, float]] = []  # when each model request began and ended, for the trace
    calls: dict[str, float] = {}              # when each tool call began, by its id

    async def stream(ctx, events):  # called once per model request and once per round of tool calls
        began, model = time.time(), False
        async for ev in events:
            if isinstance(ev, FunctionToolCallEvent):
                calls[ev.part.tool_call_id] = time.time()
                on_event("tool_call", tool=ev.part.tool_name, args=ev.part.args, call=ev.part.tool_call_id)
            elif isinstance(ev, FunctionToolResultEvent):
                text = str(ev.part.content)
                took = time.time() - calls.pop(ev.part.tool_call_id, time.time())
                on_event("tool_result", tool=ev.part.tool_name, chars=len(text), call=ev.part.tool_call_id, seconds=round(took, 3),
                         preview=text[:settings.internal["event_chars"]])
            elif type(ev).__name__.startswith(("Part", "FinalResult")):  # parts of a model's answer; a structured
                model = True                                             # result is handled in a round of its own
        if model:
            requests.append((began, time.time()))

    started = time.time()
    agent = Agent(build_model(settings, model, stand_in), instructions=instructions, tools=[t for t in tools if isinstance(t, Tool)],
                  toolsets=[t.toolset() for t in tools if isinstance(t, McpTool)], output_type=output_type,  # an MCP server lives for this run only
                  tool_timeout=limits["script_seconds"],
                  capabilities=[ToolErrors(), ProcessHistory(keep_in_context(limits["context_tokens"], settings.internal["context_keep_results"]))])
    usage, stopped = RunUsage(), None  # the run counts into `usage` as it goes, so a run that stops still says what it used
    with capture_run_messages() as said:
        try:
            result = await agent.run(prompt, event_stream_handler=stream, usage_limits=UsageLimits(request_limit=limits["model_calls"]), usage=usage,
                                     message_history=list(talk) if talk else None)
        except Exception as e:
            stopped = e
    tool_calls = 0
    responses = [m for m in said[len(talk or []):] if isinstance(m, ModelResponse)]
    timed = iter(requests if len(requests) == len(responses) else [])  # only when the two lists line up
    for m in said[len(talk or []):]:  # one model_call event per request of this run, with its own tokens and cost
        if isinstance(m, ModelResponse):
            tool_calls += sum(1 for p in m.parts if p.part_kind == "tool-call" and p.tool_name != "final_result")
            began, ended = next(timed, (None, None))
            on_event("model_call", model="stand-in" if free else m.model_name, tokens_in=m.usage.input_tokens, tokens_out=m.usage.output_tokens,
                     cost_usd=price(m.usage),
                     **({"started": round(began, 3), "seconds": round(ended - began, 3)} if began else {}))
    stats = {
        "model_calls": usage.requests, "tool_calls": tool_calls,
        "tokens_in": usage.input_tokens, "tokens_out": usage.output_tokens,
        "cost_usd": price(usage),  # from Pydantic AI's price table; None when the model has no price there, which is not zero
        "seconds": round(time.time() - started, 2),
    }
    if stopped is not None:
        raise AgentStopped(why_stopped(stopped, limits), stats) from stopped
    if talk is not None:
        talk[:] = result.all_messages()
    return result.output, stats
