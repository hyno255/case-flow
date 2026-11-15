"""The web UI and the JSON API. Pages are rendered by the server and refresh from the event stream.

Everything the pages show comes from views.py (runs and items) and catalog.py (what is installed, and the settings), which
only read the store and the folders, so a run started from the terminal in another process shows up here the same way as
one started from the page. Every page has a JSON twin at the same address under /api.

There is no sign-in. The server listens on this machine only, refuses other Host names, and accepts a
change (starting a flow, switching a schedule, checking a tool) only with a header that a page on another site cannot send.
"""
import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..agents import ModelNotReady
from ..health import EVERY, check_tools
from ..layout import REPORT, confined
from ..plugins import installed
from ..recovery import claim_resume, prepare_retry, request_cancel, resume_run, run_retry, sweep
from ..runner import run_due, start_flow
from ..schedule import set_schedule, tick
from . import catalog, views

HERE = Path(__file__).parent
REPORT_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html")


class RunRequest(BaseModel):
    force: bool = False


class ScheduleRequest(BaseModel):
    enabled: bool


def create_app(settings, store, schedule: bool = True, hosts: tuple[str, ...] = ("127.0.0.1", "localhost")) -> FastAPI:
    tasks: set[asyncio.Task] = set()
    errors: dict[str, str] = {}  # the last reason something could not go on, shown on its page; keys: flows/<name>, a run id

    def spawn(keys: list[str], work) -> None:
        async def job():
            try:
                for key in keys:
                    errors.pop(key, None)
                await work
            except Exception as e:
                errors.update({key: f"{type(e).__name__}: {e}" for key in keys})
        task = asyncio.create_task(job())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def scheduler():
        while True:
            due: dict[tuple[str, str], list[str]] = {}
            for flow, source, slot in tick(settings, store):
                due.setdefault((source, slot), []).append(flow)
            for (source, slot), flows in due.items():  # flows due together on one source share one fetch
                spawn([f"flows/{f}" for f in flows], run_due(settings, store, source, flows, slot))
            await asyncio.sleep(30)

    async def health():
        while True:
            try:
                await check_tools(settings, store)
            except Exception:  # a round that could not be made is made again at the next turn
                pass
            await asyncio.sleep(EVERY)

    async def watchdog():
        while True:
            sweep(store)  # records runs whose process is gone; it never resumes one by itself
            await asyncio.sleep(15)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        jobs = [asyncio.create_task(scheduler()), asyncio.create_task(health())] if schedule else []
        watch = asyncio.create_task(watchdog())
        yield
        stopping = [*jobs, watch, *tasks]
        for t in stopping:
            t.cancel()
        await asyncio.gather(*stopping, return_exceptions=True)  # running scripts are stopped before the server is gone

    app = FastAPI(title="agentflow", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(hosts))
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    pages = Jinja2Templates(directory=HERE / "templates")
    pages.env.trim_blocks = pages.env.lstrip_blocks = True
    pages.env.filters["num"] = lambda n: f"{n:,}"
    pages.env.globals["static"] = lambda name: f"/static/{name}?v={int((HERE / 'static' / name).stat().st_mtime)}"  # a changed file is a new address, so no browser shows an old one

    @app.middleware("http")
    async def changes_need_the_header(request: Request, call_next):
        if request.method not in ("GET", "HEAD") and request.headers.get("x-agentflow") != "1":
            return PlainTextResponse("changes need the header X-Agentflow: 1", status_code=403)
        return await call_next(request)

    def found(view):
        if view is None:
            raise HTTPException(404, "not found")
        return view

    def both(path: str):
        """Registers one function for a page and, under /api, for the same view as JSON."""
        def register(fn):
            app.get(path, response_class=HTMLResponse)(fn)
            app.get("/api" + path)(fn)
            return fn
        return register

    def show(request: Request, template: str, section: str, view):
        """The view as a page, or as JSON when it was asked for under /api. ?fragment=1 returns only the part of the page that changes."""
        v = found(view)
        if request.url.path.startswith("/api/"):
            return JSONResponse(v)
        return pages.TemplateResponse(request, template, {"v": v, "nav": views.nav(settings, store, section, errors), "fragment": "fragment" in request.query_params})

    def plain_file(folder: Path, path: str) -> PlainTextResponse:
        """One file of a folder, always as plain text, never run as a page."""
        try:
            target = confined(folder, path)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not target.is_file():
            raise HTTPException(404, "no such file")
        return PlainTextResponse(target.read_text(errors="replace")[:200_000])

    # ---- runs: the list, a run and its tabs, an item and its tabs ----
    @app.get("/", response_class=HTMLResponse)
    async def runs_page(request: Request):
        return show(request, "runs.html", "runs", views.runs_view(settings, store, errors))

    @app.get("/api/runs")
    async def api_runs():
        return views.run_rows(store)

    @both("/runs/{run_id}")
    @both("/runs/{run_id}/{tab}")
    async def run_page(request: Request, run_id: str, tab: str = "items", span: str = ""):
        return show(request, "run.html", "runs", views.run_view(settings, store, run_id, tab, span, errors.get(run_id, "")))

    @both("/runs/{run_id}/items/{item_id}")
    @both("/runs/{run_id}/items/{item_id}/{tab}")
    async def item_page(request: Request, run_id: str, item_id: str, tab: str = "output", span: str = "", file: str = ""):
        return show(request, "item.html", "runs", views.item_view(settings, store, run_id, item_id, tab, span, file))

    @app.get("/runs/{run_id}/items/{item_id}/files/{path:path}", response_class=PlainTextResponse)
    async def item_file(run_id: str, item_id: str, path: str):
        return plain_file(store.path(found(store.item(run_id, item_id))), path)  # a carried-over item's row names the folder that holds its work

    @app.get("/runs/{run_id}/report/{name}")
    async def report_file(run_id: str, name: str):
        target = store.path(found(store.run(run_id))) / REPORT / name
        if not REPORT_FILE.fullmatch(name) or not target.is_file():
            raise HTTPException(404, "no such report page")
        return FileResponse(target, media_type="text/html")

    # ---- design: what is installed, one list and one page with tabs per kind of plugin ----
    def plugin_rows(kind: str) -> list[dict]:
        return {"flows": lambda: catalog.flow_rows(settings, store, errors), "agents": lambda: catalog.agent_rows(settings),
                "tools": lambda: catalog.tool_rows(settings, store), "sources": lambda: catalog.source_rows(settings, store)}[kind]()

    def plugin_view(kind: str, name: str, tab: str = "", file: str = "") -> dict | None:
        tab = tab or catalog.TABS.get(kind, ("",))[0]
        if kind == "flows":
            return catalog.flow_view(settings, store, name, tab, file, errors)
        if kind == "agents":
            return catalog.agent_view(settings, name, tab, file)
        if kind == "tools":
            return catalog.tool_view(settings, store, name, tab, file)
        if kind == "sources":
            return catalog.source_view(settings, store, name, tab, file)
        return None

    def plugin_pages(kind: str) -> None:
        @app.get(f"/{kind}", response_class=HTMLResponse)
        async def list_page(request: Request):
            return show(request, "plugins.html", kind, {"kind": kind, "rows": plugin_rows(kind), "folder": str(settings.plugins / kind)})

        @app.get(f"/api/{kind}")
        async def list_api():
            return plugin_rows(kind)

        @both(f"/{kind}/{{name}}")
        @both(f"/{kind}/{{name}}/{{tab}}")
        async def one_page(request: Request, name: str, tab: str = "", file: str = ""):
            return show(request, "flow.html" if kind == "flows" else "plugin.html", kind, plugin_view(kind, name, tab, file))

    for kind in catalog.KINDS:
        plugin_pages(kind)

    @app.get("/plugins/{kind}/{name}/files/{path:path}", response_class=PlainTextResponse)
    async def plugin_file(kind: str, name: str, path: str):
        if kind not in catalog.KINDS or name not in installed(settings).names(kind):
            raise HTTPException(404, "not found")
        return plain_file(settings.plugins / kind / name, path)

    @app.get("/api/plugins")
    async def api_plugins():
        return catalog.plugins_view(settings, store, errors)

    @app.get("/api/plugins/{kind}/{name}")
    async def api_plugin(kind: str, name: str):
        return found(plugin_view(kind, name))

    # ---- system ----
    @both("/settings")
    async def settings_page(request: Request):
        return show(request, "settings.html", "settings", catalog.settings_view(settings))

    # ---- changes: each needs the header, see above ----
    @app.post("/api/flows/{flow}/run", status_code=202)
    async def api_run_flow(flow: str, body: RunRequest):
        """Start one flow: its source is fetched now, and this flow alone runs over what came back."""
        live = installed(settings)
        if flow not in live.names("flows"):
            raise HTTPException(404, f"no flow '{flow}'")
        errs = live.check_flow(flow)
        if errs:
            raise HTTPException(400, "the flow does not pass its checks: " + "; ".join(errs))
        spawn([f"flows/{flow}"], start_flow(settings, store, flow, force=body.force))
        return {"started": flow, "force": body.force, "go": f"/flows/{flow}/runs"}

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def api_resume(run_id: str):
        try:
            claim_resume(store, run_id)  # decided here, so the answer says whether this request got the run
        except ValueError as e:
            raise HTTPException(409, str(e))
        spawn([run_id], resume_run(settings, store, run_id, claimed=True))
        return {"resumed": run_id, "go": f"/runs/{run_id}"}

    @app.post("/api/runs/{run_id}/cancel")
    async def api_cancel(run_id: str):
        try:
            return {"run": run_id, "result": request_cancel(settings, store, run_id)}
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/runs/{run_id}/items/{item_id}/retry", status_code=202)
    async def api_retry(run_id: str, item_id: str):
        try:
            new = prepare_retry(settings, store, run_id, item_id)
        except (ValueError, ModelNotReady) as e:
            raise HTTPException(400, str(e))
        spawn([new], run_retry(settings, store, new))
        return {"run": new, "go": f"/runs/{new}/items/{item_id}"}

    @app.post("/api/flows/{flow}/schedule")
    async def api_schedule(flow: str, body: ScheduleRequest):
        try:
            return {"flow": flow, "enabled": body.enabled, "next_due": set_schedule(settings, store, flow, body.enabled)}
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/tools/health")
    async def api_health():
        """Check every tool now."""
        return await check_tools(settings, store)

    @app.post("/api/tools/{name}/health")
    async def api_tool_health(name: str):
        if name not in installed(settings).names("tools"):
            raise HTTPException(404, f"no tool '{name}'")
        return (await check_tools(settings, store, only=name))[0]

    @app.get("/api/events")
    async def api_events(request: Request, run: str | None = None, item: str | None = None, after: int = 0, follow: bool = True):
        """Replays events after a sequence number, then follows. The pages read this."""
        last = int(request.headers.get("last-event-id") or after)

        async def stream():
            nonlocal last
            quiet = 0
            while True:
                rows = store.events(run, item, after=last, limit=500)
                for event in rows:
                    last = event["seq"]
                    yield f"id: {last}\ndata: {json.dumps(event)}\n\n"
                if len(rows) == 500:
                    continue
                if not follow or await request.is_disconnected():
                    return
                quiet = 0 if rows else quiet + 1
                if quiet and quiet % 30 == 0:
                    yield ": still here\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
