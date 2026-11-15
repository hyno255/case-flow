"""A stage's work handed to a coding CLI on this machine, such as Codex or Claude Code.

The engine starts the CLI in the item's work folder with the instructions and the prompt on its stdin, waits, and
keeps everything it printed. It does not follow what the CLI does: the check script and the reviewer judge the
files it left, as for any other try. From the output the engine takes two things, the last message and the cost.

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

from .layout import NEEDS_PERSON
from .procs import STOPPED, run_proc

STAND_IN = str(Path(__file__).with_name("stand_in_cli.py"))
MACHINE = ("HOME", "USER", "LOGNAME", "SHELL", "TMPDIR")  # what a CLI needs of this machine to find its own sign-in and settings


class CliFailed(Exception):
    """The CLI did not start, ended with an error, or said nothing."""


class CliStopped(Exception):
    """The run was cancelled while the CLI was working, and the CLI was stopped."""


@dataclass(frozen=True)
class Cli:
    program: str
    args: Callable[[str, Path, list[str]], list[str]]  # (the model's name, the file for its last message, the commands it may run)
    read: Callable[[list[dict]], dict]  # what it printed -> text, tokens_in, cached_in, tokens_out, cost_usd, error; each only when found
    key: str          # the variable it reads an API key from
    prices: str       # whose price list prices its tokens when it reports no cost
    own: tuple = ()   # variables that say where its own files are


def _codex_args(model: str, last: Path, commands: list[str]) -> list[str]:
    return ["exec", "--json", "--skip-git-repo-check", "-s", "workspace-write", "-m", model, "-o", str(last), "-"]


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


def _claude_args(model: str, last: Path, commands: list[str]) -> list[str]:
    allowed = ["--allowedTools", *(f"Bash({c} *)" for c in commands)] if commands else []
    return ["-p", "--output-format", "json", "--permission-mode", "acceptEdits", "--model", model, *allowed]


def _claude_read(printed: list[dict]) -> dict:
    """One result object, with the answer, what was used and what it cost."""
    r = next((e for e in reversed(printed) if e.get("type") == "result"), None)
    if r is None:
        return {}
    u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
    cached = int(u.get("cache_read_input_tokens") or 0)
    found = {"text": r.get("result"), "cost_usd": r.get("total_cost_usd"), "error": r.get("result") or r.get("subtype") if r.get("is_error") else None}
    if u:
        found.update(tokens_in=int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + cached, cached_in=cached,
                     tokens_out=int(u.get("output_tokens") or 0))
    return found


CLIS = {
    "codex": Cli("codex", _codex_args, _codex_read, key="CODEX_API_KEY", prices="openai", own=("CODEX_HOME",)),
    "claude": Cli("claude", _claude_args, _claude_read, key="ANTHROPIC_API_KEY", prices="anthropic", own=("CLAUDE_CONFIG_DIR",)),
}


def tool_commands(registry, names: list[str]) -> list[tuple[str, str, str, list[str]]]:
    """(name, description, program, argument names) for each script tool an agent lists. A CLI runs them itself, as commands.
    A tool that is an MCP server has no command; `check` reports a stage that would need one."""
    out = []
    for name in names:
        spec = registry.tool(name)
        if "run" in spec:
            out.append((name, spec["description"], str((registry.dir("tools", name) / spec["run"]).resolve()), list(spec.get("args") or [])))
    return out


def cli_note(commands: list[tuple[str, str, str, list[str]]]) -> str:
    """What a CLI is told on top of the stage's instructions: where it is, how to ask for a person, and the commands it may run."""
    lines = ["## How you work here",
             "You are in the item's work folder. Read and write files in this folder only, and leave attempts/, verdicts/ and events.jsonl alone.",
             f"You have no tool named ask_for_person. Where the text above says to ask for a person, write {NEEDS_PERSON} "
             "with what you checked and what a person should look at, then stop.",
             "When the result is written, reply with one short line."]
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
                  commands: list[str] = (), stop=None, on_event=lambda *a, **k: None) -> tuple[str, dict]:
    """One try done by the CLI the model entry names. Returns (its last message, what the try used), as run_agent does.
    `out_file` gets everything it printed. The item's time limit and `stop` end it, with everything it started."""
    name = model["harness"]
    cli = CLIS[name]
    if settings.stand_in:
        argv, own = [sys.executable, "-I", STAND_IN, name, str(last_file)], {}
    else:
        argv, own = [cli.program, *cli.args(model["name"], last_file, list(commands))], _env(cli, model)
    last_file.unlink(missing_ok=True)  # the answer has to be this run's
    began = time.time()
    code, out = await run_proc(argv, work, f"{instructions}\n\n{prompt}", {**own, **env}, stop=stop,
                               **{**settings.proc(limits), "timeout": limits["minutes_per_item"] * 60})
    seconds = round(time.time() - began, 2)
    out_file.write_text(out + "\n")

    def failed(how: str, said: str) -> CliFailed:  # the reason, with the end of what the CLI said, within the size a reason may have
        room = limits["error_chars"] - len(how)
        return CliFailed(how + (said[-room:] if room > 0 else ""))

    if code == STOPPED:
        raise CliStopped(name)
    found = cli.read(_printed(out))
    if found.get("error"):  # its own words for what went wrong say more than the end of what it printed
        raise failed(f"{name} ended with an error: ", str(found["error"]))
    if code != 0:
        raise failed(f"{name} exited with code {code}: ", out)
    text = (last_file.read_text(errors="replace") if last_file.is_file() else found.get("text") or "").strip()
    if not text:
        raise CliFailed(f"{name} ended without an answer; what it printed is in {out_file.name}")
    reported = found.get("cost_usd") is not None
    cost = 0.0 if settings.stand_in else _cost(cli, model["name"], found)  # the stand-in costs nothing, which is known
    used = {"tokens_in": found.get("tokens_in") or 0, "tokens_out": found.get("tokens_out") or 0, "cost_usd": cost}
    on_event("cli_run", harness=name, model="stand-in" if settings.stand_in else model["name"], started=round(began, 3), seconds=seconds, reported=reported,
             output=str(out_file.relative_to(work)), said=text[:limits["event_chars"]], **used)
    return text, {"model_calls": 1, "tool_calls": 0, "seconds": seconds, **used}  # one run of the CLI, whatever it called inside


async def cli_version(settings, name: str) -> tuple[bool, str]:
    """(installed, its version line) for one CLI, as `agentflow check` prints it."""
    cli = CLIS[name]
    code, out = await run_proc([cli.program, "--version"], settings.home, env={k: os.environ[k] for k in MACHINE if k in os.environ},
                               **{**settings.proc(), "timeout": settings.engine["health_seconds"]})
    line = (out.strip().splitlines() or [""])[-1][:settings.show["line_chars"]]
    return code == 0, line if code == 0 else f"{cli.program} is not installed or does not start: {line}"
