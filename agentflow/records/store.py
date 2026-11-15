"""The run store: one SQLite file. Facts go in; status is worked out when asked. Where it lives: layout.py."""
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from agentflow.records.layout import DB, EVENTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (flow TEXT PRIMARY KEY, spec TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, next_due TEXT);
CREATE TABLE IF NOT EXISTS fetches (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, scheduled_for TEXT NOT NULL, started TEXT, ended TEXT,
  outcome TEXT, item_count INTEGER, detail TEXT, folder TEXT, UNIQUE (source, scheduled_for));
CREATE TABLE IF NOT EXISTS health (tool TEXT PRIMARY KEY, ok INTEGER NOT NULL, detail TEXT, checked TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, flow TEXT NOT NULL, source TEXT, fetch_id INTEGER, snapshot TEXT, trial INTEGER NOT NULL DEFAULT 0,
  folder TEXT, started TEXT, ended TEXT,
  beat REAL, interrupted_at REAL, cancel_requested INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, flow TEXT NOT NULL, item_id TEXT NOT NULL,
  folder TEXT, claimed_at TEXT, ended_at TEXT, ended_as TEXT, reason TEXT, payload TEXT,
  UNIQUE (run_id, item_id));
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, item_id TEXT NOT NULL, stage TEXT NOT NULL, n INTEGER NOT NULL, by TEXT,
  started REAL, ended REAL, outcome TEXT, model_calls INTEGER DEFAULT 0, tool_calls INTEGER DEFAULT 0,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, cost_usd REAL);
CREATE TABLE IF NOT EXISTS verdicts (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, item_id TEXT NOT NULL, stage TEXT NOT NULL, n INTEGER NOT NULL, by TEXT,
  passed INTEGER, reason TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, cost_usd REAL);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, run_id TEXT, item_id TEXT, stage TEXT, type TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id, seq);
CREATE INDEX IF NOT EXISTS attempts_item ON attempts (run_id, item_id, stage, n);
CREATE INDEX IF NOT EXISTS verdicts_item ON verdicts (run_id, item_id, stage, n);
"""



def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def event_of(row, data: dict | None = None) -> dict:
    """One event as everyone reads it: the fixed fields, then whatever the event carries."""
    return {"seq": row["seq"], "ts": round(row["ts"], 3), "run": row["run_id"], "item": row["item_id"], "stage": row["stage"], "type": row["type"],
            **(data if data is not None else json.loads(row["data"] or "{}"))}


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()  # the data folder
        path = self.root / DB
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.listeners: list = []

    def path(self, row) -> Path:
        """The folder of a run or an item, from its row."""
        return self.root / row["folder"]

    # ---- lookups everyone needs ----
    def run(self, run_id: str):
        return self.one("SELECT * FROM runs WHERE id = ?", run_id)

    def need_run(self, run_id: str):
        run = self.run(run_id)
        if run is None:
            raise ValueError(f"no run '{run_id}'")
        return run

    def item(self, run_id: str, item_id: str):
        return self.one("SELECT * FROM items WHERE run_id = ? AND item_id = ?", run_id, item_id)

    def items(self, run_id: str, open_only: bool = False) -> list[sqlite3.Row]:
        return self.q(f"SELECT * FROM items WHERE run_id = ? {'AND ended_at IS NULL' if open_only else ''} ORDER BY item_id", run_id)

    def events(self, run_id: str | None = None, item_id: str | None = None, after: int = 0, run_level: bool = False, limit: int = -1) -> list[dict]:
        """Events in order, each as the dictionary `emit` hands to listeners. With an item, `run_level` adds the run's own events."""
        where, args = "seq > ?", [after]
        if run_id is not None:
            where += " AND run_id = ?"
            args.append(run_id)
        if item_id is not None:
            where += " AND (item_id = ? OR item_id IS NULL)" if run_level else " AND item_id = ?"
            args.append(item_id)
        return [event_of(r) for r in self.q(f"SELECT * FROM events WHERE {where} ORDER BY seq LIMIT ?", *args, limit)]

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, *args):
        return self.db.execute(sql, args).fetchone()

    # ---- events: append-only, also mirrored into the item's own folder ----
    def emit(self, run_id: str, item_id: str | None, stage: str | None, type_: str, folder: Path | None = None, **data) -> None:
        ts = time.time()
        seq = self.db.execute("INSERT INTO events (ts, run_id, item_id, stage, type, data) VALUES (?,?,?,?,?,?)",
                              (ts, run_id, item_id, stage, type_, json.dumps(data))).lastrowid
        event = event_of({"seq": seq, "ts": ts, "run_id": run_id, "item_id": item_id, "stage": stage, "type": type_, "data": None}, data)
        if folder is not None:
            with (folder / EVENTS).open("a") as f:
                f.write(json.dumps({k: v for k, v in event.items() if k not in ("run", "item")}) + "\n")
        for listener in self.listeners:
            listener(event)

    # ---- runs and items ----
    def add_run(self, run_id, flow, source, fetch_id, snapshot, trial, folder) -> None:
        self.db.execute("INSERT INTO runs (id, flow, source, fetch_id, snapshot, trial, folder, started, beat) VALUES (?,?,?,?,?,?,?,?,?)",
                        (run_id, flow, source, fetch_id, snapshot, int(trial), self._rel(folder), now(), time.time()))

    def _rel(self, folder: Path) -> str:
        return str(Path(folder).resolve().relative_to(self.root))

    def beat(self, run_id) -> None:
        self.db.execute("UPDATE runs SET beat = ? WHERE id = ? AND ended IS NULL", (time.time(), run_id))

    def cancel_requested(self, run_id) -> bool:
        return bool(self.one("SELECT cancel_requested c FROM runs WHERE id = ?", run_id)["c"])

    def last_try(self, run_id, item_id) -> tuple[dict | None, int]:
        """(an item's latest try, how often that stage was rejected): the two facts that place it on its flow's graph."""
        last = self.one("SELECT stage, n, outcome FROM attempts WHERE run_id = ? AND item_id = ? ORDER BY id DESC LIMIT 1", run_id, item_id)
        if last is None:
            return None, 0
        rejected = self.one("SELECT COUNT(*) c FROM attempts WHERE run_id = ? AND item_id = ? AND stage = ? AND outcome = 'rejected'", run_id, item_id, last["stage"])["c"]
        return dict(last), rejected

    def end_run(self, run_id) -> None:
        self.db.execute("UPDATE runs SET ended = ? WHERE id = ? AND ended IS NULL", (now(), run_id))

    def add_item(self, run_id, flow, item_id, folder, payload: dict) -> None:
        self.db.execute("INSERT INTO items (run_id, flow, item_id, folder, payload) VALUES (?,?,?,?,?)",
                        (run_id, flow, item_id, self._rel(folder), json.dumps(payload)))

    def claim_item(self, run_id, item_id) -> bool:
        """One conditional update: whoever changes the row has the item."""
        return self.db.execute("UPDATE items SET claimed_at = ? WHERE run_id = ? AND item_id = ? AND claimed_at IS NULL AND ended_at IS NULL",
                               (now(), run_id, item_id)).rowcount == 1

    def end_item(self, run_id, item_id, ended_as, reason="") -> bool:
        """End an item and say so, once: the row is changed by one conditional update and the event follows it."""
        won = self.db.execute("UPDATE items SET ended_at = ?, ended_as = ?, reason = ? WHERE run_id = ? AND item_id = ? AND ended_at IS NULL",
                              (now(), ended_as, reason, run_id, item_id)).rowcount == 1
        if won:
            folder = self.path(self.item(run_id, item_id))
            self.emit(run_id, item_id, None, "item_ended", folder=folder if folder.is_dir() else None, ended_as=ended_as, reason=reason)
        return won

    # ---- attempts and verdicts ----
    def start_attempt(self, run_id, item_id, stage, n, by) -> int:
        return self.db.execute("INSERT INTO attempts (run_id, item_id, stage, n, by, started) VALUES (?,?,?,?,?,?)",
                               (run_id, item_id, stage, n, by, time.time())).lastrowid

    def end_attempt(self, attempt_id, outcome, stats: dict | None = None) -> None:
        s = stats or {}
        self.db.execute("""UPDATE attempts SET ended = ?, outcome = ?, model_calls = ?, tool_calls = ?, tokens_in = ?, tokens_out = ?,
                           cost_usd = ? WHERE id = ? AND ended IS NULL""",
                        (time.time(), outcome, s.get("model_calls", 0), s.get("tool_calls", 0), s.get("tokens_in", 0),
                         s.get("tokens_out", 0), s.get("cost_usd"), attempt_id))

    def close_open_attempts(self, run_id, outcome, item_id=None) -> int:
        """Ends tries that never ended (and tries whose validation never finished) with the given outcome.
        A conditional update: a try that did end keeps its own outcome."""
        where, args = "run_id = ?", [run_id]
        if item_id is not None:
            where += " AND item_id = ?"
            args.append(item_id)
        return self.db.execute(f"""UPDATE attempts SET ended = COALESCE(ended, ?), outcome = ?
                                   WHERE {where} AND (ended IS NULL OR outcome = 'done')
                                     AND item_id IN (SELECT item_id FROM items WHERE run_id = attempts.run_id AND ended_at IS NULL)""",
                               (time.time(), outcome, *args)).rowcount

    def set_attempt_outcome(self, attempt_id, outcome) -> None:
        self.db.execute("UPDATE attempts SET outcome = ? WHERE id = ?", (outcome, attempt_id))

    def add_verdict(self, run_id, item_id, stage, n, v: dict) -> None:
        self.db.execute("INSERT INTO verdicts (run_id, item_id, stage, n, by, passed, reason, tokens_in, tokens_out, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (run_id, item_id, stage, n, v["by"], int(v["passed"]), v.get("reason", ""), v.get("tokens_in", 0), v.get("tokens_out", 0), v.get("cost_usd")))

    # ---- totals: unknown is not zero ----
    def usage(self, run_id, item_id=None, stage=None) -> dict:
        where, args = "run_id = ?", [run_id]
        for col, val in (("item_id", item_id), ("stage", stage)):
            if val is not None:
                where += f" AND {col} = ?"
                args.append(val)
        a = self.one(f"""SELECT COALESCE(SUM(tool_calls),0) tools, COALESCE(SUM(tokens_in+tokens_out),0) tokens, COALESCE(SUM(cost_usd),0) cost,
                         COALESCE(SUM(model_calls),0) calls,
                         SUM(CASE WHEN (model_calls > 0 AND cost_usd IS NULL)
                                    OR (by != 'script' AND model_calls = 0 AND outcome IN ('interrupted', 'timed_out', 'error', 'cancelled')) THEN 1 ELSE 0 END) unknown,
                         COALESCE(SUM(ended - started),0) seconds FROM attempts WHERE {where}""", *args)
        v = self.one(f"""SELECT COALESCE(SUM(tokens_in+tokens_out),0) tokens, COALESCE(SUM(cost_usd),0) cost,
                         SUM(CASE WHEN tokens_in > 0 AND cost_usd IS NULL THEN 1 ELSE 0 END) unknown FROM verdicts WHERE {where}""", *args)
        return {"tool_calls": a["tools"], "tokens": a["tokens"] + v["tokens"], "cost_usd": a["cost"] + v["cost"],
                "cost_known": not (a["unknown"] or v["unknown"]), "has_model_calls": bool(a["calls"] or v["tokens"] or a["unknown"]), "seconds": a["seconds"]}
