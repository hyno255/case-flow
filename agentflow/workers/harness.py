"""A stage's work handed to a coding CLI on this machine, such as Codex or Claude Code.

The engine starts the CLI in the item's work folder with the instructions and the prompt on its stdin, waits, and
keeps everything it printed. It does not follow what the CLI does: the check script and the reviewer judge the
files it left, as for any other try. From the output the engine takes two things, the last message and the cost.

A CLI keeps its own tools and gets the agent's tool plugins on top: a script tool as a command it may run, a tool
that is an MCP server as an MCP server of this one run. A reviewer on a CLI works read-only and answers in a given shape.

A model entry in agentflow.yaml picks a CLI with `harness:`. How each one is started and read is in CLIS below;
nothing about it is a setting. With --stand-in the scripted stand-in CLI is started in its place.
"""
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agentflow.records.layout import NEEDS_PERSON
from agentflow.workers.procs import STOPPED, run_proc

STAND_IN = str(Path(__file__).with_name("stand_in_cli.py"))
MACHINE = ("HOME", "USER", "LOGNAME", "SHELL", "TMPDIR")  # what a CLI needs of this machine to find its own sign-in and settings


class CliFailed(Exception):
    """The CLI did not start, ended with an error, or said nothing. `stats` is what it said it used, when it said so."""
    stats: dict | None = None


class CliStopped(Exception):
    """The run was cancelled while the CLI was working, and the CLI was stopped."""


@dataclass(frozen=True)
class Cli:
    program: str
    args: Callable[[str, Path, "Job"], list[str]]  # (the model's name, the file for its last message, what this run is given)
    read: Callable[[list[dict]], dict]  # what it printed -> text, tokens_in, cached_in, tokens_out, cost_usd, error; each only when found
    key: str          # the variable it reads an API key from
    prices: str       # whose price list prices its tokens when it reports no cost
    own: tuple = ()   # variables that say where its own files are


@dataclass(frozen=True)
class Job:
    """What one run of a CLI is given on top of its own tools."""
    commands: tuple = ()        # the programs of the agent's script tools, which it may run
    servers: tuple = ()         # (name, argv, env) of each tool that is an MCP server
    schema: Path | None = None  # the file with the JSON Schema its answer must have
    write: bool = True          # False for a reviewer: it reads the work folder and changes nothing


def _toml(value) -> str:
    """A value as Codex reads it in `-c key=value`: text, a list of text, or a table of text."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def _codex_args(model: str, last: Path, job: Job) -> list[str]:
    """MCP servers go in as settings of this one run; the user's own config and servers stay as they are."""
    servers = [x for name, argv, env in job.servers
               for key, value in (("command", argv[0]), ("args", argv[1:]), ("env", env)) for x in ("-c", f"mcp_servers.{name}.{key}={_toml(value)}")]
    return ["exec", "--json", "--skip-git-repo-check", "-s", "workspace-write" if job.write else "read-only", "-m", model, "-o", str(last), *servers,
            *(["--output-schema", str(job.schema)] if job.schema else []), "-"]


def _codex_read(printed: list[dict]) -> dict:
    """One event per line. The last agent message is the answer; every finished turn says what it used, and none says what it cost."""
    said = [e["item"].get("text") for e in printed if e.get("type") == "item.completed" and isinstance(e.get("item"), dict) and e["item"].get("type") == "agent_message"]
    turns = [e["usage"] for e in printed if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict)]
    wrong = [e.get("message") or (e.get("error") or {}).get("message") for e in printed if e.get("type") in ("error", "turn.failed")]
    found = {"text": said[-1] if said else None, "error": next((w for w in reversed(wrong) if w), None)}
    if turns:
        found.update({ours: sum(int(t.get(theirs) or 0) for t in turns)
                      for ours, theirs in (("tokens_in", "input_tokens"), ("cached_in", "cached_input_tokens"), ("tokens_out", "output_tokens"))})
    return found


def _claude_args(model: str, last: Path, job: Job) -> list[str]:
    """MCP servers go in with --mcp-config, beside the ones the user's own settings name; each is allowed by name."""
    allowed = [*(f"Bash({c} *)" for c in job.commands), *(f"mcp__{name}" for name, _, _ in job.servers)]
    servers = {name: {"command": argv[0], "args": argv[1:], "env": env} for name, argv, env in job.servers}
    return ["-p", "--output-format", "json", "--permission-mode", "acceptEdits" if job.write else "default", "--model", model,
            *(["--mcp-config", json.dumps({"mcpServers": servers})] if servers else []),
            *(["--json-schema", job.schema.read_text()] if job.schema else []),
            *(["--allowedTools", *allowed] if allowed else [])]


def _claude_read(printed: list[dict]) -> dict:
    """One result object, with the answer, what was used and what it cost."""
    r = next((e for e in reversed(printed) if e.get("type") == "result"), None)
    if r is None:
        return {}
    u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
    cached = int(u.get("cache_read_input_tokens") or 0)
    shaped = r.get("structured_output")  # the answer in the shape that was asked for, when one was
    found = {"text": json.dumps(shaped) if shaped is not None else r.get("result"), "cost_usd": r.get("total_cost_usd"), "error": r.get("result") or r.get("subtype") if r.get("is_error") else None}
    if u:
        found.update(tokens_in=int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + cached, cached_in=cached,
                     tokens_out=int(u.get("output_tokens") or 0))
    return found


CLIS = {
    "codex": Cli("codex", _codex_args, _codex_read, key="CODEX_API_KEY", prices="openai", own=("CODEX_HOME",)),
    "claude": Cli("claude", _claude_args, _claude_read, key="ANTHROPIC_API_KEY", prices="anthropic", own=("CLAUDE_CONFIG_DIR",)),
}


def mcp_argv(registry, name: str) -> list[str]:
    """How a tool plugin that is an MCP server is started: arguments that name a file of the plugin become that file's full path."""
    folder = registry.dir("tools", name)
    return [str((folder / a).resolve()) if (folder / a).is_file() else str(a) for a in registry.tool(name)["mcp"]]


def tool_servers(registry, names: list[str], env: dict) -> tuple:
    """(name, argv, env) for each tool an agent lists that is an MCP server. A CLI starts them itself, for this one run."""
    return tuple((name, mcp_argv(registry, name), env) for name in names if "mcp" in registry.tool(name))


def tool_commands(registry, names: list[str]) -> list[tuple[str, str, str, list[str]]]:
    """(name, description, program, argument names) for each script tool an agent lists. A CLI runs them itself, as commands."""
    out = []
    for name in names:
        spec = registry.tool(name)
        if "run" in spec:
            out.append((name, spec["description"], str((registry.dir("tools", name) / spec["run"]).resolve()), list(spec.get("args") or [])))
    return out


def cli_note(commands: list[tuple[str, str, str, list[str]]], servers: tuple = (), review: bool = False) -> str:
    """What a CLI is told on top of the stage's instructions: where it is, how to answer, and the tools it is given."""
    if review:
        lines = ["## How you work here",
                 "You are in the item's work folder. Read the files in this folder and change nothing.",
                 'Answer with one JSON object and nothing else: {"passed": true or false, "reason": "...", "needs_person": true or false}.']
    else:
        lines = ["## How you work here",
                 "You are in the item's work folder. Read and write files in this folder only, and leave attempts/, verdicts/ and events.jsonl alone.",
                 f"You have no tool named ask_for_person. Where the text above says to ask for a person, write {NEEDS_PERSON} "
                 "with what you checked and what a person should look at, then stop.",
                 "When the result is written, reply with one short line."]
    if servers:
        lines += ["", "## MCP servers you are given", "Their tools are yours to call: " + ", ".join(name for name, _, _ in servers) + "."]
    if commands:
        lines += ["", "## Commands you may run", "Run them from the work folder. Each prints its answer."]
        lines += [f"- {name}: {what} Command: `{program}{''.join(f' <{a}>' for a in args)}`" for name, what, program, args in commands]
    return "\n".join(lines)


def _printed(out: str) -> list[dict]:
    """The JSON values in what a CLI printed: one object, a list of them, or one per line. Anything else it printed is skipped."""
    try:
        whole = json.loads(out)
        return [v for v in (whole if isinstance(whole, list) else [whole]) if isinstance(v, dict)]
    except ValueError:
        pass
    found = []
    for line in out.splitlines():
        try:
            v = json.loads(line) if line[:1] in "{[" else None
        except ValueError:
            continue
        found += [x for x in (v if isinstance(v, list) else [v]) if isinstance(x, dict)]
    return found


def _env(cli: Cli, model: dict) -> dict:
    """What a CLI sees of the engine's environment: who it runs as and where its files are, so it uses its own sign-in.
    When the model entry names `api_key_env`, that key is handed over as the CLI's own key variable instead."""
    env = {k: os.environ[k] for k in MACHINE + cli.own if k in os.environ}
    if model.get("api_key_env"):
        key = os.environ.get(model["api_key_env"])
        if not key:
            raise CliFailed(f"{model['api_key_env']} is not set. Export it or put it in .env, or leave api_key_env out to use the CLI's own sign-in.")
        env[cli.key] = key
    return env


def not_ready(model: dict) -> str:
    """Why the CLI a model entry names cannot be started now, or "" when it can. Asked before a run does any work."""
    cli = CLIS[model["harness"]]
    if not shutil.which(cli.program):
        return f"the {model['harness']} CLI is not installed: no program named {cli.program} was found"
    try:
        _env(cli, model)
    except CliFailed as e:
        return str(e)
    return ""


def _cost(cli: Cli, model: str, found: dict) -> float | None:
    """What the try cost: what the CLI reported, else its tokens at the price list's rate. None when neither is known, which is not zero."""
    if found.get("cost_usd") is not None:
        return float(found["cost_usd"])
    if found.get("tokens_in") is None:
        return None
    from genai_prices import Usage, calc_price
    try:
        usage = Usage(input_tokens=found["tokens_in"], cache_read_tokens=found.get("cached_in") or 0, output_tokens=found.get("tokens_out") or 0)
        return float(calc_price(usage, model_ref=model, provider_id=cli.prices).total_price)
    except (LookupError, ValueError):  # the price list does not have this model
        return None


async def run_cli(settings, model: dict, instructions: str, prompt: str, work: Path, out_file: Path, last_file: Path, env: dict, limits: dict,
                  job: Job = Job(), stop=None, on_event=lambda *a, **k: None, stand_in=None) -> tuple[str, dict]:
    """One run of the CLI the model entry names. Returns (its last message, what the run used), as run_agent does.
    `out_file` gets everything it printed. The item's time limit and `stop` end it, with everything it started."""
    name = model["harness"]
    cli = CLIS[name]
    if settings.stand_in:  # the flow's stand-in script says what the CLI does
        argv, own = [sys.executable, "-I", STAND_IN, name, str(last_file), str(stand_in), "work" if job.write else "review"], {}
    else:
        argv, own = [cli.program, *cli.args(model["name"], last_file, job)], _env(cli, model)
    last_file.unlink(missing_ok=True)  # the answer has to be this run's
    began = time.time()
    code, out = await run_proc(argv, work, f"{instructions}\n\n{prompt}", {**own, **env}, stop=stop,
                               **{**settings.proc(limits), "timeout": limits["minutes_per_item"] * 60})
    seconds = round(time.time() - began, 2)
    out_file.write_text(out + "\n")

    if code == STOPPED:
        raise CliStopped(name)
    found = cli.read(_printed(out))
    reported = found.get("cost_usd") is not None
    cost = 0.0 if settings.stand_in else _cost(cli, model["name"], found)  # the stand-in costs nothing, which is known
    used = {"tokens_in": found.get("tokens_in") or 0, "tokens_out": found.get("tokens_out") or 0, "cost_usd": cost}

    def failed(how: str, said: str = "") -> CliFailed:  # the reason, with the end of what the CLI said, within the size a reason may have
        room = settings.internal["error_chars"] - len(how)
        e = CliFailed(how + (said[-room:] if room > 0 else ""))
        if found.get("tokens_in") is not None or reported:  # a run that failed still used what it said it used
            e.stats = {"model_calls": 1, "tool_calls": 0, "seconds": seconds, **used}
        return e

    if found.get("error"):  # its own words for what went wrong say more than the end of what it printed
        raise failed(f"{name} ended with an error: ", str(found["error"]))
    if code != 0:
        raise failed(f"{name} exited with code {code}: ", out)
    text = (last_file.read_text(errors="replace") if last_file.is_file() else found.get("text") or "").strip()
    if not text:
        raise failed(f"{name} ended without an answer; what it printed is in {out_file.name}")
    on_event("cli_run", harness=name, model="stand-in" if settings.stand_in else model["name"], started=round(began, 3), seconds=seconds, reported=reported,
             output=str(out_file.relative_to(work)), said=text[:settings.internal["event_chars"]], **used)
    return text, {"model_calls": 1, "tool_calls": 0, "seconds": seconds, **used}  # one run of the CLI, whatever it called inside


async def cli_version(settings, name: str) -> tuple[bool, str]:
    """(installed, its version line) for one CLI, as `agentflow check` prints it."""
    cli = CLIS[name]
    code, out = await run_proc([cli.program, "--version"], settings.home, env={k: os.environ[k] for k in MACHINE if k in os.environ},
                               **{**settings.proc(), "timeout": settings.internal["health_seconds"]})
    line = (out.strip().splitlines() or [""])[-1][:settings.internal["line_chars"]]
    return code == 0, line if code == 0 else f"{cli.program} is not installed or does not start: {line}"
