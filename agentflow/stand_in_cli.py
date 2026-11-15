"""A scripted stand-in for a coding CLI, so a stage handed to Codex or Claude Code can be exercised with no sign-in and at no cost.

With --stand-in the engine starts this in place of the CLI: `stand_in_cli.py <codex|claude> <file for the last message>`,
in the item's work folder, with the instructions and the prompt on stdin. It plays the analyst of the vuln-triage flow:
it runs the commands the instructions name, writes origin.md or asks for a person, and prints what the CLI it stands in
for would print. It has the scripted model's forced failure: one chain link cites no file unless the prompt carries the
check script's complaint.

Run as a file with the standard library only. The scripted model (stand_in.py) uses the two functions below.
"""
import json
import re
import subprocess
import sys
from pathlib import Path


def origin_md(ticket: dict, path: list, image: dict, base: dict, told: bool) -> str:
    """origin.md as the analyst writes it. `told` says the check script's reason reached this try."""
    package = ticket.get("package", "")
    links = [f"{a['name']} depends on {b['name']} {b.get('version', '')} (file: dependency-tree.json)" for a, b in zip(path, path[1:])]
    links.append(f"{package} {image.get('version', '')} is in the {image.get('layer', 'unknown layer')} of the image"
                 + (" (file: image-sbom.json)" if told else ""))
    links.append(f"the base image {'contains' if base.get('found') else 'does not contain'} {package} (file: base-sbom.json)")
    in_app, in_base = len(path) > 1, bool(base.get("found"))
    origin = "both" if in_app and in_base else "base image" if in_base else "app dependency"
    return f"# Origin of {package} in {ticket.get('service', '')}\n\nOrigin: {origin}\n\n## Chain\n" + "".join(f"- {l}\n" for l in links)


def not_found(package: str) -> str:
    """What a person is asked when the package is in neither place."""
    return f"{package} is in neither dependency-tree.json nor image-sbom.json. Check whether the scanner flagged the right service."


def main() -> None:
    shape, last, told = sys.argv[1], Path(sys.argv[2]), sys.stdin.read()
    stage = re.search(r"[Ss]tage '([\w-]+)'", told)
    if not stage or stage.group(1) != "analyse":
        sys.exit("the scripted stand-in CLI only plays the analyse stage of vuln-triage")
    commands = dict(re.findall(r"^- ([\w-]+): .* Command: `(\S+)", told, re.M))  # the commands the engine named

    def ask(name: str, *args: str) -> dict:
        return json.loads(subprocess.run([commands[name], *args], capture_output=True, text=True, check=True).stdout)

    ticket = json.loads(Path("ticket.json").read_text())
    package = ticket.get("package", "")
    path = ask("deps-why", package).get("path", [])
    image, base = ask("sbom-query", "image-sbom.json", package), ask("sbom-query", "base-sbom.json", package)
    if not path and not image.get("found"):
        wrote, said = not_found(package), "Set aside for a person."
        Path("needs-person.md").write_text(wrote)
    else:
        wrote, said = origin_md(ticket, path, image, base, "cites no file" in told), "origin.md is written."
        Path("origin.md").write_text(wrote)
    tokens = {"input_tokens": len(told) // 4, "output_tokens": (len(wrote) + len(said)) // 4}  # a rough count, so the totals are not empty
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
