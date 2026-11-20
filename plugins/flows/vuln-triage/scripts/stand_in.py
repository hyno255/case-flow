#!/usr/bin/env python3
"""The flow's stand-in: it answers in place of the model when the flow is run with --stand-in, so the flow can be
exercised with no key and at no cost.

The engine calls it once per model request. On stdin:
    {"stage", "review", "prompt", "results": [{"tool", "content"}]}   # the tool results since the prompt, oldest first
It prints what the model would do next:
    {"calls": [{"tool", "args"}]}   call these tools
    {"text": "..."}                 the stage is done
    {"failed": "..."}               the stage cannot be done, and why
    {"verdict": {"passed", "reason"}}   a reviewer's answer (when "review" is true)

It plays the analyst, the default agent (fix, report) and the reviewer. One forced failure: the analyst leaves a chain
link without a file citation unless the prompt carries the check script's complaint, so a second try can only pass if
the feedback really reached the model.
"""
import json
import re
import sys


def call(tool: str, **args) -> dict:
    return {"calls": [{"tool": tool, "args": args}]}


def loaded(text: str) -> dict:
    try:
        return json.loads(text)
    except ValueError:
        return {}


def of(results: list, tool: str) -> list[str]:
    return [r["content"] for r in results if r["tool"] == tool]


def analyse(prompt: str, results: list) -> dict:
    seen = [r["tool"] for r in results]
    if "read_file" not in seen:
        return call("read_file", path="ticket.json")
    ticket = loaded(of(results, "read_file")[0])
    package = ticket.get("package", "")
    if "sbom-query" not in seen:
        return {"calls": [{"tool": "deps-why", "args": {"package": package}},
                          {"tool": "sbom-query", "args": {"sbom_file": "image-sbom.json", "package": package}},
                          {"tool": "sbom-query", "args": {"sbom_file": "base-sbom.json", "package": package}}]}
    if "write_file" in seen:
        return {"text": "Done."}
    path = loaded(of(results, "deps-why")[0]).get("path", [])
    sboms = {j.get("file"): j for j in map(loaded, of(results, "sbom-query"))}
    image, base = sboms.get("image-sbom.json", {}), sboms.get("base-sbom.json", {})
    if not path and not image.get("found"):
        return {"failed": f"{package} is in neither dependency-tree.json nor image-sbom.json. Check whether the scanner flagged the right service."}
    told = "cites no file" in prompt  # did the check script's reason reach this try?
    links = [f"{a['name']} depends on {b['name']} {b.get('version', '')} (file: dependency-tree.json)" for a, b in zip(path, path[1:])]
    links.append(f"{package} {image.get('version', '')} is in the {image.get('layer', 'unknown layer')} of the image" + (" (file: image-sbom.json)" if told else ""))
    links.append(f"the base image {'contains' if base.get('found') else 'does not contain'} {package} (file: base-sbom.json)")
    in_app, in_base = len(path) > 1, bool(base.get("found"))
    origin = "both" if in_app and in_base else "base image" if in_base else "app dependency"
    return call("write_file", path="origin.md",
                content=f"# Origin of {package} in {ticket.get('service', '')}\n\nOrigin: {origin}\n\n## Chain\n" + "".join(f"- {l}\n" for l in links))


def fix(prompt: str, results: list) -> dict:
    seen, reads = [r["tool"] for r in results], of(results, "read_file")
    if "write_file" in seen:
        return {"text": "Done."}
    if len(reads) < 2:
        return call("read_file", path=("origin.md", "candidates.json")[len(reads)])
    origin, cand = reads[0], json.loads(reads[1])
    lines = []
    if "Origin: app dependency" in origin or "Origin: both" in origin:
        dep = cand.get("direct_dependency") or {}
        fixed = [r for r in dep.get("releases", []) if r["fixed"]]
        pick = next((r for r in fixed if "major" not in r.get("note", "")), fixed[0] if fixed else None)
        if pick:
            lines.append(f"Raise {dep['name']} from {dep['current']} to version {pick['version']}, which brings {pick['brings']}. Risk: low, not a major release.")
    if "Origin: base image" in origin or "Origin: both" in origin:
        tags = [t for t in cand.get("base_image_tags", []) if t["fixed"] and "note" not in t]
        if tags:
            lines.append(f"Rebuild on base image tag {tags[-1]['tag']}, which brings {tags[-1]['brings']}. Risk: low, same base line.")
    if not lines:
        return {"failed": f"candidates.json shows no release or tag with {cand['package']} {cand['fixed_in']} or later. "
                          "Check whether the fix has been published yet."}
    return call("write_file", path="fix.md", content=f"# Fix for {cand['package']}\n\n" + "\n".join(lines) + "\n\nEvidence: candidates.json\n")


def report(prompt: str, results: list) -> dict:
    seen, reads = [r["tool"] for r in results], of(results, "read_file")
    if "write_file" in seen:
        return {"text": "Done."}
    if len(reads) < 2:
        return call("read_file", path=("origin.md", "fix.md")[len(reads)])
    origin, fixed = reads
    kind = re.search(r"^Origin: (.+)$", origin, re.M).group(1)
    title = origin.splitlines()[0].lstrip("# ")
    chain = origin.split("## Chain", 1)[1].strip()
    first_fix = next(l for l in fixed.splitlines()[1:] if l.strip())
    return call("write_file", path="report.md", content=(
        f"# {title}\n\nFinding: The package comes from the {kind}.\nFix: {first_fix.split('. Risk')[0]}.\n\n"
        f"## Where it comes from\n{kind.capitalize()}.\n\n## The chain\n{chain}\n\n## Suggested fix\n{first_fix}\n\n"
        "## Evidence\norigin.md, fix.md, dependency-tree.json, image-sbom.json, base-sbom.json, candidates.json\n"))


def review(stage: str, results: list) -> dict:
    target = {"analyse": "origin.md", "fix": "fix.md"}.get(stage, "report.md")
    if not results:
        return call("read_file", path=target)
    text = results[0]["content"]
    if stage == "analyse":
        links = [l for l in text.splitlines() if l.startswith("- ")]
        passed = "Origin: " in text and bool(links) and all("(file: " in l for l in links)
    elif stage == "fix":
        passed = bool(re.search(r"version \d|tag \d", text))
    else:
        passed = "Finding: " in text and "Fix: " in text
    reason = f"{target} satisfies the sentence." if passed else f"{target} does not show what the sentence asks for."
    return {"verdict": {"passed": passed, "reason": reason}}


def main() -> None:
    asked = json.load(sys.stdin)
    if asked["review"]:
        answer = review(asked["stage"], asked["results"])
    else:
        answer = {"analyse": analyse, "fix": fix, "report": report}[asked["stage"]](asked["prompt"], asked["results"])
    print(json.dumps(answer))


if __name__ == "__main__":
    main()
