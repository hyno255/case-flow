"""A stage's work handed to a coding CLI. On the stand-in the scripted CLI is started in place of Codex or Claude Code:
no sign-in, no network, no cost. What a CLI can do wrong is played by small scripts started in its place."""
import asyncio
import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from genai_prices import Usage, calc_price

from agentflow.records import trace
from agentflow.workers import harness
from agentflow.workers.agents import ModelNotReady, check_ready
from agentflow.workers.harness import CLIS, Cli, CliFailed, CliStopped, Job, cli_note, cli_version, run_cli, tool_servers
from agentflow.plugins.registry import installed
from agentflow.engine.recovery import request_cancel
from agentflow.engine.runner import start_flow
from support import ROOT, Home, make_app

FLOW = "plugins/flows/vuln-triage/flow.yaml"
CODEX_SAID = ('{"type":"thread.started","thread_id":"t1"}\n'
              '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"ls"}}\n'
              '{"type":"item.completed","item":{"id":"i2","type":"agent_message","text":"from the events"}}\n'
              '{"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":400,"output_tokens":100}}\n')


class HandedOver(Home):
    """The analyse stage of vuln-triage, done by a CLI."""

    def hand_over(self, name: str) -> None:
        self.settings.models["on-cli"] = {"harness": name, "name": "gpt-6-luna"}
        flow = self.home / FLOW
        flow.write_text(flow.read_text().replace("    agent: origin-analyst\n", "    agent: origin-analyst\n    model: on-cli\n"))
        self.assertEqual(installed(self.settings).check_flow("vuln-triage"), [])

    def a_run_on(self, name: str) -> None:
        self.hand_over(name)
        run_id = asyncio.run(start_flow(self.settings, self.store, "vuln-triage"))
        items = self.items(run_id)
        # the items end as they do when the engine's own harness does the stage
        self.assertEqual({k: v["ended_as"] for k, v in items.items()},
                         {"SEC-1042": "done", "SEC-1043": "done", "SEC-1044": "needs_person", "SEC-1045": "needs_person"})
        work = self.store.path(items["SEC-1042"])

        # the check script's reason reached the CLI's second try, and the commands the prompt named gave it its evidence
        tries = self.store.q("SELECT n, outcome, model_calls, cost_usd FROM attempts WHERE run_id = ? AND item_id = 'SEC-1042' AND stage = 'analyse' ORDER BY n", run_id)
        self.assertEqual([(t["n"], t["outcome"], t["model_calls"], t["cost_usd"]) for t in tries], [(1, "rejected", 1, 0), (2, "passed", 1, 0)])
        self.assertIn("cites no file", (work / "attempts/analyse-2.prompt.md").read_text())
        self.assertIn("(file: dependency-tree.json)", (work / "origin.md").read_text())
        self.assertIn("4.3.1", (work / "fix.md").read_text())  # the later stages, done by the engine, go on from the CLI's file

        # everything the CLI printed is kept beside what the try was told
        printed = [json.loads(l) for l in (work / f"attempts/analyse-1.{name}.jsonl").read_text().splitlines()]
        self.assertEqual(printed[-1]["type"], "turn.completed" if name == "codex" else "result")
        self.assertEqual((work / "attempts/analyse-1.codex.md").is_file(), name == "codex")  # Codex writes its last message to a file

        # a CLI asks for a person with a file, which the engine reads and takes away
        asked = self.store.path(items["SEC-1045"])
        self.assertIn("neither", items["SEC-1045"]["reason"])
        self.assertIn("neither", (asked / "attempts/analyse-1.needs-person.md").read_text())
        self.assertFalse((asked / "needs-person.md").exists() or (asked / "origin.md").exists())

        # one event per run of the CLI, and no tool call of the analyst's: the engine does not follow what a CLI does
        events = [json.loads(l) for l in (work / "events.jsonl").read_text().splitlines()]
        runs = [e for e in events if e["type"] == "cli_run"]
        self.assertEqual([(e["harness"], e["n"], e["output"], e["cost_usd"]) for e in runs],
                         [(name, n, f"attempts/analyse-{n}.{name}.jsonl", 0) for n in (1, 2)])
        self.assertTrue(all(e["tokens_in"] > 0 and e["said"] == "Done." for e in runs))
        self.assertEqual([e for e in events if e["type"] == "tool_call" and e["stage"] == "analyse" and not e.get("role")], [])
        self.assertEqual([e["model"] for e in events if e["type"] == "attempt_started" and e["stage"] == "analyse"], [f"{name} CLI · scripted stand-in"] * 2)

        # the stand-in costs nothing, and that is known
        usage = self.store.usage(run_id)
        self.assertGreater(usage["tokens"], 0)
        self.assertEqual((usage["cost_known"], usage["cost_usd"]), (True, 0))

        # the trace: the CLI's run is one span of the try, with its tokens, and a link to what it printed
        tree = trace.build(self.store, self.store.run(run_id), self.settings.internal, "SEC-1042", folder=work)
        clis = [s for s in trace.walk(tree["root"]) if s["kind"] == "cli"]
        self.assertEqual([(s["name"], s["cli_runs"], s["cost_note"]) for s in clis], [(name, 1, "free")] * 2)
        self.assertEqual(clis[0]["links"], [("output", f"attempts/analyse-1.{name}.jsonl", f"/runs/{run_id}/items/SEC-1042/files/attempts/analyse-1.{name}.jsonl")])
        stage = next(s for s in trace.walk(tree["root"]) if s["kind"] == "stage" and s["name"] == "analyse")
        self.assertEqual((stage["cli_runs"], stage["tool_calls"] > 0, stage["model_calls"] > 0), (2, True, True))  # the reviewer's own calls are still counted

        with TestClient(make_app(self.settings, self.store)) as c:
            page = c.get(f"/runs/{run_id}/items/SEC-1042/trace").text
            for text in ('<i class="kd k-cli">cli</i>', "<dt>CLI runs</dt><dd>2</dd>", f'<a href="{clis[0]["links"][0][2]}">', f"{name} CLI · scripted stand-in"):
                self.assertIn(text, page)
            self.assertIn(printed[-1]["type"], c.get(clis[0]["links"][0][2]).text)
            settings = c.get("/settings").text
            for text in (f"<td>{name} CLI</td>", "the CLI's own sign-in", "<td>engine</td>"):
                self.assertIn(text, settings)

    def test_a_stage_done_by_codex(self):
        self.a_run_on("codex")

    def test_a_stage_done_by_claude_code(self):
        self.a_run_on("claude")

    def test_a_cancelled_run_stops_the_cli(self):
        self.hand_over("codex")
        slow = self.home / "slow_cli.py"  # started in place of the CLI: it starts a child of its own and never ends
        slow.write_text("import os, subprocess, time\nchild = subprocess.Popen(['sleep', '300'])\n"
                        "open(f'{os.environ[\"AGENTFLOW_ITEM_ID\"]}.pid', 'w').write(str(child.pid))\ntime.sleep(300)\n")

        def children() -> list[str]:
            return [p.read_text() for p in (self.home / "data").rglob("SEC-*.pid") if p.read_text()]

        async def scenario() -> str:
            task = asyncio.create_task(start_flow(self.settings, self.store, "vuln-triage"))
            for _ in range(300):
                if len(children()) == 4:
                    break
                await asyncio.sleep(0.05)
            run_id = self.store.one("SELECT id FROM runs")["id"]
            self.assertEqual(request_cancel(self.store, run_id, self.stale), "asked")
            await task
            return run_id

        started = time.time()
        with mock.patch.object(harness, "STAND_IN", str(slow)):
            run_id = asyncio.run(scenario())
        self.assertLess(time.time() - started, 20)
        for pid in children():
            self.assertNotEqual(subprocess.run(["kill", "-0", pid], capture_output=True).returncode, 0, "what the CLI started outlived it")
        items = self.items(run_id)
        self.assertEqual({v["ended_as"] for v in items.values()}, {"cancelled"})
        self.assertIn("the codex CLI was working on analyse", items["SEC-1042"]["reason"])
        self.assertEqual([t["outcome"] for t in self.store.q("SELECT outcome FROM attempts WHERE run_id = ? AND stage = 'analyse'", run_id)], ["cancelled"] * 4)
        self.assertFalse(self.store.usage(run_id)["cost_known"])  # a CLI that was stopped used its model, and never said how much

    def test_a_cli_gets_the_mcp_tools_and_can_review(self):
        self.hand_over("codex")
        flow, agent = self.home / FLOW, self.home / "plugins/agents/origin-analyst/AGENT.md"

        # a tool that is an MCP server goes to the CLI as an MCP server of that one run, beside the CLI's own tools
        tool = self.home / "plugins/tools/notes"
        shutil.copytree(ROOT / "tests/fixtures/mcp-notes", tool)
        (tool / "tool.yaml").write_text("description: Notes kept beside the item.\nmcp: [python3, ./server.py]\n")
        agent.write_text(agent.read_text().replace("tools: [sbom-query, deps-why]", "tools: [sbom-query, deps-why, notes]"))
        live = installed(self.settings)
        self.assertEqual(live.check_flow("vuln-triage"), [])
        servers = tool_servers(live, ["sbom-query", "notes"], {"AGENTFLOW_RUN": "r1"})
        self.assertEqual(servers, (("notes", ["python3", str((tool / "server.py").resolve())], {"AGENTFLOW_RUN": "r1"}),))
        self.assertIn("MCP servers you are given", cli_note([], servers))
        codex = CLIS["codex"].args("m", Path("last.md"), Job(servers=servers))
        for text in ('mcp_servers.notes.command="python3"', f'mcp_servers.notes.args=["{(tool / "server.py").resolve()}"]', 'mcp_servers.notes.env={"AGENTFLOW_RUN" = "r1"}', "workspace-write"):
            self.assertIn(text, codex)
        claude = CLIS["claude"].args("m", Path("last.md"), Job(commands=("/bin/tool",), servers=servers))
        self.assertEqual(json.loads(claude[claude.index("--mcp-config") + 1])["mcpServers"]["notes"]["command"], "python3")
        self.assertEqual(claude[claude.index("--allowedTools") + 1:], ["Bash(/bin/tool *)", "mcp__notes"])

        # a reviewer on a CLI reads only, and answers in the shape it is given
        schema = self.home / "schema.json"
        schema.write_text("{}")
        review = Job(schema=schema, write=False)
        self.assertIn("read-only", CLIS["codex"].args("m", Path("last.md"), review))
        self.assertIn("--output-schema", CLIS["codex"].args("m", Path("last.md"), review))
        claude = CLIS["claude"].args("m", Path("last.md"), review)
        self.assertEqual((claude[claude.index("--permission-mode") + 1], claude[claude.index("--json-schema") + 1]), ("default", "{}"))
        self.assertEqual(CLIS["claude"].read([{"type": "result", "result": "", "structured_output": {"passed": True}}])["text"], '{"passed": true}')

        # the flow's model on a CLI: every stage and every reviewer goes to it, and the items end the same
        agent.write_text(agent.read_text().replace(", notes]", "]"))
        flow.write_text(flow.read_text().replace("source: security-tickets\n", "source: security-tickets\nmodel: on-cli\n"))
        self.assertEqual(installed(self.settings).check_flow("vuln-triage"), [])
        run_id = asyncio.run(start_flow(self.settings, self.store, "vuln-triage"))
        items = self.items(run_id)
        self.assertEqual({k: v["ended_as"] for k, v in items.items()},
                         {"SEC-1042": "done", "SEC-1043": "done", "SEC-1044": "needs_person", "SEC-1045": "needs_person"})
        work = self.store.path(items["SEC-1042"])
        for file in ("attempts/analyse-review-2.codex.jsonl", "attempts/analyse-review-2.codex.schema.json", "attempts/report-1.codex.jsonl", "attempts/report-review-1.codex.md"):
            self.assertTrue((work / file).is_file(), file)
        said = self.store.q("SELECT by, passed FROM verdicts WHERE run_id = ? AND item_id = 'SEC-1042' AND stage = 'report'", run_id)
        self.assertEqual([(v["by"], v["passed"]) for v in said], [("checker", 1)])
        self.assertEqual(self.store.q("SELECT type FROM events WHERE run_id = ? AND type IN ('model_call', 'tool_call')", run_id), [])  # nothing ran in the engine

        # agentflow.yaml: an entry on a CLI has three keys, names a CLI the engine knows, and is not the default
        self.settings.models["odd"] = {"harness": "nope", "name": "m", "api": "chat"}
        self.settings.models["bare"] = {"harness": "claude"}
        self.settings.default_model = "on-cli"  # the default may be on a CLI too
        self.assertEqual(self.settings.check(), ["models: odd: unknown key 'api'; a model on a CLI takes api_key_env, harness, name",
                                                 "models: odd: no harness named 'nope'; the engine knows codex, claude", "models: bare: missing 'name'"])
        self.settings.stand_in = False
        self.assertEqual((self.settings.model_label("on-cli"), self.settings.harness_of("on-cli"), self.settings.harness_of("luna"), self.settings.harness_of("gone")),
                         ("codex CLI · gpt-6-luna", "codex", None, None))

    def test_a_run_stops_before_any_work_when_the_cli_cannot_start(self):
        self.hand_over("codex")
        self.settings.stand_in = False
        os.environ["OPENAI_API_KEY"] = "sk-test-not-real"  # the engine's own stages are ready
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY", None)
        there = Cli("sh", lambda *a: [], CLIS["codex"].read, key="K", prices="openai")
        with mock.patch.dict(CLIS, {"codex": there}):
            check_ready(self.settings, "on-cli")
            self.settings.models["on-cli"]["api_key_env"] = "KEY_NOT_SET"
            with self.assertRaisesRegex(ModelNotReady, "KEY_NOT_SET is not set"):
                check_ready(self.settings, "on-cli")
        with mock.patch.dict(CLIS, {"codex": Cli("no-such-cli-anywhere", lambda *a: [], CLIS["codex"].read, key="K", prices="openai")}):
            with self.assertRaisesRegex(ModelNotReady, "the codex CLI is not installed"):
                asyncio.run(start_flow(self.settings, self.store, "vuln-triage"))


class OneRun(Home):
    """run_cli with a small script started in place of a CLI, and that CLI's own way of reading what it printed."""

    def setUp(self):
        super().setUp()
        self.settings.stand_in = False
        self.work = self.home / "data/runs/r1/items/SEC-1"
        (self.work / "attempts").mkdir(parents=True)
        self.events: list[dict] = []

    def run_as(self, like: str, script: str, model: dict | None = None, **how) -> tuple[str, dict]:
        """One try on a script that stands where the CLI `like` would: it gets the model's name and the last-message file as arguments."""
        exe = self.home / "fake-cli"
        exe.write_text("#!/bin/sh\n" + script)
        exe.chmod(0o755)
        fake = Cli(str(exe), lambda model, last, job: [model, str(last), *job.commands], CLIS[like].read, key="FAKE_KEY", prices=CLIS[like].prices)
        with mock.patch.dict(CLIS, {"fake": fake}):
            return asyncio.run(run_cli(self.settings, {"harness": "fake", "name": "gpt-6-luna", **(model or {})}, "Instructions.", "Prompt.", self.work,
                                       self.work / "attempts/a-1.fake.jsonl", self.work / "attempts/a-1.fake.md", {"AGENTFLOW_ITEM_ID": "SEC-1"},
                                       self.settings.limits, on_event=lambda t, **d: self.events.append({"type": t, **d}), **how))

    def test_codex_says_tokens_and_the_engine_prices_them(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-not-real"}):
            text, used = self.run_as("codex", f"cat > told.txt; env > env.txt; printf 'from the file' > \"$2\"; printf '%s' '{CODEX_SAID}'")
            price = float(calc_price(Usage(input_tokens=1000, cache_read_tokens=400, output_tokens=100), model_ref="gpt-6-luna", provider_id="openai").total_price)
            self.assertEqual((text, used["tokens_in"], used["tokens_out"], used["model_calls"]), ("from the file", 1000, 100, 1))
            self.assertAlmostEqual(used["cost_usd"], price)
            self.assertGreater(price, 0)
            self.assertEqual((self.work / "told.txt").read_text(), "Instructions.\n\nPrompt.")  # the instructions, then the prompt, on stdin
            self.assertEqual([(e["type"], e["harness"], e["model"], e["reported"], e["output"]) for e in self.events],
                             [("cli_run", "fake", "gpt-6-luna", False, "attempts/a-1.fake.jsonl")])
            self.assertIn("turn.completed", (self.work / "attempts/a-1.fake.jsonl").read_text())

            # the CLI sees who it runs as and which item, and nothing else of the engine's: it uses its own sign-in
            env = dict(l.split("=", 1) for l in (self.work / "env.txt").read_text().splitlines() if "=" in l)
            self.assertEqual((env["AGENTFLOW_ITEM_ID"], env["HOME"], "OPENAI_API_KEY" in env, "FAKE_KEY" in env), ("SEC-1", os.environ["HOME"], False, False))
            # a key the model entry names is handed over under the CLI's own name for it
            self.run_as("codex", f"env > env.txt; printf '%s' '{CODEX_SAID}'", {"api_key_env": "OPENAI_API_KEY"})
            env = dict(l.split("=", 1) for l in (self.work / "env.txt").read_text().splitlines() if "=" in l)
            self.assertEqual((env["FAKE_KEY"], "OPENAI_API_KEY" in env), ("sk-test-not-real", False))
        with self.assertRaisesRegex(CliFailed, "KEY_NOT_SET is not set"):
            self.run_as("codex", "exit 0", {"api_key_env": "KEY_NOT_SET"})

        # without the file, the last agent message in the events is the answer; a model the price list lacks has no cost, which is not zero
        text, used = self.run_as("codex", f"printf '%s' '{CODEX_SAID}'", {"name": "a-model-with-no-price"})
        self.assertEqual((text, used["tokens_in"], used["cost_usd"]), ("from the events", 1000, None))

    def test_claude_code_says_what_it_cost(self):
        said = {"type": "result", "subtype": "success", "is_error": False, "result": "origin.md is written.", "total_cost_usd": 0.0412,
                "usage": {"input_tokens": 20, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 18100, "output_tokens": 1630}}
        text, used = self.run_as("claude", f"echo 'a line that is not JSON'; echo '{json.dumps(said)}'")
        self.assertEqual((text, used["cost_usd"], used["tokens_in"], used["tokens_out"]), ("origin.md is written.", 0.0412, 18420, 1630))
        self.assertTrue(self.events[-1]["reported"])
        # when it fails it says why itself, which is kept as the reason whatever it exits with
        with self.assertRaisesRegex(CliFailed, "^fake ended with an error: Failed to authenticate: OAuth session expired$"):
            self.run_as("claude", f"echo '{json.dumps({**said, 'is_error': True, 'result': 'Failed to authenticate: OAuth session expired'})}'; exit 1")
        with self.assertRaisesRegex(CliFailed, "^fake ended with an error: The model does not exist$"):
            self.run_as("codex", """echo '{"type":"error","message":"The model does not exist"}'; echo '{"type":"turn.failed","error":{"message":"The model does not exist"}}'; exit 1""")

    def test_a_cli_that_fails_says_nothing_or_is_missing(self):
        with self.assertRaisesRegex(CliFailed, "(?s)fake exited with code 3: .*boom"):
            self.run_as("codex", "echo started; echo boom >&2; exit 3")
        self.assertIn("boom", (self.work / "attempts/a-1.fake.jsonl").read_text())  # what it printed is kept either way
        with self.assertRaisesRegex(CliFailed, "fake ended without an answer; what it printed is in a-1.fake.jsonl"):
            self.run_as("codex", "echo 'no JSON here'")
        long = self.settings.internal["error_chars"]
        with self.assertRaises(CliFailed) as failed:
            self.run_as("codex", f"head -c {long * 3} /dev/zero | tr '\\0' 'x'; echo ' the end'; exit 1")
        self.assertEqual((len(str(failed.exception)), str(failed.exception).endswith("the end")), (long, True))  # the reason keeps the end, within its size

        missing = Cli("no-such-cli-anywhere", lambda *a: [], CLIS["codex"].read, key="K", prices="openai")
        with mock.patch.dict(CLIS, {"fake": missing}):
            with self.assertRaisesRegex(CliFailed, "fake exited with code 126: could not start"):
                asyncio.run(run_cli(self.settings, {"harness": "fake", "name": "m"}, "", "", self.work, self.work / "attempts/a-1.fake.jsonl",
                                    self.work / "attempts/a-1.fake.md", {}, self.settings.limits))
            self.assertEqual(asyncio.run(cli_version(self.settings, "fake"))[0], False)
        (self.home / "fake-cli").write_text("#!/bin/sh\necho 'fake-cli 1.2.3'\n")  # made runnable by the tries above
        with mock.patch.dict(CLIS, {"fake": Cli(str(self.home / "fake-cli"), lambda *a: [], CLIS["codex"].read, key="K", prices="openai")}):
            self.assertEqual(asyncio.run(cli_version(self.settings, "fake")), (True, "fake-cli 1.2.3"))

    def test_the_stop_and_the_time_limit(self):
        started = time.time()
        with self.assertRaises(CliStopped):
            self.run_as("codex", 'trap "" TERM; sleep 300 & echo $! > child.pid; while true; do sleep 0.1; done', stop=lambda: (self.work / "child.pid").is_file())
        self.assertLess(time.time() - started, 10)
        child = (self.work / "child.pid").read_text().strip()
        self.assertNotEqual(subprocess.run(["kill", "-0", child], capture_output=True).returncode, 0, "what the CLI started outlived it")
        # a CLI may take as long as an item may, not as long as a script may
        self.settings.limits.update(script_seconds=1, minutes_per_item=1)
        self.assertEqual(self.run_as("codex", f"sleep 2; printf '%s' '{CODEX_SAID}'")[0], "from the events")
        self.settings.limits["minutes_per_item"] = 1 / 60
        with self.assertRaisesRegex(CliFailed, "fake exited with code 124: timed out after 1s"):
            self.run_as("codex", "sleep 30")


if __name__ == "__main__":
    unittest.main()
