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
    "queued": "queued", "run_started": "running", "resumed": "running", "awaiting_approval": "awaiting_approval",
    "paused": "paused", "capability_needed": "needs_capability", "source_needed": "needs_source", "run_finished": "finished", "run_stopped": "stopped", "run_failed": "failed",
    "pause_requested": "pausing", "stop_requested": "stopping",
}
CONTROL_EVENTS = ("pause_requested", "stop_requested", "resumed")


class RunStore:
    def __init__(self, path=RUNS_DB_PATH, *, readonly: bool = False):
        self.path = path
        self.readonly = readonly              # an archived run log: read, never written
        self.listener = None  # optional callable(event dict), e.g. a CLI printing progress live
        if readonly:
            return
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            from pathlib import Path
            db = sqlite3.connect(f"file:{Path(self.path).resolve().as_posix()}?mode=ro", uri=True, timeout=30)
        else:
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

    def requested(self, run_id: str) -> str | None:
        """'pause' or 'stop' when the person asked for it and the run has not been resumed since; else None. Read by the
        runner between steps and between model calls, so a run pauses or stops at the next safe point."""
        with closing(self._connect()) as db:
            row = db.execute("SELECT type FROM run_events WHERE run_id = ? AND type IN (?,?,?) ORDER BY event_id DESC LIMIT 1",
                             (run_id, *CONTROL_EVENTS)).fetchone()
        return {"pause_requested": "pause", "stop_requested": "stop"}.get(row["type"]) if row else None

    def status(self, run_id: str) -> str | None:
        with closing(self._connect()) as db:
            marks = ",".join("?" * len(STATUS_BY_EVENT))
            row = db.execute(f"SELECT type FROM run_events WHERE run_id = ? AND type IN ({marks})"
                             " ORDER BY event_id DESC LIMIT 1", (run_id, *STATUS_BY_EVENT)).fetchone()
        return STATUS_BY_EVENT[row["type"]] if row else None

    def runs(self, limit: int = 50) -> list[dict]:
        """Newest runs first, with question and derived status. A run that is only queued is listed too."""
        with closing(self._connect()) as db:
            rows = db.execute("SELECT run_id, ts, type, payload FROM run_events WHERE type IN ('queued', 'run_started')"
                              " ORDER BY event_id").fetchall()
        found: dict[str, dict] = {}
        for r in rows:
            payload = json.loads(r["payload"])
            entry = found.setdefault(r["run_id"], {"run_id": r["run_id"], "started_at": r["ts"], "question": None, "kind": None})
            entry["question"] = payload.get("question") or entry["question"]
            entry["kind"] = ("block" if "graph" in payload else "research") if r["type"] == "run_started" else payload.get("kind", entry["kind"])
        newest = list(found.values())[::-1][:limit]
        return [{**e, "status": self.status(e["run_id"])} for e in newest]
