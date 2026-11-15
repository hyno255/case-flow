"""A scripted stand-in for the model, so the engine can be exercised with no key and at no cost.

It only knows the vuln-triage flow. It plays the analyst, the default agent (fix, report) and the checker.
One forced failure: the analyst leaves a chain link without a file citation unless the prompt carries the
check script's complaint, so a second try can only pass if the feedback really reached the model.
"""
import json
import re

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel


def _user_text(messages) -> str:
    return "\n".join(str(p.content) for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, UserPromptPart))


def _returns(messages) -> list[ToolReturnPart]:
    return [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]


def _json(part: ToolReturnPart) -> dict:
    try:
        return json.loads(part.content) if isinstance(part.content, str) else dict(part.content)
    except Exception:
        return {}


def _call(name: str, **args) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(name, args)])


def _done(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def _read(returns, n: int) -> str:
    reads = [r for r in returns if r.tool_name == "read_file"]
    return str(reads[n].content) if len(reads) > n else ""


def _analyse(messages) -> ModelResponse:
    returns = _returns(messages)
    seen = [r.tool_name for r in returns]
    if "read_file" not in seen:
        return _call("read_file", path="ticket.json")
    ticket = _json(next(r for r in returns if r.tool_name == "read_file"))
    package = ticket.get("package", "")
    if "sbom-query" not in seen:
        return ModelResponse(parts=[
            ToolCallPart("deps-why", {"package": package}),
            ToolCallPart("sbom-query", {"sbom_file": "image-sbom.json", "package": package}),
            ToolCallPart("sbom-query", {"sbom_file": "base-sbom.json", "package": package}),
        ])
    if "write_file" in seen or "ask_for_person" in seen:
        return _done("Done.")
    path = _json(next(r for r in returns if r.tool_name == "deps-why")).get("path", [])
    sboms = {j["file"]: j for j in map(_json, (r for r in returns if r.tool_name == "sbom-query"))}
    image, base = sboms.get("image-sbom.json", {}), sboms.get("base-sbom.json", {})
    if not path and not image.get("found"):
        return _call("ask_for_person", reason=f"{package} is in neither dependency-tree.json nor image-sbom.json. "
                                              "Check whether the scanner flagged the right service.")
    told = "cites no file" in _user_text(messages)  # did the check script's reason reach this run?
    links = [f"{a['name']} depends on {b['name']} {b.get('version', '')} (file: dependency-tree.json)" for a, b in zip(path, path[1:])]
    links.append(f"{package} {image.get('version', '')} is in the {image.get('layer', 'unknown layer')} of the image"
                 + (" (file: image-sbom.json)" if told else ""))
    links.append(f"the base image {'contains' if base.get('found') else 'does not contain'} {package} (file: base-sbom.json)")
    in_app, in_base = len(path) > 1, bool(base.get("found"))
    origin = "both" if in_app and in_base else "base image" if in_base else "app dependency"
    body = f"# Origin of {package} in {ticket.get('service', '')}\n\nOrigin: {origin}\n\n## Chain\n" + "".join(f"- {l}\n" for l in links)
    return _call("write_file", path="origin.md", content=body)


def _fix(messages) -> ModelResponse:
    returns = _returns(messages)
    seen = [r.tool_name for r in returns]
    if "write_file" in seen or "ask_for_person" in seen:
        return _done("Done.")
    if seen.count("read_file") < 2:
        return _call("read_file", path=("origin.md", "candidates.json")[seen.count("read_file")])
    origin, cand = _read(returns, 0), json.loads(_read(returns, 1))
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
        return _call("ask_for_person", reason=f"candidates.json shows no release or tag with {cand['package']} {cand['fixed_in']} or later. "
                                              "Check whether the fix has been published yet.")
    return _call("write_file", path="fix.md", content=f"# Fix for {cand['package']}\n\n" + "\n".join(lines) + "\n\nEvidence: candidates.json\n")


def _report(messages) -> ModelResponse:
    returns = _returns(messages)
    seen = [r.tool_name for r in returns]
    if "write_file" in seen:
        return _done("Done.")
    if seen.count("read_file") < 2:
        return _call("read_file", path=("origin.md", "fix.md")[seen.count("read_file")])
    origin, fix = _read(returns, 0), _read(returns, 1)
    kind = re.search(r"^Origin: (.+)$", origin, re.M).group(1)
    title = origin.splitlines()[0].lstrip("# ")
    chain = origin.split("## Chain", 1)[1].strip()
    first_fix = next(l for l in fix.splitlines()[1:] if l.strip())
    body = (f"# {title}\n\nFinding: The package comes from the {kind}.\nFix: {first_fix.split('. Risk')[0]}.\n\n"
            f"## Where it comes from\n{kind.capitalize()}.\n\n## The chain\n{chain}\n\n## Suggested fix\n{first_fix}\n\n"
            "## Evidence\norigin.md, fix.md, dependency-tree.json, image-sbom.json, base-sbom.json, candidates.json\n")
    return _call("write_file", path="report.md", content=body)


def _checker(messages, info: AgentInfo, stage: str) -> ModelResponse:
    returns = _returns(messages)
    target = {"analyse": "origin.md", "fix": "fix.md", "report": "report.md"}.get(stage, "report.md")
    if not returns:
        return _call("read_file", path=target)
    text = str(returns[0].content)
    if stage == "analyse":
        links = [l for l in text.splitlines() if l.startswith("- ")]
        passed = "Origin: " in text and bool(links) and all("(file: " in l for l in links)
    elif stage == "fix":
        passed = bool(re.search(r"version \d|tag \d", text))
    else:
        passed = "Finding: " in text and "Fix: " in text
    reason = f"{target} satisfies the sentence." if passed else f"{target} does not show what the sentence asks for."
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"passed": passed, "reason": reason, "needs_person": False})])


def scripted(messages, info: AgentInfo) -> ModelResponse:
    m = re.search(r"[Ss]tage '([\w-]+)'", _user_text(messages))
    stage = m.group(1) if m else ""
    if info.output_tools:
        return _checker(messages, info, stage)
    return {"analyse": _analyse, "fix": _fix, "report": _report}[stage](messages)


async def scripted_stream(messages, info: AgentInfo):
    """The same script, sent as a stream, so the engine's live event handler is exercised."""
    for i, part in enumerate(scripted(messages, info).parts):
        if isinstance(part, TextPart):
            yield part.content
        else:
            yield {i: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args))}


def build() -> FunctionModel:
    return FunctionModel(scripted, stream_function=scripted_stream, model_name="stand-in-scripted")
