"""Engine tests. They use the scripted stand-in model: no key, no network, no cost.

    .venv/bin/python -m unittest discover -s tests -v
"""
import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from agentflow.workers.agents import CLEARED, ModelNotReady, ToolErrors, build_model, file_tools, keep_in_context, run_agent
from agentflow.cli import main
from agentflow.plugins.graph import compile_flow
from agentflow.records.history import earlier, earlier_file
from agentflow.plugins.health import tool_health
from agentflow.records.layout import confined
from agentflow.plugins.registry import Registry, installed
from agentflow.workers.procs import DONE_EARLY, STOPPED, TIMED_OUT, run_proc
from agentflow.engine.runner import FetchFailed, run_due, start_flow, try_flow
from agentflow.engine.schedule import next_due, parse_schedule, set_schedule, tick
from agentflow.settings import DEFAULTS, Settings
from agentflow.engine.stages import Ctx, decide
from agentflow.web import show
from support import ROOT, Home


class Pure(unittest.TestCase):
    def test_decide(self):
        ok, bad = {"passed": True}, {"passed": False}
        for args, want in [((3, 1, [ok, ok]), "pass"), ((3, 1, []), "pass"), ((3, 1, [bad]), "again"), ((3, 2, [ok, bad]), "again"),
                           ((3, 3, [bad]), "person"), ((2, 2, [ok, bad]), "person"),
                           ((3, 1, [{"passed": False, "needs_person": True}]), "person"), ((3, 1, [{"passed": False, "error": True}]), "person")]:
            self.assertEqual(decide(*args), want, args)

    def test_confined(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            (work / "link").symlink_to("/etc")
            for path, writing in (("../x", False), ("/etc/hosts", False), ("events.jsonl", True), ("verdicts/a.md", True), ("link/hosts", False)):
                with self.assertRaises(ValueError, msg=path):
                    confined(work, path, writing)
            self.assertEqual(confined(work, "notes/a.md", True), work / "notes/a.md")

    def test_a_flow_compiles_to_a_graph(self):
        stages = [{"id": "a", "run": "x"}, {"id": "b", "done_when": "ok"}, {"id": "c", "check": "y", "on_fail": "b", "tries": 3}]
        graph, errs = compile_flow({"stages": stages}, DEFAULTS["limits"])
        self.assertEqual(errs, [])
        self.assertEqual([(e.src, e.dst, e.on) for e in graph.edges],
                         [("a", "b", "pass"), ("b", "c", "pass"), ("b", "b", "fail"), ("c", "end", "pass"), ("c", "b", "fail")])
        self.assertEqual((graph.start, graph.after("a"), graph.after("c"), graph.after("c", "fail"), graph.senders("b")), ("a", "b", "end", "b", {"c"}))
        # where an item stands, from its latest try
        self.assertEqual(graph.position(None), "a")
        self.assertEqual(graph.position({"stage": "b", "outcome": "passed"}), "c")
        self.assertEqual(graph.position({"stage": "c", "outcome": "rejected"}, rejected=1), "b")   # sent back
        self.assertEqual(graph.position({"stage": "c", "outcome": "rejected"}, rejected=3), "c")   # out of tries
        self.assertEqual(graph.position({"stage": "c", "outcome": "interrupted"}), "c")
        for change, wanted in (({"id": "c", "next": "a"}, "A loop must use on_fail"), ({"id": "c", "next": "nope"}, "not a stage"),
                               ({"id": "a", "run": "x", "on_fail": "a"}, "needs something that can fail"), ({"id": "b", "done_when": "ok", "on_fail": "c"}, "comes later"),
                               ({"id": "a", "run": "x", "next": "c"}, "stage b: nothing leads to it"), ({"id": "b", "done_when": "ok", "tries": 0}, "1 or more"),
                               ({"id": "a"}, "the id is used twice")):
            changed = [change if s["id"] == change["id"] else s for s in stages] + ([change] if change == {"id": "a"} else [])
            self.assertTrue(any(wanted in e for e in compile_flow({"stages": changed}, DEFAULTS["limits"])[1]), (wanted, compile_flow({"stages": changed}, DEFAULTS["limits"])[1]))
        self.assertEqual(compile_flow({"stages": []}, DEFAULTS["limits"])[1], ["the flow has no stages"])

    def test_schedule(self):
        self.assertEqual(parse_schedule("weekdays 07:00"), ("weekdays", 7, 0))
        self.assertEqual(parse_schedule("every 10 minutes"), ("every", 10))
        with self.assertRaises(ValueError):
            parse_schedule("when it rains")
        friday_8 = datetime(2026, 11, 13, 8, 0)
        self.assertEqual(next_due("weekdays 07:00", friday_8), datetime(2026, 11, 16, 7, 0))  # skips the weekend
        self.assertEqual(next_due("daily 07:00", friday_8), datetime(2026, 11, 14, 7, 0))
        self.assertEqual(next_due("daily 09:00", friday_8), datetime(2026, 11, 13, 9, 0))

    def test_an_agent_is_told_of_a_failed_tool_and_old_results_are_cleared(self):
        from pydantic_ai import Agent
        from pydantic_ai.usage import RequestUsage

        # a tool that breaks does not end the run: its error is the tool's answer, and the agent goes on
        def broken() -> str:
            raise ConnectionError("no route to the registry")

        seen = []

        def model(messages, info):
            results = [p.content for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
            seen.extend(results)
            return ModelResponse(parts=[TextPart("went on without it")] if results else [ToolCallPart("broken", {})])

        done = asyncio.run(Agent(FunctionModel(model), tools=[broken], capabilities=[ToolErrors()]).run("go"))
        self.assertEqual(done.output, "went on without it")
        self.assertIn("error: the tool broken failed: ConnectionError: no route to the registry", seen[0])

        # once a request has read more than the limit, all but the latest results are cleared; the calls stay
        def history(read):
            turns = []
            for i in range(4):
                turns += [ModelResponse(parts=[ToolCallPart("read_file", {"path": f"{i}.md"}, tool_call_id=str(i))], usage=RequestUsage(input_tokens=read)),
                          ModelRequest(parts=[ToolReturnPart("read_file", f"text {i}", tool_call_id=str(i))])]
            return turns

        def results(messages):
            return [p.content for m in messages if isinstance(m, ModelRequest) for p in m.parts]

        self.assertEqual(results(keep_in_context(100, 2)(history(50))), ["text 0", "text 1", "text 2", "text 3"])
        cleared = keep_in_context(100, 2)(history(500))
        self.assertEqual(results(cleared), [CLEARED, CLEARED, "text 2", "text 3"])
        self.assertEqual(sum(isinstance(m, ModelResponse) for m in cleared), 4)

    def test_a_plugin_markdown_file_is_shown_inert(self):
        out = show.md_to_html("# T\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1)) [y](https://example.com)")
        self.assertNotIn("<script>", out)
        self.assertNotIn("javascript:", out)
        self.assertIn('href="https://example.com"', out)


class EndToEnd(Home):
    def test_plugins_check(self):
        for kind, name, errs, _ in installed(self.settings).check_all():
            self.assertEqual(errs, [], f"{kind}/{name}")
        flow = self.settings.plugins / "flows/vuln-triage/flow.yaml"
        flow.write_text(flow.read_text() + "retries: 3\n")
        self.assertIn("unknown key 'retries'", installed(self.settings).check_flow("vuln-triage"))

    def test_a_full_run_and_then_another(self):
        run_id = asyncio.run(start_flow(self.settings, self.store, "vuln-triage"))  # a person starts a flow; its source is fetched for it
        items = self.items(run_id)
        self.assertEqual({k: v["ended_as"] for k, v in items.items()},
                         {"SEC-1042": "done", "SEC-1043": "done", "SEC-1044": "needs_person", "SEC-1045": "needs_person"})
        work = self.store.path(items["SEC-1042"])

        # the feedback loop: analyse failed its check once, the reason reached the second try, and both rounds are kept
        tries = self.store.q("SELECT n, outcome FROM attempts WHERE run_id = ? AND item_id = 'SEC-1042' AND stage = 'analyse' ORDER BY n", run_id)
        self.assertEqual([(t["n"], t["outcome"]) for t in tries], [(1, "rejected"), (2, "passed")])
        self.assertIn("cites no file", (work / "attempts/analyse-2.prompt.md").read_text())
        self.assertIn("fail", (work / "verdicts/analyse-1.md").read_text())
        self.assertIn("pass", (work / "verdicts/analyse-2.md").read_text())
        self.assertIn("4.3.1", (work / "fix.md").read_text())
        self.assertIn("3.12-slim-20261110", (self.store.path(items["SEC-1043"]) / "fix.md").read_text())

        # who asked for a person, and at which stage
        self.assertIn("neither", items["SEC-1045"]["reason"])
        self.assertFalse((self.store.path(items["SEC-1045"]) / "origin.md").exists())
        self.assertIn("no release or tag", items["SEC-1044"]["reason"])

        # events: numbered in order, mirrored into the item folder, tool calls recorded as they happened
        lines = [json.loads(l) for l in (work / "events.jsonl").read_text().splitlines()]
        self.assertEqual([l["seq"] for l in lines], sorted(l["seq"] for l in lines))
        self.assertIn("deps-why", [l.get("tool") for l in lines if l["type"] == "tool_call"])
        self.assertEqual(lines[-1]["type"], "item_ended")

        # the stand-in costs nothing, and that is known. A try that was cut short would make the cost unknown
        usage = self.store.usage(run_id)
        self.assertGreater(usage["tokens"], 0)
        self.assertEqual((usage["cost_known"], usage["cost_usd"]), (True, 0))

        # what a run puts out is the flow's own: its finish script ran once, in the run's output folder, and wrote the report there
        run = self.store.one("SELECT * FROM runs WHERE id = ?", run_id)
        out = self.store.path(run) / "output"
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["SEC-1042.html", "SEC-1043.html", "SEC-1044.html", "SEC-1045.html", "index.html", "run.log"])
        self.assertIn("4 tickets: 2 triaged, 2 needs a person", (out / "index.html").read_text())
        self.assertIn("Raise acme-http", (out / "SEC-1042.html").read_text())
        ended = [e for e in self.store.events(run_id) if e["item"] is None]
        self.assertEqual([e["type"] for e in ended], ["run_started", "finish_ended", "run_ended"])
        self.assertEqual((ended[1]["script"], ended[1]["exit_code"]), ("scripts/report.py", 0))
        self.assertEqual((run["source"], self.store.one("SELECT COUNT(*) n FROM fetches WHERE id = ? AND outcome = 'ok'", run["fetch_id"])["n"]), ("security-tickets", 1))
        # everything the run wrote is in its own folder; the data folder holds only the database, the fetches and the runs
        self.assertEqual(sorted(p.name for p in self.store.path(run).iterdir()), ["items", "output", "plugins"])
        self.assertEqual(sorted(p.name for p in (self.store.path(run) / "items").iterdir()), ["SEC-1042", "SEC-1043", "SEC-1044", "SEC-1045"])
        self.assertEqual(sorted(p.name for p in self.settings.data.iterdir()), ["db", "fetches", "runs"])
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["agentflow.yaml", "data", "plugins"])
        self.assertFalse(Path(run["folder"]).is_absolute())  # stored relative, so the data folder can be moved

        # a run is just a run. The next one, here from the flow's schedule, works every item the flow accepts again,
        # in its own folder; leaving an item out is for the flow to do (what its source hands over, its `where`)
        (second,) = asyncio.run(run_due(self.settings, self.store, "security-tickets", ["vuln-triage"], "2026-11-16T07:00:00"))
        again = self.items(second)
        self.assertEqual({k: v["ended_as"] for k, v in again.items()}, {k: v["ended_as"] for k, v in items.items()})
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM attempts WHERE run_id = ? AND item_id = 'SEC-1042' AND stage = 'analyse'", second)["n"], 2)
        self.assertNotEqual(self.store.path(again["SEC-1042"]), work)
        self.assertIn("2 triaged, 2 needs a person", (self.store.path(self.store.one("SELECT folder FROM runs WHERE id = ?", second)) / "output/index.html").read_text())

        # the engine does not look back, but a flow can: `agentflow history` is what its scripts call, and the asking run is left out
        rows = earlier(self.store, "vuln-triage", "SEC-1044", self.settings.internal["rows"], not_run=second)
        self.assertEqual([(r["run"], r["ended_as"], r["trial"]) for r in rows], [(run_id, "needs_person", False)])
        self.assertIn("no release or tag", rows[0]["reason"])
        self.assertEqual((rows[0]["folder"], rows[0]["output"]), (str(self.store.path(items["SEC-1044"]).resolve()), str(out.resolve())))
        self.assertIn("origin.md", rows[0]["files"])
        self.assertEqual(len(earlier(self.store, "vuln-triage", None, self.settings.internal["rows"])), 8)
        self.assertIn("4.3.1", earlier_file(self.store, run_id, "SEC-1042", "fix.md", self.settings.internal["read_chars"]))
        with self.assertRaisesRegex(ValueError, "inside the work folder"):
            earlier_file(self.store, run_id, "SEC-1042", "../SEC-1043/fix.md", 100)
        # a script of the flow reaches the command by name, and is told its flow and its run
        script = self.home / "ask.sh"
        script.write_text('#!/bin/sh\nagentflow history --item SEC-1042\n')
        script.chmod(0o755)
        told = {"AGENTFLOW_HOME": str(self.home), "AGENTFLOW_FLOW": "vuln-triage", "AGENTFLOW_RUN": second}
        code, said = asyncio.run(run_proc([str(script)], self.home, env=told, **self.settings.proc()))
        self.assertEqual((code, [r["run"] for r in json.loads(said)]), (0, [run_id]))
        # a flow that does not pass its checks is not started
        (self.home / "plugins/flows/vuln-triage/flow.yaml").write_text("source: security-tickets\nstages: []\nsurprise: 1\n")
        with self.assertRaisesRegex(ValueError, "unknown key 'surprise'"):
            asyncio.run(start_flow(self.settings, self.store, "vuln-triage"))

    def test_a_source_hands_over_what_it_says_and_a_flow_takes_what_it_accepts(self):
        source, flows = self.home / "plugins/sources/security-tickets", self.home / "plugins/flows"
        flow = flows / "vuln-triage/flow.yaml"
        written = flow.read_text()
        # two flows accept the same source and come due in the same slot: one fetch, kept in its own folder, and one run each
        shutil.copytree(flows / "vuln-triage", flows / "high-only")
        (flows / "high-only/flow.yaml").write_text(written.replace("source: security-tickets\n", "source: security-tickets\nwhere: {severity: high}\n"))
        self.assertEqual(installed(self.settings).check_flow("high-only"), [])
        all_of_them, high = asyncio.run(run_due(self.settings, self.store, "security-tickets", ["vuln-triage", "high-only"], "2026-11-16T07:00:00"))
        (fetch,) = self.store.q("SELECT * FROM fetches")
        self.assertEqual([i["id"] for i in json.loads((self.settings.data / fetch["folder"] / "tickets.json").read_text())], ["SEC-1042", "SEC-1043", "SEC-1044", "SEC-1045"])
        self.assertEqual({r["id"]: r["fetch_id"] for r in self.store.q("SELECT * FROM runs")}, {all_of_them: fetch["id"], high: fetch["id"]})
        self.assertEqual(sorted(self.items(all_of_them)), ["SEC-1042", "SEC-1043", "SEC-1044", "SEC-1045"])
        self.assertEqual(sorted(self.items(high)), ["SEC-1042", "SEC-1044"])  # the engine routes: this flow asked for high only
        shutil.rmtree(flows / "high-only")

        # a flow cannot ask about a field its source does not list
        flow.write_text(written.replace("source: security-tickets\n", "source: security-tickets\nwhere: {team: payments}\n"))
        with self.assertRaisesRegex(ValueError, "lists no field 'team'"):
            asyncio.run(start_flow(self.settings, self.store, "vuln-triage"))
        flow.write_text(written)
        # a source that breaks its word is a failed fetch, and no flow gets half an item
        said = (source / "source.yaml").read_text()
        (source / "source.yaml").write_text(said.replace("severity, summary]", "severity, summary, owner]"))
        with self.assertRaisesRegex(FetchFailed, "lists the field 'owner', but 4 items do not have it"):
            asyncio.run(start_flow(self.settings, self.store, "vuln-triage"))
        (source / "source.yaml").write_text(said)
        (source / "fetch.sh").write_text("#!/bin/sh\ncat fixtures/tickets.json\n")  # prints, but writes nothing where it said it would
        with self.assertRaisesRegex(FetchFailed, "did not write tickets.json"):
            asyncio.run(start_flow(self.settings, self.store, "vuln-triage"))
        self.assertEqual([f["outcome"] for f in self.store.q("SELECT * FROM fetches ORDER BY id")], ["ok", "failed", "failed"])
        self.assertEqual(installed(self.settings).check_source("security-tickets"), [])
        (source / "source.yaml").write_text("run: ./fetch.sh\nsurprise: 1\n")
        self.assertEqual(installed(self.settings).check_source("security-tickets"), ["unknown key 'surprise'", "missing 'description'", "missing 'output'"])

    def test_a_trial_is_one_item_in_its_own_folder(self):
        run_id = asyncio.run(try_flow(self.settings, self.store, "vuln-triage", "SEC-1043"))
        self.assertEqual({k: v["ended_as"] for k, v in self.items(run_id).items()}, {"SEC-1043": "done"})
        self.assertRegex(self.store.one("SELECT folder FROM runs WHERE id = ?", run_id)["folder"], r"^runs/\d{4}-\d\d-\d\d-t_")

    def test_plugin_edit_mid_run_uses_snapshot(self):
        live = installed(self.settings)
        digest = live.snapshot("vuln-triage", self.home / "copy-1")
        (self.settings.plugins / "flows/vuln-triage/FLOW.md").write_text("# changed\n")
        self.assertIn("Vulnerability triage", Registry(self.home / "copy-1", self.settings).flow_goal("vuln-triage"))
        self.assertNotEqual(digest, live.snapshot("vuln-triage", self.home / "copy-2"))

    def test_cost_cap_stops_the_run(self):
        self.settings.limits["cost_per_run_usd"] = 0.0
        run_id = asyncio.run(start_flow(self.settings, self.store, "vuln-triage"))
        self.assertEqual({v["ended_as"] for v in self.items(run_id).values()}, {"over_budget"})

    def test_every_limit_is_a_setting(self):
        """The packaged defaults, then agentflow.yaml, then a flow's own `limits`. The code holds no number of its own."""
        self.assertEqual(self.settings.check(), [])
        self.assertEqual((self.settings.limits, self.settings.internal), (DEFAULTS["limits"], DEFAULTS["internal"]))
        flow_dir = self.settings.plugins / "flows/vuln-triage"
        flow = flow_dir / "flow.yaml"
        # the platform's limit on model calls: an agent that needs a second call is stopped. That is a try that was not
        # accepted, so the next try is told why; out of tries, the item is set aside for a person, with what the tries used
        self.settings.limits["model_calls"] = 1
        run_id = asyncio.run(try_flow(self.settings, self.store, "vuln-triage", "SEC-1042"))
        item = self.items(run_id)["SEC-1042"]
        self.assertEqual(item["ended_as"], "needs_person")
        self.assertIn("analyse: not accepted after 2 of 2 tries. Last reason from engine: the try stopped without an answer: the run used all of its 1 model requests", item["reason"])
        tries = self.store.q("SELECT n, outcome, model_calls, cost_usd FROM attempts WHERE run_id = ? AND stage = 'analyse' ORDER BY n", run_id)
        self.assertEqual([(t["n"], t["outcome"], t["model_calls"], t["cost_usd"]) for t in tries], [(1, "rejected", 1, 0), (2, "rejected", 1, 0)])
        self.assertIn("- attempt 1, engine: the try stopped without an answer", (self.store.path(item) / "attempts/analyse-2.prompt.md").read_text())
        self.assertTrue(self.store.usage(run_id)["cost_known"])
        self.settings.limits["model_calls"] = DEFAULTS["limits"]["model_calls"]
        # how much of a file an agent is given
        (self.home / "long.txt").write_text("0123456789")
        read = next(t for t in file_tools(self.home, False, mock.Mock(internal={"read_chars": 4})) if t.name == "read_file")
        self.assertEqual(read.function("long.txt"), "0123")
        # the tries of a stage that names none, and a stage's own
        self.settings.limits["tries"] = 4
        graph = installed(self.settings).graph("vuln-triage")
        self.assertEqual((graph.tries("analyse"), graph.default_tries), (4, 4))
        flow.write_text(flow.read_text().replace("  - id: fix\n", "  - id: fix\n    tries: 0\n"))
        self.assertIn("stage fix: 'tries' must be a number, 1 or more", installed(self.settings).check_flow("vuln-triage"))
        flow.write_text(flow.read_text().replace("    tries: 0\n", "    tries: 1\n"))
        self.assertEqual(installed(self.settings).graph("vuln-triage").tries("fix"), 1)
        # a flow's own limit wins: its scripts get one second
        (flow_dir / "scripts/gather.sh").write_text("#!/bin/sh\nsleep 30\n")
        flow.write_text(flow.read_text() + "limits:\n  script_seconds: 1\n")
        self.assertEqual(installed(self.settings).check_flow("vuln-triage"), [])
        self.assertEqual(self.settings.limits_for(installed(self.settings).flow("vuln-triage"))["script_seconds"], 1)
        run_id = asyncio.run(try_flow(self.settings, self.store, "vuln-triage", "SEC-1042"))
        self.assertIn("timed out after 1s", self.items(run_id)["SEC-1042"]["reason"])
        # a wrong key or value is a check error, in a flow and in agentflow.yaml
        flow.write_text(flow.read_text() + "  surprise: 1\n  model_calls: none\n")
        errs = installed(self.settings).check_flow("vuln-triage")
        self.assertTrue({"limits: unknown key 'surprise'", "limits: model_calls must be a number above 0"} <= set(errs), errs)
        yaml = self.home / "agentflow.yaml"
        yaml.write_text(yaml.read_text().replace("  tries: 2 ", "  tries: many\n  heartbeat_seconds: 90\n  model_calls: 7 "))
        changed = Settings(self.home)
        self.assertEqual(changed.check(), ["limits: unknown key 'heartbeat_seconds'", "limits: tries must be a number above 0"])  # the engine's mechanics are not settings
        self.assertEqual((changed.limits["model_calls"], changed.internal["heartbeat_seconds"]), (7, DEFAULTS["internal"]["heartbeat_seconds"]))

    def test_reviewer_agent_and_early_end(self):
        flow_dir = self.settings.plugins / "flows/vuln-triage"
        flow = flow_dir / "flow.yaml"
        flow.write_text(flow.read_text().replace("  - id: fix\n", "  - id: fix\n    review: origin-analyst\n"))
        self.assertEqual(installed(self.settings).check_flow("vuln-triage"), [])
        run_id = asyncio.run(try_flow(self.settings, self.store, "vuln-triage", "SEC-1042"))
        self.assertEqual(self.items(run_id)["SEC-1042"]["ended_as"], "done")
        by = [v["by"] for v in self.store.q("SELECT by FROM verdicts WHERE run_id = ? AND stage = 'fix'", run_id)]
        self.assertEqual(by, ["origin-analyst"])  # the named reviewer judged the stage, not the built-in checker
        # a reviewer may not be the agent that did the stage
        flow.write_text(flow.read_text().replace("    check: scripts/chain-cites-files.py\n", "    check: scripts/chain-cites-files.py\n    review: origin-analyst\n"))
        self.assertTrue(any("reviewer cannot be" in e for e in installed(self.settings).check_flow("vuln-triage")))
        flow.write_text(flow.read_text().replace("    check: scripts/chain-cites-files.py\n    review: origin-analyst\n", "    check: scripts/chain-cites-files.py\n"))
        # a stage script can end an item early with exit code 78; a failing script fails the item
        (flow_dir / "scripts/gather.sh").write_text("#!/bin/sh\necho not a security ticket\nexit 78\n")
        run_id = asyncio.run(try_flow(self.settings, self.store, "vuln-triage", "SEC-1042"))
        item = self.items(run_id)["SEC-1042"]
        self.assertEqual((item["ended_as"], item["reason"]), ("done", "ended early at gather: not a security ticket"))
        (flow_dir / "scripts/gather.sh").write_text("#!/bin/sh\necho tracker is down >&2\nexit 3\n")
        run_id = asyncio.run(try_flow(self.settings, self.store, "vuln-triage", "SEC-1042"))
        item = self.items(run_id)["SEC-1042"]
        self.assertEqual(item["ended_as"], "failed")
        self.assertIn("tracker is down", item["reason"])

    def test_a_rejected_stage_can_send_the_item_back_to_an_earlier_one(self):
        flow_dir = self.settings.plugins / "flows/vuln-triage"
        again = flow_dir / "scripts/analysed-again.sh"  # accepts the fix only once analyse has run a third time
        again.write_text('#!/bin/sh\nn=$(ls attempts | grep -c "^analyse-.*prompt")\n[ "$n" -ge 3 ] || { echo "analyse has only run $n times"; exit 1; }\n')
        again.chmod(0o755)
        flow = flow_dir / "flow.yaml"
        flow.write_text(flow.read_text().replace("  - id: fix\n", "  - id: fix\n    check: scripts/analysed-again.sh\n    on_fail: analyse\n"))
        self.assertEqual(installed(self.settings).check_flow("vuln-triage"), [])
        run_id = asyncio.run(try_flow(self.settings, self.store, "vuln-triage", "SEC-1042"))
        item = self.items(run_id)["SEC-1042"]
        self.assertEqual(item["ended_as"], "done", item["reason"])
        tries = {s: [(a["n"], a["outcome"]) for a in self.store.q("SELECT n, outcome FROM attempts WHERE run_id = ? AND stage = ? ORDER BY n", run_id, s)]
                 for s in ("gather", "analyse", "fix", "report")}
        self.assertEqual(tries, {"gather": [(1, "passed")], "analyse": [(1, "rejected"), (2, "passed"), (3, "passed")],
                                 "fix": [(1, "rejected"), (2, "passed")], "report": [(1, "passed")]})
        back = [e for e in self.store.events(run_id) if e["type"] == "sent_back"]
        self.assertEqual([(e["stage"], e["to"]) for e in back], [("fix", "analyse")])
        told = (self.store.path(item) / "attempts/analyse-3.prompt.md").read_text()
        self.assertIn("the later stage 'fix' was not accepted", told)
        self.assertIn("analyse has only run 2 times", told)
        # every agent event says who acted and on which model
        calls = [e for e in self.store.events(run_id) if e["type"] in ("model_call", "tool_call")]
        self.assertEqual({e["agent"] for e in calls}, {"origin-analyst", "default agent", "checker"})
        self.assertEqual({e["model"] for e in calls if e["type"] == "model_call"}, {"stand-in"})
        self.assertEqual(self.store.usage(run_id)["cost_known"], True)  # the stand-in costs nothing, and that is known

    def test_a_flow_a_stage_and_an_agent_pick_their_model(self):
        self.settings.models["small"] = {"provider": "openai", "name": "some-small-model", "api_key_env": "KEY_FOR_SMALL"}
        flow = self.settings.plugins / "flows/vuln-triage/flow.yaml"
        agent = self.settings.plugins / "agents/origin-analyst/AGENT.md"
        flow.write_text(flow.read_text().replace("source: security-tickets\n", "source: security-tickets\nmodel: small\n").replace(
            "  - id: report\n", "  - id: report\n    model: {provider: litellm, name: via-proxy, api_key_env: KEY_FOR_PROXY, api: chat}\n"))
        live = installed(self.settings)
        self.assertEqual(live.check_flow("vuln-triage"), [])
        stages = live.graph("vuln-triage").stages
        self.assertEqual(live.model_for("vuln-triage", stages["analyse"], "origin-analyst"), "small")           # the flow's
        self.assertEqual(live.model_for("vuln-triage", stages["report"])["name"], "via-proxy")                  # the stage's own
        agent.write_text(agent.read_text().replace("tools: [sbom-query, deps-why]\n", "tools: [sbom-query, deps-why]\nmodel: luna\n"))
        self.assertEqual(live.model_for("vuln-triage", stages["analyse"], "origin-analyst"), "luna")            # the agent's own wins over the flow's
        self.assertEqual(self.settings.model_label("small"), "scripted stand-in")
        self.settings.stand_in = False
        self.assertEqual((self.settings.model_label("small"), self.settings.model_label()), ("openai · some-small-model", "openai · gpt-6-luna"))
        with self.assertRaisesRegex(ModelNotReady, "KEY_FOR_SMALL is not set"):   # each model reads its own key
            build_model(self.settings, "small")
        with self.assertRaisesRegex(ModelNotReady, "KEY_FOR_PROXY is not set"):
            build_model(self.settings, stages["report"]["model"])
        with self.assertRaisesRegex(ModelNotReady, "is not set"):                  # a run stops before any work
            asyncio.run(try_flow(self.settings, self.store, "vuln-triage", "SEC-1042"))
        flow.write_text(flow.read_text().replace("model: small\n", "model: nope\n"))
        self.assertIn("the flow: no model named 'nope' in agentflow.yaml", installed(self.settings).check_flow("vuln-triage"))

    def test_show_lists_runs_shows_one_and_follows_it(self):
        run_id = asyncio.run(try_flow(self.settings, self.store, "vuln-triage", "SEC-1042"))
        os.environ["AGENTFLOW_HOME"] = str(self.home)
        try:
            for argv, wanted in ((["show"], f"{run_id}  vuln-triage"), (["show", run_id], "SEC-1042  done"),
                                 (["show", run_id, "SEC-1042", "--follow"], "SEC-1042  ended: done")):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(argv), 0, argv)
                self.assertIn(wanted, out.getvalue(), argv)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["show", "r_none"]), 2)
            self.assertIn("no run 'r_none'", out.getvalue())
        finally:
            del os.environ["AGENTFLOW_HOME"]

    def test_schedule_tick(self):
        self.assertEqual(tick(self.settings, self.store, datetime(2026, 11, 13, 6, 0)), [])  # registered, switched off
        set_schedule(self.settings, self.store, "vuln-triage", True)
        self.store.db.execute("UPDATE schedules SET next_due = '2026-11-13T07:00:00'")
        self.assertEqual(tick(self.settings, self.store, datetime(2026, 11, 13, 6, 59)), [])
        late = datetime(2026, 11, 17, 9, 30)  # days late: one catch-up run, not one per missed day
        self.assertEqual(tick(self.settings, self.store, late), [("vuln-triage", "security-tickets", "2026-11-13T07:00:00")])
        self.assertEqual(tick(self.settings, self.store, late), [])
        self.assertEqual(self.store.one("SELECT next_due FROM schedules")["next_due"], "2026-11-18T07:00:00")
        # the same slot is never fetched twice
        self.assertEqual(len(asyncio.run(run_due(self.settings, self.store, "security-tickets", ["vuln-triage"], "2026-11-13T07:00:00"))), 1)
        self.assertEqual(asyncio.run(run_due(self.settings, self.store, "security-tickets", ["vuln-triage"], "2026-11-13T07:00:00")), [])



class Scripts(Home):
    """What a script or a tool is given, and that the engine can always stop it. The container the engine runs in is what
    separates a script from the machine; inside it a script is a child process with an environment of its own."""

    def setUp(self):
        super().setUp()
        self.registry = installed(self.settings)
        self.flow = self.registry.dir("flows", "vuln-triage")
        self.work = self.home / "data/runs/r1/items/SEC-1"
        self.work.mkdir(parents=True)
        self.probe = self.flow / "probe.sh"  # runs its one argument as a shell command, the way the engine runs a stage's script
        self.probe.write_text('#!/bin/sh\nsh -c "$1"\n')
        self.probe.chmod(0o755)

    def ctx(self, trial: bool = False) -> Ctx:
        return Ctx(self.settings, self.store, self.registry, "vuln-triage", "r1", {"id": "SEC-1"}, self.work, trial, self.settings.limits,
                   self.registry.graph("vuln-triage"), self.home / "data/runs/r1/output")

    def run_probe(self, command: str, trial: bool = False, **how) -> tuple[int, str]:
        return asyncio.run(run_proc([str(self.probe), command], self.work, env=self.ctx(trial).told(), **{**self.settings.proc(), **how}))

    def test_a_script_sees_what_the_engine_tells_it_and_none_of_its_keys(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-engine-only", "TRACKER_TOKEN": "the engine's own"}):
            self.assertEqual(self.run_probe('test -z "$OPENAI_API_KEY$TRACKER_TOKEN$USER$HOME"')[0], 0)  # nothing of the engine's environment
            self.assertEqual(self.run_probe('test -z "$AGENTFLOW_TRIAL"')[0], 0)
            self.assertEqual(self.run_probe('test "$AGENTFLOW_TRIAL" = 1', trial=True)[0], 0)  # a script is told when the run is a trial
            self.assertEqual(self.run_probe('test "$AGENTFLOW_FLOW" = vuln-triage && test "$AGENTFLOW_RUN" = r1 && test -f "$AGENTFLOW_HOME/agentflow.yaml"')[0], 0)
            self.assertEqual(self.run_probe('test "$AGENTFLOW_ITEM_ID" = SEC-1 && test -f "$AGENTFLOW_FLOW_DIR/flow.yaml" && test "$PWD" = "$(cd . && pwd)"')[0], 0)

    def test_the_exit_codes_and_the_stop(self):
        self.assertEqual(asyncio.run(run_proc([str(self.probe), "cat; echo ' seen' >&2; exit 78"], self.work, stdin='{"id": 1}', **self.settings.proc())), (DONE_EARLY, '{"id": 1} seen'))
        self.assertEqual(self.run_probe("sleep 30", timeout=1)[0], TIMED_OUT)
        # a script that ignores the request to end and has a child of its own is still stopped, child and all
        started = time.time()
        code, _ = self.run_probe('trap "" TERM; sleep 300 & echo $! > child.pid; while true; do sleep 0.1; done', stop=lambda: (self.work / "child.pid").is_file())
        self.assertEqual(code, STOPPED)
        self.assertLess(time.time() - started, 10)
        child = (self.work / "child.pid").read_text().strip()
        self.assertNotEqual(subprocess.run(["kill", "-0", child], capture_output=True).returncode, 0, "the script's child outlived it")

    def test_an_mcp_server_is_a_tool(self):
        tool = self.home / "plugins/tools/notes"
        shutil.copytree(ROOT / "tests/fixtures/mcp-notes", tool)
        (tool / "tool.yaml").write_text("description: Notes kept beside the item.\nmcp: [python3, ./server.py]\n")
        self.assertEqual(self.registry.check_tool("notes"), [])

        def script(messages) -> ModelResponse:  # a model that calls both of the server's tools, then repeats what they said
            returns = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
            if not returns:
                return ModelResponse(parts=[ToolCallPart("notes_where", {}), ToolCallPart("notes_note", {"line": "seen"})])
            return ModelResponse(parts=[TextPart(" | ".join(str(r.content) for r in returns))])

        async def stream(messages, info):
            for i, part in enumerate(script(messages).parts):
                if isinstance(part, TextPart):
                    yield part.content
                else:
                    yield {i: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args))}

        events = []
        with mock.patch("agentflow.workers.agents.build_model", lambda *a, **k: FunctionModel(lambda m, i: script(m), stream_function=stream)):
            said, stats = asyncio.run(run_agent(self.settings, "Use the tools.", "go", [self.ctx().tool("notes")], on_event=lambda kind, **d: events.append((kind, d))))
        self.assertIn(str(self.work.resolve()), said)  # the server is started in the item's folder
        self.assertIn("noted", said)
        self.assertEqual((self.work / "notes.txt").read_text(), "seen\n")
        self.assertEqual([d["tool"] for kind, d in events if kind == "tool_call"], ["notes_where", "notes_note"])
        self.assertEqual(stats["tool_calls"], 2)
        self.assertEqual(subprocess.run(["pgrep", "-f", str(tool)], capture_output=True, text=True).stdout, "")  # the server ended with the agent's run
        # its health check: it starts and lists its tools; a script tool's is its own health script
        self.assertEqual(asyncio.run(tool_health(self.settings, self.registry, "notes")), (True, "offers where, note"))
        self.assertEqual(asyncio.run(tool_health(self.settings, self.registry, "sbom-query")), (True, "finds a known package in health/sbom.json"))
        (tool / "tool.yaml").write_text("description: x\nmcp: [python3, ./gone.py]\n")
        ok, why = asyncio.run(tool_health(self.settings, self.registry, "notes"))
        self.assertEqual((ok, why[:26]), (False, "the server did not start o"))
        (self.home / "plugins/tools/sbom-query/tool.yaml").write_text("description: x\nrun: ./run.py\n")
        self.assertEqual(asyncio.run(tool_health(self.settings, self.registry, "sbom-query")),
                         (False, "tool.yaml: missing 'health': a script that exits with 0 when the tool works"))
        (tool / "tool.yaml").write_text("description: x\nmcp: python3 server.py\n")
        self.assertIn("must be a command as a list", self.registry.check_tool("notes")[0])
        (tool / "tool.yaml").write_text("description: x\nmcp: [python3, ./server.py]\nrun: ./server.py\n")
        self.assertIn("give one of", self.registry.check_tool("notes")[0])


if __name__ == "__main__":
    unittest.main()
