"""Web UI and API tests, on the scripted stand-in model: no key, no network, no cost."""
import json
import os
import re
import shutil
import time
import unittest

from fastapi.testclient import TestClient

from agentflow.records import trace
from agentflow.settings import DEFAULTS, Settings
from support import CHANGE, ROOT, Home, make_app

FLOW = "plugins/flows/vuln-triage/flow.yaml"
ITEMS = ("SEC-1042", "SEC-1043", "SEC-1044", "SEC-1045")
DESIGN_PAGES = ["/flows", "/flows/vuln-triage", "/flows/vuln-triage/files", "/flows/vuln-triage/runs", "/agents", "/agents/origin-analyst",
                "/agents/origin-analyst/method", "/agents/origin-analyst/files", "/tools", "/tools/sbom-query", "/tools/sbom-query/files", "/sources",
                "/sources/security-tickets", "/sources/security-tickets/fetches", "/sources/security-tickets/files", "/settings"]


def run_pages(run_id: str, items=ITEMS) -> list[str]:
    """Every page and tab of a run and of its items."""
    urls = [f"/runs/{run_id}{tab}" for tab in ("", "/output", "/trace", "/flow")]
    return urls + [f"/runs/{run_id}/items/{i}{tab}" for i in items for tab in ("", "/trace")]


def spans(view: dict, kind: str | None = None) -> list[dict]:
    return [s for s in trace.walk(view["trace"]["root"]) if kind in (None, s["kind"])]


class Site(Home):
    """A home and the app on it."""

    def setUp(self):
        super().setUp()
        self.app = make_app(self.settings, self.store)

    def run_once(self, c) -> str:
        started = c.post("/api/flows/vuln-triage/run", headers=CHANGE)  # a run is started from its flow
        self.assertEqual((started.status_code, started.json()["go"]), (202, "/flows/vuln-triage/runs"))
        return self.finished_run(c)

    def edit_flow(self, old: str, new: str) -> None:
        file = self.home / FLOW
        self.assertIn(old, file.read_text())
        file.write_text(file.read_text().replace(old, new))


class Runs(Site):

    def test_every_page_and_tab_of_a_run(self):
        with TestClient(self.app) as c:
            self.assertIn("No runs yet.", c.get("/").text)
            run_id = self.run_once(c)
            for url in ["/"] + run_pages(run_id) + DESIGN_PAGES:
                page = c.get(url)
                self.assertEqual(page.status_code, 200, url)
                self.assertNotIn("unknown", page.text.lower(), url)  # what is not known is a dash with the reason in its tooltip
                self.assertIn('<nav class="side"', page.text, url)
                part = c.get(url + "?fragment=1")  # the live refresh asks for the part that changes, without the shell
                self.assertEqual(part.status_code, 200, url)
                self.assertNotIn("<html", part.text, url)
                self.assertIn('<header class="bar"', part.text, url)
                if url != "/":
                    self.assertEqual(c.get("/api" + url).status_code, 200, url)  # the same view as JSON
            for url in ("/runs/r_none", f"/runs/{run_id}/nope", f"/runs/{run_id}/items/SEC-9", f"/runs/{run_id}/items/SEC-1042/nope", "/api/runs/r_none"):
                self.assertEqual(c.get(url).status_code, 404, url)

            # the sidebar is real pages, and the page a person is on is marked
            home = c.get("/").text
            for href in ("/flows", "/agents", "/tools", "/sources", "/settings"):
                self.assertIn(f'<a href="{href}"', home)
            self.assertIn('<a href="/" aria-current="page">Runs', home)
            self.assertIn('<a href="/flows" aria-current="page">Flows', c.get("/flows/vuln-triage/files").text)

            # the list of runs: counts, cost, and the flow's output to open
            row = c.get("/api/runs").json()[0]
            self.assertEqual([(k["n"], k["label"]) for k in row["counts"]], [(2, "done"), (2, "failed")])
            self.assertEqual(row["cost"]["text"], "$0")  # the stand-in is free, which is known
            for text in (run_id, "2 done", "2 failed", f'href="/runs/{run_id}/output/index.html"'):
                self.assertIn(text, home)
            # the list of runs starts nothing: there may be many flows, and each is started from its own page
            for page in (home, c.get("/sources").text, c.get("/sources/security-tickets").text, c.get("/flows").text):
                self.assertNotIn("/run\"", page)
            flow_page = c.get("/flows/vuln-triage").text
            for text in ('data-post="/api/flows/vuln-triage/run"', ">Run<"):
                self.assertIn(text, flow_page)
            self.assertEqual(flow_page.count("/api/flows/vuln-triage/run"), 1)  # one way to run a flow
            for page in (home, flow_page):  # the model is a flow's, a stage's or an agent's, so the sidebar names none
                self.assertNotIn("gpt-6-luna", page.split("</nav>")[0])
                self.assertNotIn("scripted stand-in", page.split("</nav>")[0])

    def test_the_items_of_a_run(self):
        with TestClient(self.app) as c:
            run_id = self.run_once(c)
            run = c.get(f"/api/runs/{run_id}").json()
            self.assertEqual({i["id"]: (i["status"], i["stage"]) for i in run["items"]},
                             {"SEC-1042": ("done", "report"), "SEC-1043": ("done", "report"), "SEC-1044": ("failed", "fix"), "SEC-1045": ("failed", "analyse")})
            self.assertEqual(run["items"][0]["title"], "checkout-api · libexample-xml")
            self.assertEqual((run["status"], run["by"]), ("finished", "by hand"))
            page = c.get(f"/runs/{run_id}").text
            for item in run["items"]:
                self.assertTrue(os.path.isdir(item["folder"]))
                # each row names the work folder from the run folder, with the whole path in its tooltip
                self.assertIn(f'<code class="path" title="{item["folder"]}">items/{item["id"]}</code>', page)
            self.assertIn(run["items"][2]["reason"], page)  # why an item is not done is in its row, in full
            self.assertIn("candidates.json shows no release", run["items"][2]["reason"])
            for text in (run["snapshot"], "of $2.00"):
                self.assertIn(text, page)
            self.assertNotIn(run["folder"], page.split("<main>")[0])  # a page's head names no folder
            for folder in (self.settings.data, self.settings.plugins, self.settings.home / "agentflow.yaml"):
                self.assertIn(f'<code class="path" title="{folder}">', c.get("/settings").text)

            # the Output tab lists what the flow put in the run's output folder; next to it the engine keeps the run's log
            out = c.get(f"/api/runs/{run_id}/output").json()
            self.assertEqual([f["path"] for f in out["files"]], ["SEC-1042.html", "SEC-1043.html", "SEC-1044.html", "SEC-1045.html", "index.html", "run.log"])
            self.assertEqual((out["finish"], out["files"][0]["href"], out["more"]), ("scripts/report.py", f"/runs/{run_id}/output/SEC-1042.html", 0))
            self.assertIn(f'<a href="/runs/{run_id}/output/index.html" target="_blank"', c.get(f"/runs/{run_id}/output").text)

            # the flow as this run saw it, with what the run did at each stage
            nodes = {n["id"]: n for n in c.get(f"/api/runs/{run_id}/flow").json()["graph"]["nodes"]}
            self.assertEqual(list(nodes), [":source", "gather", "analyse", "fix", "report", ":end"])
            self.assertEqual(nodes[":end"]["lines"], ["scripts/report.py", "6 files"])
            self.assertEqual((nodes["analyse"]["state"], nodes["analyse"]["lines"][-2:]), ("done", ["3 passed", "7 tries"]))  # SEC-1045's analyst ended analyse as failed
            self.assertEqual((nodes[":end"]["state"], nodes[":end"]["href"]), ("done", f"/runs/{run_id}/output"))  # a run ends at its output folder
            stages = c.get(f"/api/runs/{run_id}/flow").json()["stages"]
            self.assertEqual([(s["id"], s["by"], s["passed"], s["tries"], s["now"]) for s in stages][:2],
                             [("gather", "script scripts/gather.sh", 4, 4, 0), ("analyse", "agent origin-analyst", 3, 7, 0)])
            self.assertIn("<svg", c.get(f"/runs/{run_id}/flow").text)

            # an item: its files, and why it stopped. The engine knows no file of a flow by name and renders none
            done = c.get(f"/runs/{run_id}/items/SEC-1042").text
            for text in ("files?file=report.md", "/items/SEC-1042/base-sbom.json"):
                self.assertIn(text, done)
            for tab in ("", "/trace"):
                self.assertIn('<p class="alert">analyse: origin-analyst could not do it: libexample-png is in neither dependency-tree.json nor image-sbom.json', c.get(f"/runs/{run_id}/items/SEC-1045{tab}").text)


            # a finished run's pages are not fetched again, and there is no log tab and no event stream: the run's lines are a file in its output
            self.assertNotIn("data-refresh", c.get(f"/runs/{run_id}/trace").text)
            self.assertEqual((c.get(f"/runs/{run_id}/log").status_code, c.get(f"/api/events?run={run_id}").status_code), (404, 404))
            lines = c.get(f"/runs/{run_id}/output/run.log").text
            for text in ("run       started: vuln-triage, 4 items", "SEC-1042  analyse  try 2  origin-analyst  (with feedback)", "SEC-1045  ended: failed", "run       ended"):
                self.assertIn(text, lines)

    def test_the_trace_names_who_ran_what(self):
        self.edit_flow("    check: scripts/chain-cites-files.py\n", "    check: scripts/chain-cites-files.py\n    on_fail: gather\n")
        with TestClient(self.app) as c:
            run_id = self.run_once(c)
            run = c.get(f"/api/runs/{run_id}/trace").json()
            root = run["trace"]["root"]
            self.assertEqual([(s["kind"], s["name"]) for s in root["children"][:5]], [("fetch", "security-tickets")] + [("item", i) for i in ITEMS])
            self.assertEqual([s["name"] for s in root["children"] if s["kind"] == "mark"], ["finish script"])  # the flow's own script makes the output; the engine sends nothing anywhere
            for s in spans(run):  # every bar sits on the line, and no part begins before the run or ends after it
                self.assertTrue(0 <= s["left"] <= 100 and 0 <= s["width"] and s["left"] + s["width"] <= 100.01, s["id"])
                self.assertTrue(root["start"] - 0.001 <= s["start"] <= s["end"] <= root["end"] + 0.001, s["id"])
            self.assertEqual(spans(run, "model") + spans(run, "tool"), [])  # the run's tree stops at the parts of a try
            self.assertEqual(root["tokens"], c.get(f"/api/runs/{run_id}").json()["tokens"])  # parents add up what is under them
            self.assertEqual(sum(s["tokens"] for s in root["children"]), root["tokens"])
            page = c.get(f"/runs/{run_id}/trace").text
            for text in ('class="pane"', f'href="/runs/{run_id}/items/SEC-1042/trace"', "4 model calls · 5 tool calls"):
                self.assertIn(text, page)
            self.assertNotIn('class="kd k-model"', page)

            # an item's trace goes down to each call
            item = c.get(f"/api/runs/{run_id}/items/SEC-1042/trace").json()
            top = item["trace"]["root"]
            self.assertEqual((top["kind"], top["name"], [s["name"] for s in top["children"]]), ("item", "SEC-1042", ["gather", "analyse", "fix", "report"]))
            gather, analyse = top["children"][0], top["children"][1]
            # analyse was not accepted and its fail edge leads to gather: both ran twice, and the way back is a row
            self.assertEqual([(s["kind"], s["name"], s["note"]) for s in analyse["children"]],
                             [("try", "try 1", "not accepted"), ("mark", "sent back to gather", ""), ("try", "try 2", "passed")])
            self.assertEqual([s["name"] for s in gather["children"]], ["try 1", "try 2"])
            first, second = analyse["children"][0], analyse["children"][2]
            self.assertEqual([(p["kind"], p["name"], p["who"]) for p in first["children"]],
                             [("agent", "execute", "origin-analyst"), ("check", "scripts/chain-cites-files.py", "script")])
            self.assertEqual([(p["kind"], p["who"], p["note"]) for p in second["children"]],
                             [("agent", "origin-analyst", ""), ("check", "script", "pass"), ("review", "checker", "pass")])
            for s in spans(item, "model"):  # every model call says which agent made it, and on which model
                self.assertIn(s["who"], ("origin-analyst", "default agent", "checker"), s["id"])
                self.assertEqual((s["model"], s["cost_cell"]["text"], s["cost_cell"]["tip"], s["note"]), ("stand-in", "$0", "free", ""), s["id"])
            self.assertTrue(all(s["who"] for s in spans(item, "tool")))
            execute = first["children"][0]
            self.assertEqual((execute["model"], execute["model_calls"], execute["tool_calls"]), ("scripted stand-in", 4, 5))
            self.assertEqual([title for title, _ in execute["blocks"]], ["prompt · attempts/analyse-1.prompt.md"])  # what the try was told
            self.assertIn("Do the stage 'analyse' for item SEC-1042", execute["blocks"][0][1])
            self.assertEqual(second["facts"], [["told", "the reasons from earlier tries"]])
            tool = next(s for s in execute["children"] if s["name"] == "sbom-query")
            self.assertEqual([title for title, _ in tool["blocks"]], ["arguments", "answer"])
            self.assertIn("cites no file", dict(first["children"][1]["blocks"])["output"])
            script = gather["children"][0]["children"][0]
            self.assertEqual((script["kind"], script["name"], script["who"], dict(script["facts"])["exit code"]), ("script", "scripts/gather.sh", "script", "0"))

            page = c.get(f"/runs/{run_id}/items/SEC-1042/trace").text
            for text in ('title="origin-analyst · stand-in"', 'title="checker · stand-in"', 'title="origin-analyst · scripted stand-in"', 'class="kd k-tool"',
                         "sent back to gather", "prompt · attempts/analyse-1.prompt.md"):
                self.assertIn(text, page)
            # the address selects the span the pane shows
            picked = c.get(f"/runs/{run_id}/items/SEC-1042/trace", params={"span": tool["id"]}).text
            self.assertIn(f'<section class="sp-info" data-for="{tool["id"]}">', picked)
            self.assertIn('<section class="sp-info" data-for="SEC-1042" hidden>', picked)
            self.assertIn('<section class="sp-info" data-for="SEC-1042">', page)

            # the events keep what the trace shows: who, answers of tools and output of scripts
            events = self.store.events(run_id)
            self.assertTrue({"preview", "seconds", "call", "agent"} <= set(next(e for e in events if e["type"] == "tool_result")))
            self.assertIn("fetch_seconds", next(e for e in events if e["type"] == "run_started"))
            self.assertEqual({e["to"] for e in events if e["type"] == "sent_back"}, {"gather"})

    def test_the_trace_while_a_run_is_working(self):
        with TestClient(self.app) as c:
            run_id = self.run_once(c)
            # as it would look part-way: the run has no ending yet and one try is still open
            self.store.db.execute("UPDATE runs SET ended = NULL, beat = ? WHERE id = ?", (time.time(), run_id))
            self.store.db.execute("UPDATE items SET ended_at = NULL, ended_as = NULL WHERE run_id = ? AND item_id = 'SEC-1042'", (run_id,))
            self.store.db.execute("UPDATE attempts SET outcome = NULL, ended = NULL WHERE run_id = ? AND item_id = 'SEC-1042' AND stage = 'report'", (run_id,))
            root = trace.build(self.store, self.store.run(run_id), self.settings.internal, live=True)["root"]
            item = next(s for s in root["children"] if s["name"] == "SEC-1042")
            stages = {s["name"]: s for s in item["children"]}
            self.assertEqual((stages["gather"]["state"], stages["report"]["state"]), ("ok", "run"))
            self.assertLess(stages["gather"]["seconds"], 5)                    # a finished stage keeps its own length
            self.assertLessEqual(stages["gather"]["end"], stages["analyse"]["start"] + 0.5)
            self.assertGreater(stages["report"]["end"], stages["fix"]["end"])  # the open try runs up to now
            self.assertAlmostEqual(stages["report"]["end"], root["end"], delta=1)

            # its pages are fetched again every few seconds, and say what can be done
            page = c.get(f"/runs/{run_id}").text
            for text in (f'data-refresh="{self.settings.internal["refresh_seconds"]}"', " data-live>", ">running<", f'data-post="/api/runs/{run_id}/cancel"', '<span class="run">1 running</span>'):
                self.assertIn(text, page)
            self.assertNotIn(f"/api/runs/{run_id}/resume", page)
            self.assertIn('title="running now">1<', page)  # the sidebar counts it
            flow = {n["id"]: n for n in c.get(f"/api/runs/{run_id}/flow").json()["graph"]["nodes"]}
            self.assertEqual((flow["report"]["state"], flow["report"]["lines"][-2]), ("now", "1 passed · 1 running"))

    def test_files_and_output(self):
        with TestClient(self.app) as c:
            run_id = self.run_once(c)
            base = f"/runs/{run_id}/items/SEC-1042"

            # the Files tab: the folder as a tree, the engine's own files marked, one file shown
            view = c.get(f"/api{base}/files").json()
            tree = {r["name"]: r for r in view["tree"]}
            self.assertEqual([n for n, r in tree.items() if "children" in r], ["attempts", "verdicts"])
            self.assertTrue(tree["attempts"]["engine"] and tree["events.jsonl"]["engine"] and not tree["origin.md"]["engine"])
            self.assertEqual(view["file"]["path"], "base-sbom.json")  # the first file: the engine prefers none
            page = c.get(f"{base}/files", params={"file": "verdicts/analyse-1.md"}).text
            for text in (f'href="{base}/files?file=origin.md"', f'href="{base}/files?file=verdicts/analyse-1.md" aria-current="true"', 'title="written by the engine',
                         "<details data-id=\"dir:verdicts\" open>", "cites no file", f'href="{base}/files/verdicts/analyse-1.md"'):
                self.assertIn(text, page)
            self.assertNotIn('class="files"', page)  # no list of files as tags
            for escape in ("../../../../agentflow.yaml", "/etc/hosts"):
                self.assertNotIn("models:", c.get(f"{base}/files", params={"file": escape}).text)
                self.assertNotIn("localhost", c.get(f"{base}/files", params={"file": escape}).text)

            # files are served as plain text, and only from inside the work folder
            f = c.get(f"{base}/files/origin.md")
            self.assertTrue(f.headers["content-type"].startswith("text/plain"))
            self.assertIn("Origin: app dependency", f.text)
            self.assertIn("fail", c.get(f"{base}/files/verdicts/analyse-1.md").text)
            for url in (f"{base}/files/%2e%2e/%2e%2e/%2e%2e/%2e%2e/agentflow.yaml", f"{base}/files/%2Fetc%2Fhosts", f"{base}/files/nope.txt",
                        "/plugins/flows/vuln-triage/files/%2e%2e/%2e%2e/%2e%2e/agentflow.yaml", "/plugins/flows/%2e%2e/files/agentflow.yaml",
                        "/plugins/nothing/vuln-triage/files/flow.yaml", "/plugins/flows/vuln-triage/files/nope.txt"):
                self.assertIn(c.get(url).status_code, (400, 404), url)
            shown = c.get("/plugins/flows/vuln-triage/files/flow.yaml")
            self.assertTrue(shown.headers["content-type"].startswith("text/plain"))
            self.assertIn("source: security-tickets", shown.text)

            # the run's output folder is served as the flow wrote it, sandboxed, and nothing else is served from there
            index = c.get(f"/runs/{run_id}/output/index.html")
            self.assertEqual((index.status_code, index.headers["content-security-policy"]), (200, "sandbox allow-scripts allow-popups"))
            self.assertTrue(index.headers["content-type"].startswith("text/html"))
            self.assertIn('href="SEC-1042.html"', index.text)
            self.assertIn("Raise acme-http", c.get(f"/runs/{run_id}/output/SEC-1042.html").text)
            for name in ("..%2f..%2fdb%2fagentflow.db", "%2e%2e/items/SEC-1042/fix.md", "nope.html"):
                self.assertIn(c.get(f"/runs/{run_id}/output/{name}").status_code, (400, 404), name)

    def test_a_cost_that_was_not_recorded_is_a_dash(self):
        with TestClient(self.app) as c:
            run_id = self.run_once(c)
            self.store.db.execute("UPDATE attempts SET cost_usd = NULL WHERE run_id = ?", (run_id,))  # as after tries that were cut short
            self.store.db.execute("UPDATE verdicts SET cost_usd = NULL WHERE run_id = ?", (run_id,))
            for url in ["/"] + run_pages(run_id):
                page = c.get(url)
                self.assertEqual(page.status_code, 200, url)
                self.assertNotIn("unknown", page.text.lower(), url)
            run = c.get(f"/api/runs/{run_id}").json()
            self.assertEqual(run["cost"], {"text": "—", "tip": "cost was not recorded for this run"})
            self.assertIn('<span title="cost was not recorded for this run">—</span>', c.get("/").text)

            # part of the cost known: the sum of what was reported, as a plain number; the tooltip says what is missing
            self.store.db.execute("UPDATE attempts SET cost_usd = 0.0031 WHERE run_id = ? AND item_id = 'SEC-1042' AND stage = 'fix'", (run_id,))
            cost = c.get(f"/api/runs/{run_id}").json()["cost"]
            self.assertEqual(cost["text"], "$0.0031")
            self.assertIn("cut short", cost["tip"])


class Changes(Site):

    def test_changes_need_the_header(self):
        with TestClient(self.app) as c:
            run_id = self.run_once(c)
            for url in ("/api/flows/vuln-triage/run", f"/api/runs/{run_id}/resume", f"/api/runs/{run_id}/cancel",
                        "/api/flows/vuln-triage/schedule", "/api/tools/health", "/api/tools/sbom-query/health"):
                self.assertEqual(c.post(url, json={}).status_code, 403, url)  # so a page on another site cannot change anything
            self.assertEqual(len(c.get("/api/runs").json()), 1)
            self.assertEqual(c.post("/api/flows/nope/run", json={}, headers=CHANGE).status_code, 404)
            self.assertEqual(c.post("/api/sources/security-tickets/run", json={}, headers=CHANGE).status_code, 405)  # a source starts nothing: a flow accepts it
            self.assertEqual(len(c.get("/api/runs").json()), 1)
            self.assertEqual(c.post(f"/api/runs/{run_id}/resume", headers=CHANGE).status_code, 409)
            self.assertEqual(c.post(f"/api/runs/{run_id}/cancel", headers=CHANGE).status_code, 409)
            # a request that arrives under another host name is refused
            self.assertEqual(c.get("/", headers={"Host": "evil.example"}).status_code, 400)

    def test_the_schedule_switch_is_the_flows(self):
        with TestClient(self.app) as c:
            off = c.get("/flows/vuln-triage").text
            for text in ("weekdays 07:00", 'role="switch" aria-checked="false"', 'data-post="/api/flows/vuln-triage/schedule"', '{"enabled": true}'):
                self.assertIn(text, off)
            self.assertEqual(c.post("/api/flows/vuln-triage/schedule", json={"enabled": True}, headers=CHANGE).status_code, 200)
            flow = c.get("/api/flows").json()[0]
            self.assertTrue(flow["schedule"]["enabled"] and flow["schedule"]["next_due"])
            on = c.get("/flows/vuln-triage").text
            for text in ('role="switch" aria-checked="true"', '{"enabled": false}', "next "):
                self.assertIn(text, on)
            self.assertIn(">on<", c.get("/flows").text)
            self.assertIn(">on<", c.get("/sources/security-tickets").text)  # the source's page says which flows accept it, and when
            self.assertNotIn('role="switch"', c.get("/sources/security-tickets").text)  # a source has nothing to switch
            self.assertEqual(c.post("/api/flows/nope/schedule", json={"enabled": True}, headers=CHANGE).status_code, 400)

    def test_a_due_schedule_starts_its_flow(self):
        from agentflow.engine.schedule import set_schedule
        from agentflow.web import create_app
        set_schedule(self.settings, self.store, "vuln-triage", True)
        self.store.db.execute("UPDATE schedules SET next_due = '2026-11-13T07:00:00'")
        with TestClient(create_app(self.settings, self.store, hosts=("testserver",))) as c:  # with the server's own loops
            run_id = self.finished_run(c)
            self.assertEqual(c.get(f"/api/runs/{run_id}").json()["by"], "schedule")
            self.assertIn("<small>schedule</small>", c.get("/").text)
            (fetch,) = c.get("/api/sources/security-tickets/fetches").json()["fetches"]
            self.assertEqual((fetch["by"], fetch["count"], fetch["runs"]), ("schedule", 4, [{"id": run_id, "flow": "vuln-triage"}]))
            self.assertTrue(fetch["folder"].endswith("-1-security-tickets"))
            self.assertEqual({t["name"]: t["health"]["state"] for t in c.get("/api/tools").json()}, {"deps-why": "ok", "history": "ok", "history-file": "ok", "sbom-query": "ok"})  # checked when the server started

    def test_tool_health_is_checked_and_shown(self):
        with TestClient(self.app) as c:
            self.assertIn(">not checked<", c.get("/tools").text)
            (self.home / "plugins/tools/deps-why/health.sh").write_text("#!/bin/sh\necho the tree service is down\nexit 1\n")
            rows = {r["tool"]: r for r in c.post("/api/tools/health", headers=CHANGE).json()}
            self.assertEqual({n: r["ok"] for n, r in rows.items()}, {"deps-why": False, "history": True, "history-file": True, "sbom-query": True})
            self.assertIn("finds a known package", rows["sbom-query"]["detail"])
            listed = c.get("/tools").text
            for text in (">ok<", ">failing<", "the tree service is down", "Check all"):
                self.assertIn(text, listed)
            page = c.get("/tools/deps-why").text
            for text in (">failing<", "the tree service is down", 'data-post="/api/tools/deps-why/health"', 'href="/tools/deps-why/files?file=health.sh"'):
                self.assertIn(text, page)
            # the flow whose agents use it says so
            self.assertIn("is failing its health check: the tree service is down", c.get("/flows/vuln-triage").text)
            shutil.copy(ROOT / "plugins/tools/deps-why/health.sh", self.home / "plugins/tools/deps-why/health.sh")
            self.assertTrue(c.post("/api/tools/deps-why/health", headers=CHANGE).json()["ok"])
            self.assertNotIn("is failing its health check", c.get("/flows/vuln-triage").text)
            self.assertEqual(c.post("/api/tools/nope/health", headers=CHANGE).status_code, 404)

    def test_a_failing_source_is_shown(self):
        (self.home / "plugins/sources/security-tickets/fetch.sh").write_text("#!/bin/sh\necho tracker is down >&2\nexit 2\n")
        with TestClient(self.app) as c:
            c.post("/api/flows/vuln-triage/run", json={}, headers=CHANGE)
            for _ in range(50):
                flow = c.get("/api/flows").json()[0]
                if flow["error"]:
                    break
                time.sleep(0.1)
            self.assertIn("tracker is down", flow["error"])  # the flow that was started says why it has no run
            self.assertEqual(len(re.findall(r'<p class="alert bad"><a href="/flows/vuln-triage">.*could not start a run: .*tracker is down', c.get("/").text)), 1)  # one line above the table
            self.assertIn("tracker is down", c.get("/flows/vuln-triage").text)
            self.assertIn("could not start", c.get("/flows").text)
            history = c.get("/sources/security-tickets/fetches").text
            for text in (">failed<", "by hand", "tracker is down"):
                self.assertIn(text, history)


class Design(Site):
    """What is installed: the lists, a flow drawn from its flow.yaml, and the settings."""

    def test_flows_agents_tools_and_sources(self):
        with TestClient(self.app) as c:
            listed = c.get("/api/plugins").json()
            self.assertEqual([(f["name"], f["source"], f["stages"], f["errors"], f["last_run"]) for f in listed["flows"]], [("vuln-triage", "security-tickets", 4, [], None)])
            used = {a["name"]: [(u["stage"], u["as"]) for u in a["used"]] for a in listed["agents"]}
            self.assertEqual(used, {"origin-analyst": [("analyse", "executor")]})  # the engine's own two agents are not plugins
            self.assertEqual({t["name"]: t["used"] for t in listed["tools"]}, {"deps-why": ["origin-analyst"], "history": [], "history-file": [], "sbom-query": ["origin-analyst"]})
            self.assertEqual(c.get("/api/flows").json(), listed["flows"])

            for url, texts in (("/flows", ("vuln-triage", "security-tickets", "weekdays 07:00", ">off<", 'title="this flow has not run"')),
                               ("/agents", ("origin-analyst", "Decides whether")),
                               ("/tools", ("sbom-query", "deps-why", ">script<", "<th>Health</th>")),
                               ("/sources", ("security-tickets", "Open security tickets", "<code>tickets.json</code>", 'title="never fetched"')),
                               ("/agents/origin-analyst", ('href="/tools/deps-why"', "<code>write_file</code>", "vuln-triage · analyse", "executor", "Decides whether")),
                               ("/agents/origin-analyst/method", ("Use deps-why to see", 'href="/agents/origin-analyst/files?file=AGENT.md"')),
                               ("/agents/origin-analyst/files", ("AGENT.md", "tools: [sbom-query, deps-why]")),
                               ("/tools/sbom-query", ("<code>sbom_file</code>", 'href="/tools/sbom-query/files?file=run.py"', 'href="/agents/origin-analyst"', "vuln-triage · analyse")),
                               ("/tools/sbom-query/files?file=run.py", ("#!/usr/bin/env", 'aria-current="true"')),
                               ("/sources/security-tickets", ('href="/flows/vuln-triage"', 'href="/sources/security-tickets/files?file=fetch.sh"',
                                                             "<code>severity</code>", "every item")),
                               ("/sources/security-tickets/fetches", ("Never fetched.",)),
                               ("/sources/security-tickets/files?file=fixtures/tickets.json", ("SEC-1042", '<details data-id="dir:fixtures" open>'))):
                page = c.get(url)
                self.assertEqual(page.status_code, 200, url)
                for text in texts:
                    self.assertIn(text, page.text, url)
            # a list holds what picks a row; who uses it, its arguments and its next slot are on its own page
            for url, gone in (("/flows", (">ok<", "platform default")), ("/agents", ("Used by", "vuln-triage · analyse", 'href="/tools/sbom-query"', ">ok<")),
                              ("/tools", ("Used by", "<code>sbom_file</code>", 'href="/agents/origin-analyst"')), ("/sources", ("weekdays", 'href="/flows/vuln-triage"', ">ok<"))):
                for text in gone:
                    self.assertNotIn(text, c.get(url).text, url)
            for url in ("/flows/nope", "/agents/nope", "/tools/nope", "/sources/nope", "/flows/vuln-triage/nope", "/tools/sbom-query/method", "/api/tools/nope", "/plugins"):
                self.assertEqual(c.get(url).status_code, 404, url)

            # a flow lists its runs
            run_id = self.run_once(c)
            self.assertIn(f'href="/runs/{run_id}"', c.get("/flows/vuln-triage/runs").text)
            self.assertIn(f'href="/runs/{run_id}"', c.get("/flows").text)
            self.assertIn(f'href="/runs/{run_id}"', c.get("/sources/security-tickets/fetches").text)

            # a file that cannot be read is reported on the page; the pages themselves still work
            (self.home / "plugins/tools/sbom-query/tool.yaml").write_text("description: [unclosed\n")
            (self.home / FLOW).write_text("source: security-tickets\nstages:\n  - id: a\n    surprise: 1\n  - nonsense\n")
            for url in ["/"] + run_pages(run_id) + DESIGN_PAGES:
                self.assertEqual(c.get(url).status_code, 200, url)
            self.assertIn("to fix", c.get("/flows").text)
            broken = c.get("/flows/vuln-triage").text
            for text in ("stage a: unknown key", "<th>surprise</th>", '<ul class="alert bad">'):
                self.assertIn(text, broken)
            self.assertIn("to fix", c.get("/tools/sbom-query").text)

    def test_a_flow_is_drawn_from_its_flow_yaml(self):
        with TestClient(self.app) as c:
            flow = c.get("/api/flows/vuln-triage").json()
            g = flow["graph"]
            self.assertEqual([(n["kicker"], n["title"]) for n in g["nodes"]],
                             [("source", "security-tickets"), ("1 · script", "gather"), ("2 · agent", "analyse"), ("3 · default agent", "fix"),
                              ("4 · default agent", "report"), ("end", "output")])
            edges = [(e["src"], e["dst"], e["on"], e["kind"]) for e in g["edges"]]
            self.assertEqual([e for e in edges if e[2] == "pass"], [(":source", "gather", "pass", "next"), ("gather", "analyse", "pass", "next"), ("analyse", "fix", "pass", "next"),
                                                                     ("fix", "report", "pass", "next"), ("report", ":end", "pass", "next")])
            # one column, top to bottom: every box at the same x, each below the one before
            self.assertEqual(len({n["x"] for n in g["nodes"]}), 1)
            self.assertEqual([n["y"] for n in g["nodes"]], sorted(n["y"] for n in g["nodes"]))
            self.assertTrue(all(b["y"] >= a["y"] + a["h"] for a, b in zip(g["nodes"], g["nodes"][1:])))
            self.assertGreater(g["height"], g["width"])
            # a stage with something that can fail tries again itself: a dashed loop, bounded by its tries
            self.assertEqual([e for e in edges if e[2] == "fail"], [(s, s, "fail", "loop") for s in ("analyse", "fix", "report")])
            self.assertEqual({e["label"] for e in g["edges"] if e["on"] == "fail"}, {"fail · up to 5"})
            page = c.get("/flows/vuln-triage").text
            for text in ("<svg", 'class="g-edge fail loop"', 'class="g-edge pass next"', "fail · up to 5", 'id="stage-analyse"', "stroke-dasharray"):
                self.assertIn(text, page + c.get("/static/app.css").text)
            self.assertNotIn("g-edge fail back", page)

            # the stage blocks show every key as written, then what the engine fills in, marked
            analyse = next(s for s in flow["stages"] if s["id"] == "analyse")
            rows = {r["k"]: r for r in analyse["rows"]}
            self.assertEqual(list(rows), ["agent", "check", "done_when", "tools", "model", "review", "review model", "tries", "on_fail", "next"])
            self.assertEqual({k: r["mark"] for k, r in rows.items() if r["mark"]},
                             {"tools": "from agent", "model": "platform default", "review": "default", "review model": "platform default", "tries": "default",
                              "on_fail": "default", "next": "default"})
            self.assertEqual((rows["agent"]["href"], rows["check"]["href"]), ("/agents/origin-analyst", "/flows/vuln-triage/files?file=scripts/chain-cites-files.py"))
            self.assertEqual([link["href"] for link in rows["tools"]["links"]], ["/tools/sbom-query", "/tools/deps-why"])
            self.assertEqual((rows["tries"]["v"], rows["on_fail"]["v"], rows["next"]["v"], rows["model"]["v"]), ("5", "analyse", "fix", "luna"))
            self.assertEqual((analyse["md"], analyse["md_href"]), ("stages/analyse.md", "/flows/vuln-triage/files?file=stages/analyse.md"))
            config = {r["k"]: r for r in flow["config"]}
            self.assertEqual({k: r["mark"] for k, r in config.items()},  # every limit the flow runs under, none of them its own
                             {"source": "", "finish": "", "stand_in": "", "model": "platform default", **{f"limits.{k}": "platform default" for k in self.settings.limits}})
            for text in ('href="/flows/vuln-triage/files?file=stages/fix.md"', "Read origin.md and candidates.json.", 'href="/sources/security-tickets"',
                         '<span class="tag">default</span>', "For each security ticket", 'href="/flows/vuln-triage/files?file=flow.yaml"'):
                self.assertIn(text, page)
            files = c.get("/flows/vuln-triage/files", params={"file": "scripts/gather.sh"}).text
            for text in ('<details data-id="dir:scripts" open>', '<details data-id="dir:fixtures">', 'aria-current="true"', "#!/bin/sh"):
                self.assertIn(text, files)

            # on_fail to an earlier stage is an arrow back; next: end and a flow's own keys show as written
            self.edit_flow("  - id: fix\n", "  - id: fix\n    tries: 3\n    on_fail: analyse\n    model: {name: other-model, provider: litellm}\n")
            self.edit_flow("  - id: gather\n", "  - id: gather\n    next: fix\n")
            self.edit_flow("source: security-tickets\n", "source: security-tickets\nlimits:\n  at_once: 1\n")
            flow = c.get("/api/flows/vuln-triage").json()
            g = flow["graph"]
            edges = {(e["src"], e["dst"], e["on"]): e for e in g["edges"]}
            self.assertEqual((edges["fix", "analyse", "fail"]["kind"], edges["fix", "analyse", "fail"]["label"]), ("back", "fail · up to 3"))
            self.assertEqual(edges["gather", "fix", "pass"]["kind"], "skip")
            self.assertNotIn(("fix", "fix", "fail"), edges)
            self.assertTrue(all(n["x"] >= 0 and n["y"] >= 0 and n["x"] + n["w"] <= g["width"] and n["y"] + n["h"] <= g["height"] for n in g["nodes"]))
            self.assertTrue(all(0 <= e["lx"] <= g["width"] and e["ly"] <= g["height"] for e in g["edges"] if e["label"]))
            page = c.get("/flows/vuln-triage").text
            for text in ('class="g-edge fail back"', "fail · up to 3", 'class="g-edge pass skip"', "nothing leads to it"):
                self.assertIn(text, page)
            rows = {r["k"]: r for r in next(s for s in flow["stages"] if s["id"] == "fix")["rows"]}
            self.assertEqual((rows["tries"], rows["on_fail"]["href"], rows["model"]["v"], rows["model"]["mark"]),
                             ({"k": "tries", "v": "3", "href": "", "note": "", "mark": "", "links": []}, "#stage-analyse", "other-model", ""))
            self.assertEqual(next(r for r in flow["config"] if r["k"] == "limits.at_once"), {"k": "limits.at_once", "v": "1", "href": "", "note": "", "mark": "", "links": []})

    def test_an_mcp_tool_and_a_malformed_block_are_shown(self):
        tool = self.home / "plugins/tools/notes"
        tool.mkdir()
        (tool / "tool.yaml").write_text("description: Notes kept beside the item.\nmcp: [python3, ./server.py]\n")
        (self.home / FLOW).write_text((self.home / FLOW).read_text() + "limits: 7\n")
        with TestClient(self.app) as c:
            page = c.get("/tools/notes").text  # an MCP server shows its command, not empty script fields
            self.assertIn("python3 ./server.py", page)
            self.assertNotIn("<th>args</th>", page)
            self.assertIn(">MCP server<", c.get("/tools").text)
            flow = c.get("/api/flows/vuln-triage").json()  # a malformed block is a check error on the page, not a crash
            self.assertTrue(any(e.startswith("limits: must be a mapping of at_once, context_tokens, ") for e in flow["errors"]), flow["errors"])

    def test_the_settings_show_names_and_never_a_key(self):
        os.environ["OPENAI_API_KEY"] = "sk-not-for-any-page"
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY", None)
        settings = Settings(self.home)
        settings.stand_in = True
        with TestClient(make_app(settings, self.store)) as c:
            view = c.get("/api/settings").json()
            self.assertEqual(view["models"], [{"name": "luna", "default": True, "runs_on": "engine", "provider": "openai", "model": "gpt-6-luna", "api": "responses", "reasoning": "low",
                                               "key_env": "OPENAI_API_KEY", "key_set": True},
                                              {"name": "luna-codex", "default": False, "runs_on": "codex CLI", "provider": "", "model": "gpt-6-luna", "api": "", "reasoning": "",
                                               "key_env": "OPENAI_API_KEY", "key_set": True},  # a CLI has its own provider and interface
                                              {"name": "sonnet-cc", "default": False, "runs_on": "claude CLI", "provider": "", "model": "sonnet", "api": "", "reasoning": "",
                                               "key_env": "", "key_set": False}])           # and, with no key named, its own sign-in
            self.assertEqual(view["errors"], [])
            limits = {r["k"]: (r["v"], r["mark"]) for r in view["limits"]}  # the limits of a run, and whether agentflow.yaml sets each
            self.assertEqual(list(limits), list(DEFAULTS["limits"]))
            self.assertEqual(limits["script_seconds"], ("120", ""))
            page = c.get("/settings").text
            for text in ("<code>OPENAI_API_KEY</code>", ">set<", "gpt-6-luna", ">default<", "cost_per_run_usd", "context_tokens"):
                self.assertIn(text, page)
            for text in ("heartbeat_seconds", "line_chars", "fact_chars"):  # the engine's own mechanics are not settings
                self.assertNotIn(text, page)
            for url in ["/settings", "/sources/security-tickets", "/api/settings", "/api/plugins", "/flows/vuln-triage", "/api/sources/security-tickets"]:
                self.assertNotIn("sk-not-for-any-page", c.get(url).text, url)


if __name__ == "__main__":
    unittest.main()
