"""Append-only connector registry: manifests (versioned), lifecycle events, API call log, capability requests.

A manifest version is never edited. A connector becomes active only through events: proposed,
approved (by a human), tested_pass, activated. Disabling is another event. Which version is live
is derived from the event history.
"""

import json
import sqlite3
import time
import uuid
from contextlib import closing

from config import DATA_DIR
from connectors.manifest import Manifest
from tools.archive import utc_now

CONNECTORS_DB_PATH = str(DATA_DIR / "connectors.db")
TABLES = ["manifests", "connector_events", "api_calls", "capability_requests", "capability_events"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS manifests (
    connector_id TEXT NOT NULL, version INTEGER NOT NULL,
    manifest TEXT NOT NULL, proposed_at TEXT NOT NULL, proposed_by TEXT NOT NULL,
    PRIMARY KEY (connector_id, version)
);
CREATE TABLE IF NOT EXISTS connector_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    connector_id TEXT NOT NULL, version INTEGER,
    event TEXT NOT NULL CHECK (event IN ('proposed','approved','rejected','tested_pass','tested_fail','activated','disabled')),
    detail TEXT NOT NULL, at TEXT NOT NULL, by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_calls (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    connector_id TEXT NOT NULL, version INTEGER NOT NULL, endpoint TEXT NOT NULL, run_id TEXT,
    ts TEXT NOT NULL, ts_epoch REAL NOT NULL, status INTEGER, ok INTEGER NOT NULL, cost_usd REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS capability_requests (
    request_id TEXT PRIMARY KEY, run_id TEXT, need TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES capability_requests(request_id),
    event TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
);
"""

CAPABILITY_STATUS = {
    "requested": "requested", "researching": "researching", "awaiting_user": "awaiting_user",
    "integrated": "integrating", "tests_failed": "tests_failed", "ready": "ready", "resumed": "done",
    "cancelled": "cancelled", "failed": "failed",
}


class ConnectorRegistry:
    def __init__(self, path=CONNECTORS_DB_PATH):
        self.path = str(path)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            for table in TABLES:
                for op in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} "
                               f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END")
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # -- manifests and lifecycle ------------------------------------------------------------------

    def propose(self, manifest: Manifest, by: str = "capability-manager") -> int:
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT COALESCE(MAX(version), 0) FROM manifests WHERE connector_id = ?",
                             (manifest.connector_id,)).fetchone()
            version = row[0] + 1
            db.execute("INSERT INTO manifests VALUES (?,?,?,?,?)",
                       (manifest.connector_id, version, manifest.model_dump_json(by_alias=True), utc_now(), by))
        self.event(manifest.connector_id, version, "proposed", {}, by)
        return version

    def event(self, connector_id: str, version: int | None, event: str, detail: dict | None = None, by: str = "system") -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO connector_events (connector_id, version, event, detail, at, by) VALUES (?,?,?,?,?,?)",
                       (connector_id, version, event, json.dumps(detail or {}, default=str), utc_now(), by))

    def get_manifest(self, connector_id: str, version: int) -> Manifest:
        with closing(self._connect()) as db:
            row = db.execute("SELECT manifest FROM manifests WHERE connector_id = ? AND version = ?",
                             (connector_id, version)).fetchone()
        if row is None:
            raise KeyError(f"no manifest {connector_id} v{version}")
        return Manifest.model_validate_json(row["manifest"])

    def events(self, connector_id: str) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM connector_events WHERE connector_id = ? ORDER BY event_id", (connector_id,)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    def activate(self, connector_id: str, version: int, by: str = "capability-manager") -> None:
        """Make a version live. Refuses unless a human approved it and its tests passed."""
        seen = {e["event"] for e in self.events(connector_id) if e["version"] == version}
        missing = {"approved", "tested_pass"} - seen
        if missing:
            raise PermissionError(f"{connector_id} v{version} cannot be activated: missing {sorted(missing)}")
        self.event(connector_id, version, "activated", {}, by)

    def disable(self, connector_id: str, reason: str, by: str = "user") -> None:
        self.event(connector_id, None, "disabled", {"reason": reason}, by)

    def active(self, connector_id: str) -> tuple[int, Manifest] | None:
        live = None
        for e in self.events(connector_id):
            if e["event"] == "activated":
                live = e["version"]
            elif e["event"] == "disabled":
                live = None
        return (live, self.get_manifest(connector_id, live)) if live else None

    def list_active(self) -> list[tuple[int, Manifest]]:
        with closing(self._connect()) as db:
            ids = [r[0] for r in db.execute("SELECT DISTINCT connector_id FROM manifests ORDER BY connector_id")]
        return [a for a in (self.active(i) for i in ids) if a]

    # -- call log (also drives rate limits and budgets, so it survives restarts) ------------------

    def log_call(self, connector_id: str, version: int, endpoint: str, run_id: str | None, status: int | None,
                 ok: bool, cost_usd: float = 0.0) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO api_calls (connector_id, version, endpoint, run_id, ts, ts_epoch, status, ok, cost_usd)"
                       " VALUES (?,?,?,?,?,?,?,?,?)",
                       (connector_id, version, endpoint, run_id, utc_now(), time.time(), status, int(ok), cost_usd))

    def count_calls(self, connector_id: str, since_epoch: float = 0, run_id: str | None = None) -> int:
        sql, args = "SELECT COUNT(*) FROM api_calls WHERE connector_id = ? AND ts_epoch >= ?", [connector_id, since_epoch]
        if run_id:
            sql, args = sql + " AND run_id = ?", args + [run_id]
        with closing(self._connect()) as db:
            return db.execute(sql, args).fetchone()[0]

    def cost_since(self, connector_id: str, since_epoch: float) -> float:
        with closing(self._connect()) as db:
            return db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls WHERE connector_id = ? AND ts_epoch >= ?",
                              (connector_id, since_epoch)).fetchone()[0]

    # -- capability requests ----------------------------------------------------------------------

    def create_request(self, run_id: str | None, need: str, reason: str) -> str:
        request_id = uuid.uuid4().hex[:12]
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO capability_requests VALUES (?,?,?,?,?)", (request_id, run_id, need, reason, utc_now()))
        self.request_event(request_id, "requested", {})
        return request_id

    def request_event(self, request_id: str, event: str, detail: dict | None = None) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO capability_events (request_id, event, detail, at) VALUES (?,?,?,?)",
                       (request_id, event, json.dumps(detail or {}, default=str), utc_now()))

    def get_request(self, request_id: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM capability_requests WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                return None
            events = db.execute("SELECT event, detail, at FROM capability_events WHERE request_id = ? ORDER BY event_id",
                                (request_id,)).fetchall()
        last = events[-1]["event"] if events else "requested"
        return {**dict(row), "status": CAPABILITY_STATUS.get(last, last),
                "events": [{**dict(e), "detail": json.loads(e["detail"])} for e in events]}

    def requests(self, limit: int = 30) -> list[dict]:
        with closing(self._connect()) as db:
            ids = [r[0] for r in db.execute("SELECT request_id FROM capability_requests ORDER BY created_at DESC LIMIT ?", (limit,))]
        return [self.get_request(i) for i in ids]
