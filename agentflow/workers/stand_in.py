"""A stand-in for the model, so a flow can be exercised with no key and at no cost.

It knows no flow. What the model would say comes from the flow's own stand-in script (`stand_in` in flow.yaml), which is
asked once per model request with the stage, the prompt and the tool results so far; stand_in_cli.py says how.
"""
import asyncio
import json
import sys

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from agentflow.workers.stand_in_cli import answer, question


def build(script) -> FunctionModel:
    """The model that answers as the flow's stand-in script says. `script` may be None where the model is only built, not asked."""

    async def scripted(messages, info: AgentInfo) -> ModelResponse:
        parts = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts]
        prompt = "\n".join(str(p.content) for p in parts if isinstance(p, UserPromptPart))
        results = [{"tool": p.tool_name, "content": p.content if isinstance(p.content, str) else json.dumps(p.content, default=str)}
                   for p in parts if isinstance(p, ToolReturnPart)]
        proc = await asyncio.create_subprocess_exec(sys.executable, "-I", str(script), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate(question(prompt, bool(info.output_tools), results).encode())
        said = answer(proc.returncode, out.decode(), err.decode())
        if "verdict" in said:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, said["verdict"])])
        if "text" in said:
            return ModelResponse(parts=[TextPart(said["text"])])
        return ModelResponse(parts=[ToolCallPart(c["tool"], c["args"]) for c in said["calls"]])

    async def stream(messages, info: AgentInfo):
        """The same answer, sent as a stream, so the engine's live event handler is exercised."""
        for i, part in enumerate((await scripted(messages, info)).parts):
            if isinstance(part, TextPart):
                yield part.content
            else:
                yield {i: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args))}

    return FunctionModel(scripted, stream_function=stream, model_name="stand-in-scripted")
