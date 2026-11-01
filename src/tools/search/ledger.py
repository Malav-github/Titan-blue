"""Append-only record of every search: what was asked, who answered, who failed, what came back.

Lets "nothing found" be reported as "searched these sources, got these results, none matched".
SQLite triggers reject UPDATE and DELETE, so history cannot be rewritten by accident.
"""

import json
import sqlite3
import uuid

from config import DATA_DIR
from tools.archive import utc_now

LEDGER_PATH = DATA_DIR / "search_ledger.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    search_id TEXT PRIMARY KEY,
    searched_at TEXT NOT NULL,
    query TEXT NOT NULL,
    recency TEXT,
    providers TEXT NOT NULL,       -- JSON: per-provider ok / error / degraded engines / counts / latency
    results TEXT NOT NULL          -- JSON: the fused result list exactly as returned
);
CREATE TRIGGER IF NOT EXISTS searches_no_update BEFORE UPDATE ON searches
BEGIN SELECT RAISE(ABORT, 'search ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS searches_no_delete BEFORE DELETE ON searches
BEGIN SELECT RAISE(ABORT, 'search ledger is append-only'); END;
"""


def record(query: str, recency: str | None, providers: list[dict], results: list[dict],
           path=LEDGER_PATH) -> str:
    search_id = uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        with db:  # commits on success; the connection itself must still be closed
            db.executescript(SCHEMA)
            db.execute(
                "INSERT INTO searches VALUES (?, ?, ?, ?, ?, ?)",
                (search_id, utc_now(), query, recency, json.dumps(providers), json.dumps(results)),
            )
    finally:
        db.close()
    return search_id
