"""Whether each tool works right now. A script tool is healthy when its `health` script exits with 0; an MCP server
when it starts and lists its tools. The server checks every tool when it starts and every few minutes, and a person can
ask for a check; the last answer is kept in the store, so every page can show it without running anything."""
import asyncio

from agentflow.workers.agents import plugin_tool
from agentflow.plugins.registry import installed
from agentflow.workers.procs import run_proc
from agentflow.records.store import now


async def tool_health(settings, live, name: str) -> tuple[bool, str]:
    """(healthy, one line saying why)."""
    try:
        errs = live.check_tool(name)
    except Exception as e:
        errs = [f"{type(e).__name__}: {e}"]
    if errs:
        return False, f"tool.yaml: {errs[0]}"
    spec, folder, env = live.tool(name), live.dir("tools", name), {}
    seconds, line = settings.internal["health_seconds"], settings.internal["line_chars"]
    if "mcp" in spec:
        try:
            offered = await asyncio.wait_for(plugin_tool(settings, live, name, folder, env).offered(seconds), seconds)
        except Exception as e:
            return False, f"the server did not start or did not answer: {type(e).__name__}: {str(e)[:line]}"
        return bool(offered), f"offers {', '.join(offered)}" if offered else "the server started but offers no tool"
    code, out = await run_proc([str((folder / spec["health"]).resolve())], cwd=folder, env=env, **{**settings.proc(), "timeout": seconds})
    last = (out.strip().splitlines() or [""])[-1][:line]
    return code == 0, last or f"{spec['health']} exited with code {code}"


async def check_tools(settings, store, only: str | None = None) -> list[dict]:
    """Check every tool (or one) and keep the answers. Tools that are gone are forgotten."""
    live = installed(settings)
    names = [n for n in live.names("tools") if only in (None, n)]

    async def one(name: str) -> dict:
        ok, detail = await tool_health(settings, live, name)
        row = {"tool": name, "ok": ok, "detail": detail, "checked": now()}
        store.db.execute("INSERT OR REPLACE INTO health (tool, ok, detail, checked) VALUES (?,?,?,?)", (name, int(ok), detail, row["checked"]))
        return row

    rows = list(await asyncio.gather(*(one(n) for n in names)))
    if only is None:
        store.db.execute(f"DELETE FROM health WHERE tool NOT IN ({','.join('?' * len(names))})", names)
    return rows
