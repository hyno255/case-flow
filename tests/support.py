"""What every test file sets up the same way: a throwaway home with a copy of the plugins, on the stand-in model."""
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agentflow.records import log
from agentflow.settings import Settings
from agentflow.records.store import Store

ROOT = Path(__file__).resolve().parent.parent
CHANGE = {"X-Agentflow": "1"}  # the header every change through the API needs


def make_home() -> Path:
    """A throwaway home: the project's own plugins and agentflow.yaml."""
    home = Path(tempfile.mkdtemp())
    shutil.copytree(ROOT / "plugins", home / "plugins")
    shutil.copy(ROOT / "agentflow.yaml", home / "agentflow.yaml")
    return home


def make_app(settings, store):
    """The app as the tests use it: no schedule loop, and the test client's host name allowed."""
    from agentflow.web import create_app  # here, so the engine's tests do not need the pages
    return create_app(settings, store, schedule=False, hosts=("testserver",))


def wait_until(done, seconds: float = 10.0, every: float = 0.1):
    """The first true thing `done()` returns, or None when the time is up."""
    end = time.time() + seconds
    while time.time() < end:
        got = done()
        if got:
            return got
        time.sleep(every)
    return None


class Home(unittest.TestCase):
    """A copy of the plugins, an empty store, and the scripted stand-in model: no key, no network, no cost."""

    def setUp(self):
        self.home = make_home()
        self.settings = Settings(self.home)
        self.settings.stand_in = True
        self.store = Store(self.settings.data)
        self.store.listeners.append(log.writer(self.store, self.settings.internal["line_chars"]))
        self.stale = self.settings.internal["stale_after_seconds"]  # how long a silent run counts as still working

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def items(self, run_id: str) -> dict:
        return {r["item_id"]: r for r in self.store.items(run_id)}

    def finished_run(self, c) -> str:
        """The id of the newest run, once the API says it has finished."""
        def newest():
            runs = c.get("/api/runs").json()
            return runs[0]["id"] if runs and runs[0]["status"] == "finished" else None
        return wait_until(newest) or self.fail("the run did not finish")
