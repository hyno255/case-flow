"""Watchdog, resume, cancel and the time cap. Scripted stand-in model: no key, no network, no cost.

To stop a run at a known point, the analyse stage's check is wrapped in a gate: it runs the real check and,
when that passes, waits for as long as a "hold" file exists.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import unittest

from fastapi.testclient import TestClient

from agentflow.workers.procs import STOPPED, TIMED_OUT, run_proc
from agentflow.engine.recovery import claim_resume, request_cancel, resume_run, sweep
from agentflow.engine.runner import start_flow
from agentflow.records.status import run_status
from support import CHANGE, ROOT, Home, make_app


class Recovery(Home):
    def setUp(self):
        super().setUp()
        self.hold = self.home / "hold"

    def tearDown(self):
        self.hold.unlink(missing_ok=True)
        time.sleep(0.3)  # let any gate script see that the hold is gone
        super().tearDown()

    def add_gate(self):
        flow = self.home / "plugins/flows/vuln-triage"
        gate = flow / "scripts/gate.sh"
        gate.write_text(f'#!/bin/sh\n"$(dirname "$0")"/chain-cites-files.py || exit 1\nwhile [ -f "{self.hold}" ]; do sleep 0.1; done\nexit 0\n')
        gate.chmod(0o755)
        (flow / "flow.yaml").write_text((flow / "flow.yaml").read_text().replace("check: scripts/chain-cites-files.py", "check: scripts/gate.sh"))
        self.hold.write_text("")

    def held(self) -> int:
        """How many items sit in the gate: their second analyse try is done and its check is waiting."""
        return self.store.one("SELECT COUNT(*) n FROM attempts WHERE stage = 'analyse' AND n = 2 AND outcome = 'done'")["n"]

    def scripts(self) -> list[str]:
        """The processes still running a script from this test's plugins: gate scripts and their guards."""
        return subprocess.run(["pgrep", "-f", str(self.home)], capture_output=True, text=True).stdout.split()

    def assert_scripts_gone(self, within: float = 6.0):
        end = time.time() + within
        while self.scripts() and time.time() < end:
            time.sleep(0.1)
        self.assertEqual(self.scripts(), [], "a script outlived the thing that should have stopped it")

    def tries(self, run_id, item, stage):
        return [(a["n"], a["outcome"]) for a in self.store.q("SELECT n, outcome FROM attempts WHERE run_id = ? AND item_id = ? AND stage = ? ORDER BY n", run_id, item, stage)]

    def test_kill_the_process_then_resume(self):
        self.add_gate()
        env = {**os.environ, "AGENTFLOW_HOME": str(self.home), "PYTHONPATH": str(ROOT)}
        proc = subprocess.Popen([sys.executable, "-m", "agentflow", "run", "vuln-triage", "--stand-in"],
                                cwd=self.home, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(300):
                if self.held() == 3:
                    break
                time.sleep(0.1)
            self.assertEqual(self.held(), 3, "the run did not reach the gate")
        finally:
            self.assertGreaterEqual(len(self.scripts()), 3)  # the gate scripts are waiting
            proc.kill()  # no clean-up of any kind: the process is simply gone
            proc.wait()
        self.assert_scripts_gone()  # the hold is still in place: their guards stopped them
        self.hold.unlink()
        run = self.store.one("SELECT * FROM runs")
        run_id = run["id"]
        self.assertIsNone(run["ended"])

        # at first the run still looks alive; once it has been quiet long enough it is shown as interrupted
        self.assertEqual(run_status(run, self.stale)[0], "running")
        self.assertEqual(sweep(self.store, self.stale), [])
        self.assertEqual(sweep(self.store, stale_after=0), [run_id])
        self.assertEqual(sweep(self.store, stale_after=0), [])  # recorded once
        self.assertEqual(self.tries(run_id, "SEC-1042", "analyse"), [(1, "rejected"), (2, "interrupted")])
        events = [e["type"] for e in self.store.q("SELECT type FROM events WHERE run_id = ? ORDER BY seq", run_id)]
        self.assertEqual(events.count("run_interrupted"), 1)
        self.assertEqual(events.count("attempt_interrupted"), 3)
        self.assertNotIn("finish_ended", events)  # the flow's finish script waits until every item has ended
        before = self.items(run_id)
        self.assertEqual(before["SEC-1045"]["ended_as"], "needs_person")  # it had ended before the kill
        self.assertIsNone(before["SEC-1042"]["ended_at"])

        asyncio.run(resume_run(self.settings, self.store, run_id, stale_after=0))

        after = self.items(run_id)
        self.assertEqual({k: v["ended_as"] for k, v in after.items()},
                         {"SEC-1042": "done", "SEC-1043": "done", "SEC-1044": "needs_person", "SEC-1045": "needs_person"})
        self.assertEqual(after["SEC-1045"]["ended_at"], before["SEC-1045"]["ended_at"])  # an ended item is left alone
        for item in ("SEC-1042", "SEC-1043", "SEC-1044"):
            self.assertEqual(self.tries(run_id, item, "gather"), [(1, "passed")], item)  # a finished stage is not done again
        # the interrupted try does not count against the two tries, and the first try's reason reached the third
        self.assertEqual(self.tries(run_id, "SEC-1042", "analyse"), [(1, "rejected"), (2, "interrupted"), (3, "passed")])
        work = self.store.path(after["SEC-1042"])
        self.assertIn("cites no file", (work / "attempts/analyse-3.prompt.md").read_text())
        lines = [json.loads(l) for l in (work / "events.jsonl").read_text().splitlines()]
        self.assertIn({"type": "item_started", "from_stage": "analyse"}, [{k: l[k] for k in ("type", "from_stage") if k in l} for l in lines])

        # the flow's finish script runs once, after the resume, with the finished result
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM events WHERE run_id = ? AND type = 'finish_ended'", run_id)["n"], 1)
        run = self.store.one("SELECT * FROM runs WHERE id = ?", run_id)
        self.assertEqual(run_status(run, self.stale)[0], "finished")
        self.assertIn("2 triaged, 2 needs a person", (self.store.path(run) / "output/index.html").read_text())
        with self.assertRaisesRegex(ValueError, "has finished"):
            claim_resume(self.store, run_id, stale_after=0)

    def test_cancel_a_live_run_and_close_a_dead_one(self):
        self.add_gate()

        async def scenario():
            task = asyncio.create_task(start_flow(self.settings, self.store, "vuln-triage"))
            for _ in range(300):
                if self.held() == 3:
                    break
                await asyncio.sleep(0.05)
            run_id = self.store.one("SELECT id FROM runs")["id"]
            with self.assertRaisesRegex(ValueError, "still running"):  # a live run cannot be taken over
                claim_resume(self.store, run_id, self.stale)
            self.assertEqual(request_cancel(self.store, run_id, self.stale), "asked")
            self.assertEqual(run_status(self.store.one("SELECT * FROM runs"), self.stale)[0], "stopping")
            await task  # the hold is still in place: the gate scripts are stopped, not waited for
            return run_id

        started = time.time()
        run_id = asyncio.run(scenario())
        self.assertLess(time.time() - started, 20)
        self.assert_scripts_gone(within=0)
        # the check script that was running was stopped, and no item went on to its next stage
        items = self.items(run_id)
        self.assertEqual({k: v["ended_as"] for k, v in items.items()},
                         {"SEC-1042": "cancelled", "SEC-1043": "cancelled", "SEC-1044": "cancelled", "SEC-1045": "needs_person"})
        self.assertIn("gate.sh", items["SEC-1042"]["reason"])
        self.assertEqual(self.tries(run_id, "SEC-1042", "analyse")[-1], (2, "cancelled"))
        self.assertEqual(self.tries(run_id, "SEC-1042", "fix"), [])
        self.assertEqual(run_status(self.store.one("SELECT * FROM runs"), self.stale)[0], "cancelled")
        self.assertTrue((self.store.path(self.items(run_id)["SEC-1042"]) / "origin.md").is_file())  # folders are kept
        with self.assertRaisesRegex(ValueError, "already finished"):
            request_cancel(self.store, run_id, self.stale)

        # a run whose process is gone can be closed without resuming it
        self.hold.write_text("")

        async def dies():
            task = asyncio.create_task(start_flow(self.settings, self.store, "vuln-triage"))
            for _ in range(300):
                if self.held() == 3:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(self.held(), 3, "the second run did not reach the gate")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(dies())
        self.assert_scripts_gone(within=0)  # a task that is cancelled takes its scripts with it
        dead = self.store.one("SELECT * FROM runs WHERE ended IS NULL")
        self.assertEqual(request_cancel(self.store, dead["id"], stale_after=0), "closed")
        self.assertEqual({v["ended_as"] for k, v in self.items(dead["id"]).items() if k != "SEC-1045"}, {"cancelled"})
        self.assertIsNotNone(self.store.one("SELECT ended FROM runs WHERE id = ?", dead["id"])["ended"])

    def test_time_cap(self):
        self.add_gate()
        self.settings.limits["minutes_per_item"] = 0.01  # 0.6 seconds
        run_id = asyncio.run(start_flow(self.settings, self.store, "vuln-triage"))
        self.assert_scripts_gone(within=0)  # the hold is still in place: the scripts were stopped when the time ran out
        items = self.items(run_id)
        self.assertEqual({k: v["ended_as"] for k, v in items.items()},
                         {"SEC-1042": "timed_out", "SEC-1043": "timed_out", "SEC-1044": "timed_out", "SEC-1045": "needs_person"})
        self.assertIn("not finished after 0.01 minutes", items["SEC-1042"]["reason"])
        self.assertEqual(self.tries(run_id, "SEC-1042", "analyse")[-1], (2, "timed_out"))
        self.assertIsNotNone(self.store.one("SELECT ended FROM runs WHERE id = ?", run_id)["ended"])

    def test_a_stopped_script_takes_its_children_and_cannot_refuse(self):
        script, child = self.home / "stubborn.sh", self.home / "child.pid"
        # ignores the request to end, and starts a child that would run for five minutes
        script.write_text(f'#!/bin/sh\ntrap "" TERM\nsleep 300 &\necho $! > "{child}"\nwhile true; do sleep 0.1; done\n')
        script.chmod(0o755)

        def child_alive() -> bool:
            try:
                os.kill(int(child.read_text()), 0)
                return True
            except (OSError, ValueError):
                return False

        async def stopped_by(**how):
            child.unlink(missing_ok=True)
            started = time.time()
            code, _ = await run_proc([str(script)], self.home, **{**self.settings.proc(), **how})
            self.assertLess(time.time() - started, 8)
            self.assertFalse(child_alive())
            return code

        async def cancelled_task():
            child.unlink(missing_ok=True)
            task = asyncio.create_task(run_proc([str(script)], self.home, **self.settings.proc()))
            while not child.is_file() or not child.read_text().strip():
                await asyncio.sleep(0.05)
            self.assertTrue(child_alive())
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(child_alive())

        self.assertEqual(asyncio.run(stopped_by(stop=lambda: child.is_file())), STOPPED)  # as a cancelled run does
        self.assertEqual(asyncio.run(stopped_by(timeout=1)), TIMED_OUT)                   # the script's own limit
        asyncio.run(cancelled_task())                                                     # as an item out of time does
        self.assert_scripts_gone(within=0)
        # an ordinary script is not disturbed: its output and its exit code come back
        script.write_text('#!/bin/sh\ncat\necho " and out" >&2\nexit 3\n')
        self.assertEqual(asyncio.run(run_proc([str(script)], self.home, stdin="in", **self.settings.proc())), (3, "in and out"))

    def test_resume_and_cancel_from_the_page(self):
        app = make_app(self.settings, self.store)
        with TestClient(app) as c:
            c.post("/api/flows/vuln-triage/run", json={}, headers=CHANGE)
            for _ in range(100):
                runs = c.get("/api/runs").json()
                if runs and runs[0]["status"] == "finished":
                    break
                time.sleep(0.1)
            run_id = runs[0]["id"]
            self.assertEqual(c.post(f"/api/runs/{run_id}/resume", headers=CHANGE).status_code, 409)
            self.assertEqual(c.post(f"/api/runs/{run_id}/cancel", headers=CHANGE).status_code, 409)

            # an interrupted run shows the Resume button, and the button's endpoint finishes it
            self.store.db.execute("UPDATE runs SET ended = NULL, beat = 0 WHERE id = ?", (run_id,))
            self.store.db.execute("UPDATE items SET ended_at = NULL, ended_as = NULL WHERE run_id = ? AND item_id = 'SEC-1042'", (run_id,))
            self.store.db.execute("UPDATE attempts SET outcome = 'done' WHERE run_id = ? AND item_id = 'SEC-1042' AND stage = 'report'", (run_id,))
            html = c.get(f"/runs/{run_id}").text
            self.assertIn('<span class="pill bad">interrupted</span>', html)
            self.assertIn(f'data-post="/api/runs/{run_id}/resume"', html)
            self.assertEqual(c.get(f"/api/runs/{run_id}").json()["items"][0]["status"], "interrupted")
            self.assertEqual(c.post(f"/api/runs/{run_id}/resume", headers=CHANGE).status_code, 202)
            for _ in range(100):
                if c.get(f"/api/runs/{run_id}").json()["status"] == "finished":
                    break
                time.sleep(0.1)
            self.assertEqual(self.tries(run_id, "SEC-1042", "report"), [(1, "interrupted"), (2, "passed")])
            self.assertEqual(self.tries(run_id, "SEC-1042", "fix"), [(1, "passed")])


if __name__ == "__main__":
    unittest.main()
