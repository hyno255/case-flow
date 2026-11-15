"""A stand-in for a coding CLI, so a stage handed to Codex or Claude Code can be exercised with no sign-in and at no cost.

With --stand-in the engine starts this in place of the CLI:
`stand_in_cli.py <codex|claude> <file for the last message> <the flow's stand-in script>`, in the item's work folder,
with the instructions and the prompt on stdin. It knows no flow. It asks the flow's stand-in script what the model would
do next (see `ask`), does it the way a CLI would (reads and writes files, runs the commands the instructions name,
writes needs-person.md), and prints what the CLI it stands in for would print.

Run as a file with the standard library only. The stand-in for the model (stand_in.py) asks the same script the same way, with `question` and `answer`.
"""
import json
import re
import subprocess
import sys
from pathlib import Path


def stage_of(told: str) -> str:
    """The stage a try is for, from the prompt the engine wrote."""
    found = re.search(r"[Ss]tage '([\w-]+)'", told)
    return found.group(1) if found else ""


def question(prompt: str, review: bool, results: list[dict]) -> str:
    """What the flow's stand-in script is asked, on its stdin: the stage, whether a reviewer asks, the prompt, and the
    tool results so far. It prints {"calls": [{"tool", "args"}]}, {"text": ...} or, for a reviewer, {"verdict": {...}}."""
    return json.dumps({"stage": stage_of(prompt), "review": review, "prompt": prompt, "results": results})


def answer(code: int, out: str, err: str) -> dict:
    if code:
        raise RuntimeError(f"the flow's stand-in script failed: {err.strip()}")
    return json.loads(out)


def ask(script: str, prompt: str, results: list[dict]) -> dict:
    done = subprocess.run([sys.executable, "-I", script], input=question(prompt, False, results), capture_output=True, text=True)
    return answer(done.returncode, done.stdout, done.stderr)


def main() -> None:
    shape, last, script, told = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.stdin.read()
    commands = dict(re.findall(r"^- ([\w-]+): .* Command: `(\S+)", told, re.M))  # the commands the engine named
    results, wrote = [], 0
    while True:
        next_ = ask(script, told, results)
        if "text" in next_:
            said = next_["text"]
            break
        for c in next_["calls"]:
            tool, args = c["tool"], c["args"]
            if tool == "read_file":
                content = Path(args["path"]).read_text()
            elif tool in ("write_file", "ask_for_person"):
                path, content = (args["path"], args["content"]) if tool == "write_file" else ("needs-person.md", args["reason"])
                Path(path).write_text(content)
                wrote += len(content)
            else:  # a tool plugin, as the command the instructions name for it; its arguments in the order written
                content = subprocess.run([commands[tool], *map(str, args.values())], capture_output=True, text=True, check=True).stdout
            results.append({"tool": tool, "content": content})
    tokens = {"input_tokens": len(told) // 4, "output_tokens": (wrote + len(said)) // 4}  # a rough count, so the totals are not empty
    if shape == "codex":
        last.write_text(said)
        printed = [{"type": "thread.started", "thread_id": "stand-in"}, {"type": "turn.started"},
                   {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": said}},
                   {"type": "turn.completed", "usage": {**tokens, "cached_input_tokens": 0}}]
    else:
        printed = [{"type": "result", "subtype": "success", "is_error": False, "result": said, "total_cost_usd": 0.0, "usage": tokens}]
    for value in printed:
        print(json.dumps(value))


if __name__ == "__main__":
    main()
