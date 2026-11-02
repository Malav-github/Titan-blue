"""Append-only log of everything a research run does. The frontend and API read this; nothing edits it.

A run's status is never stored, only derived from its last status event, so history can't be rewritten.
"""

import json
import sqlite3
from contextlib import closing

from config import DATA_DIR
from tools.archive import utc_now

RUNS_DB_PATH = str(DATA_DIR / "runs.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    node TEXT NOT NULL,        -- graph node, or 'runner' for lifecycle events
    type TEXT NOT NULL,        -- e.g. plan_ready, source_fetched, claim_recorded, support_checked
    payload TEXT NOT NULL      -- JSON
);
CREATE INDEX IF NOT EXISTS run_events_by_run ON run_events (run_id, event_id);
CREATE TRIGGER IF NOT EXISTS run_events_no_update BEFORE UPDATE ON run_events
BEGIN SELECT RAISE(ABORT, 'run_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS run_events_no_delete BEFORE DELETE ON run_events
BEGIN SELECT RAISE(ABORT, 'run_events is append-only'); END;
"""

# Event type -> run status it implies. Other event types do not change the status.
STATUS_BY_EVENT = {
    "run_started": "running", "resumed": "running", "awaiting_approval": "awaiting_approval",
    "paused": "paused", "capability_needed": "needs_capability", "run_finished": "finished", "run_stopped": "stopped", "run_failed": "failed",
}


class RunStore:
    def __init__(self, path=RUNS_DB_PATH):
        self.path = path
        self.listener = None  # optional callable(event dict), e.g. a CLI printing progress live
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def emit(self, run_id: str, node: str, type_: str, payload: dict | None = None) -> int:
        ts = utc_now()
        with closing(self._connect()) as db, db:
            event_id = db.execute(
                "INSERT INTO run_events (run_id, ts, node, type, payload) VALUES (?,?,?,?,?)",
                (run_id, ts, node, type_, json.dumps(payload or {}, default=str)),
            ).lastrowid
        if self.listener:
            self.listener({"event_id": event_id, "run_id": run_id, "ts": ts, "node": node, "type": type_,
                           "payload": payload or {}})
        return event_id

    def events(self, run_id: str, after_id: int = 0) -> list[dict]:
        """Events in order. Pass the last seen event_id as `after_id` to tail a running run."""
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM run_events WHERE run_id = ? AND event_id > ? ORDER BY event_id",
                              (run_id, after_id)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def status(self, run_id: str) -> str | None:
        with closing(self._connect()) as db:
            marks = ",".join("?" * len(STATUS_BY_EVENT))
            row = db.execute(f"SELECT type FROM run_events WHERE run_id = ? AND type IN ({marks})"
                             " ORDER BY event_id DESC LIMIT 1", (run_id, *STATUS_BY_EVENT)).fetchone()
        return STATUS_BY_EVENT[row["type"]] if row else None

    def runs(self, limit: int = 50) -> list[dict]:
        """Newest runs first, with question and derived status."""
        with closing(self._connect()) as db:
            started = db.execute("SELECT run_id, ts, payload FROM run_events WHERE type = 'run_started'"
                                 " ORDER BY event_id DESC LIMIT ?", (limit,)).fetchall()
        return [{"run_id": r["run_id"], "started_at": r["ts"], "status": self.status(r["run_id"]),
                 "question": json.loads(r["payload"]).get("question")} for r in started]
