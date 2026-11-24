#!/usr/bin/env python3
"""A very small MCP server over stdio, with the standard library only, so it needs nothing installed.

Two tools: `where` says what system it runs on, `note` writes a line into the current folder.
A real one would be written with an MCP SDK.
"""
import json
import platform
import sys
from pathlib import Path

TOOLS = [
    {"name": "where", "description": "Say which system this server runs on.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "note", "description": "Append one line to notes.txt in the work folder.",
     "inputSchema": {"type": "object", "properties": {"line": {"type": "string"}}, "required": ["line"], "additionalProperties": False}},
]


def call(name: str, args: dict) -> str:
    if name == "where":
        return f"{platform.system()} · home visible: {Path('/Users').exists()} · cwd {Path.cwd()}"
    with open("notes.txt", "a") as f:
        f.write(args["line"] + "\n")
    return "noted"


def answer(msg: dict):
    method, params = msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                "serverInfo": {"name": "notes", "version": "0.1"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            return {"content": [{"type": "text", "text": call(params["name"], params.get("arguments") or {})}], "isError": False}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    if method == "ping":
        return {}
    return None


for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue  # a notification: nothing to answer
    result = answer(msg)
    reply = {"jsonrpc": "2.0", "id": msg["id"], **({"result": result} if result is not None else {"error": {"code": -32601, "message": "method not found"}})}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
