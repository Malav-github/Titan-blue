"""Evidence store: sources, entities and claims, append-only, with quotes verified by code.

Rules this module enforces:
- A claim is stored only if its quote appears word for word in the stored text of an archived
  page. A model never decides that; `verify_quote` does. Failed attempts go to `rejected_claims`.
- Nothing is edited or deleted (SQLite triggers abort UPDATE and DELETE). Corrections are new
  rows: retractions, supersessions and contradictions point at the claims they concern.
- Files are checked against their SHA-256 before use, so a changed archive file is detected.
"""

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from difflib import SequenceMatcher
from pathlib import Path

from config import EVIDENCE_DB_PATH
from tools.archive import archive_bytes, utc_now
from tools.primary import host_of, is_official
from tools.web import EXTRACTOR, extract_text

UPLOAD_SCHEME = "upload://"   # URL given to sources that came from a user-uploaded file
MIN_QUOTE_CHARS = 20
HINT_CONTEXT = 60

TABLES = ["sources", "fetches", "entities", "claims", "rejected_claims", "claim_events", "contradictions", "source_dates",
          "support_checks"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_sha256 TEXT PRIMARY KEY,       -- hash of the raw archived bytes
    url TEXT NOT NULL, final_url TEXT NOT NULL, content_type TEXT, method TEXT,
    archive_path TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,            -- hash of the extracted text that quotes are checked against
    text_path TEXT NOT NULL,
    extractor TEXT NOT NULL,
    first_fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fetches (       -- every fetch, so changes to a page over time stay visible
    fetch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_sha256 TEXT NOT NULL REFERENCES sources(source_sha256),
    url TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, kind TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',       -- official domain, '' if unknown
    domain_status TEXT,                    -- confirmed | ambiguous | needs_review | NULL
    resolution_evidence TEXT,              -- JSON
    created_at TEXT NOT NULL,
    UNIQUE (name, kind, domain)
);
CREATE TABLE IF NOT EXISTS claims (
    claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES entities(entity_id),
    predicate TEXT NOT NULL,               -- e.g. pricing.starter_monthly_usd
    value TEXT NOT NULL,
    claim_text TEXT NOT NULL,              -- the claim as a sentence
    source_sha256 TEXT NOT NULL REFERENCES sources(source_sha256),
    source_tier TEXT NOT NULL,             -- primary (company-owned domain) | secondary
    quote TEXT NOT NULL,                   -- verbatim from the source text
    quote_start INTEGER NOT NULL, quote_end INTEGER NOT NULL,   -- offsets in the normalized text
    text_sha256 TEXT NOT NULL,             -- the exact text the quote was verified against
    as_of TEXT,                            -- when the fact held, if the source says
    created_at TEXT NOT NULL, created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rejected_claims (   -- what the model tried that code refused
    rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at TEXT NOT NULL, created_by TEXT NOT NULL,
    entity_name TEXT, predicate TEXT, value TEXT, claim_text TEXT,
    source_sha256 TEXT, quote TEXT, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(claim_id),
    event TEXT NOT NULL CHECK (event IN ('retracted', 'superseded')),
    related_claim_id INTEGER REFERENCES claims(claim_id),   -- the claim that supersedes
    reason TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contradictions (
    contradiction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_a INTEGER NOT NULL REFERENCES claims(claim_id),
    claim_b INTEGER NOT NULL REFERENCES claims(claim_id),
    note TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL,
    CHECK (claim_a < claim_b), UNIQUE (claim_a, claim_b)
);
CREATE TABLE IF NOT EXISTS support_checks (   -- does the quote actually back the claim? (a verdict, not a fact)
    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(claim_id),
    verdict TEXT NOT NULL CHECK (verdict IN ('supported', 'partially_supported', 'not_supported')),
    reasoning TEXT NOT NULL,
    unsupported_parts TEXT NOT NULL,       -- JSON list
    missing_tokens TEXT NOT NULL,          -- JSON list: numbers/identifiers in the claim absent from the quote
    checker TEXT NOT NULL, prompt_version TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_dates (   -- the dates a page carried when it was fetched, each with where it came from
    date_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_sha256 TEXT NOT NULL REFERENCES sources(source_sha256),
    kind TEXT NOT NULL,                    -- published | modified | created | event_start | event_end | time_element | url_date | search_result | server_date
    value TEXT NOT NULL,                   -- YYYY, YYYY-MM, YYYY-MM-DD or an ISO datetime
    precision TEXT NOT NULL CHECK (precision IN ('year', 'month', 'day', 'datetime')),
    raw TEXT NOT NULL,                     -- the text it was read from
    origin TEXT NOT NULL,                  -- meta:..., jsonld:..., text:byline, http:..., url, search
    relative INTEGER NOT NULL DEFAULT 0,   -- 1 if it was worked out from words like "3 days ago"
    collected_at TEXT NOT NULL,
    UNIQUE (source_sha256, kind, value, origin)
);
CREATE VIEW IF NOT EXISTS current_claims AS
    SELECT * FROM claims WHERE claim_id NOT IN (SELECT claim_id FROM claim_events);
"""


def normalize(text: str) -> str:
    """Unicode NFC and collapsed whitespace. Case, punctuation and digits are left strictly alone."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def verify_quote(text: str, quote: str) -> dict:
    """Deterministic check that `quote` occurs verbatim in `text` (modulo whitespace)."""
    t, q = normalize(text), normalize(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return {"ok": False, "reason": f"quote shorter than {MIN_QUOTE_CHARS} characters"}
    start = t.find(q)
    if start >= 0:
        return {"ok": True, "start": start, "end": start + len(q)}
    # Not found: point at the closest passage so the caller can retry with an exact quote
    match = SequenceMatcher(None, t, q, autojunk=False).find_longest_match(0, len(t), 0, len(q))
    lo = max(0, match.a - HINT_CONTEXT)
    hint = t[lo:match.a + len(q) + HINT_CONTEXT] if match.size >= 8 else None
    return {"ok": False, "reason": "quote not found verbatim in the source text", "closest_passage": hint}


MIN_WIDEN_FROM = 8            # shorter quotes are too thin to widen; they stay rejected
MAX_WIDEN_CHARS = 60          # never widen a quote into more than this much surrounding text


def widen_quote(text: str, quote: str, min_chars: int = MIN_QUOTE_CHARS) -> str | None:
    """A quote that is verbatim but under the minimum length (`"tickers":["AAPL"]` in an API response, a key and its
    value) is extended with the text next to it in the source, so the quote stays word for word and gains context.
    Only when the short quote occurs exactly once in the source: if it could point at several places, or at nothing,
    it stays rejected. The extended quote still has to pass the support check like any other.
    Returns the longer quote, or None when it cannot be widened safely."""
    t, q = normalize(text), normalize(quote)
    if len(q) >= min_chars or len(q) < MIN_WIDEN_FROM or t.count(q) != 1:
        return None
    start = t.index(q)
    end = start + len(q)
    stop = " ,;]}"
    while end - start < min_chars and end < len(t):
        end += 1
    while end < len(t) and t[end - 1] not in stop and end - start < MAX_WIDEN_CHARS:
        end += 1                                          # finish the word or field rather than cut it in half
    while end - start < min_chars and start > 0:
        start -= 1
    wider = t[start:end].strip()
    return wider if min_chars <= len(wider) <= MAX_WIDEN_CHARS and q in wider else None


def _sha256_file(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class EvidenceStore:
    def __init__(self, path=EVIDENCE_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            for table in TABLES:
                for op in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} "
                        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
                    )
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    # -- sources ----------------------------------------------------------------------------

    def ingest(self, fetched: dict) -> str:
        """Register a fetch result (from fetch_url or render_url) as a source. Returns source_sha256."""
        body = Path(fetched["archive_path"]).read_bytes()
        sha = hashlib.sha256(body).hexdigest()
        if sha != fetched["sha256"]:
            raise ValueError(f"Archive file does not match its recorded hash: {fetched['archive_path']}")
        _, text = extract_text(body, fetched.get("content_type", ""), fetched.get("encoding", "utf-8"),
                               fetched.get("final_url", fetched["url"]))
        text_sha, text_path = archive_bytes(text.encode("utf-8"), ".txt")
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sha, fetched["url"], fetched.get("final_url", fetched["url"]), fetched.get("content_type"),
                 fetched.get("method"), fetched["archive_path"], text_sha, text_path, EXTRACTOR,
                 fetched["fetched_at"]),
            )
            db.execute("INSERT INTO fetches (source_sha256, url, fetched_at) VALUES (?,?,?)",
                       (sha, fetched["url"], fetched["fetched_at"]))
            self._insert_dates(db, sha, fetched.get("dates") or [])
        return sha

    @staticmethod
    def _insert_dates(db, sha: str, dates: list) -> int:
        added = 0
        for d in dates:
            d = d if isinstance(d, dict) else d.as_dict()
            cur = db.execute(
                "INSERT OR IGNORE INTO source_dates (source_sha256, kind, value, precision, raw, origin, relative, collected_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (sha, d["kind"], d["value"], d["precision"], d["raw"], d["origin"], int(bool(d.get("relative"))), utc_now()))
            added += cur.rowcount
        return added

    def record_source_dates(self, source_sha256: str, dates: list) -> int:
        """Add dates found for a source after it was ingested (for example the date a search engine showed for it).
        Existing rows are never changed; a date already recorded is skipped. Returns how many were new."""
        with closing(self._connect()) as db, db:
            return self._insert_dates(db, source_sha256, dates)

    def source_dates(self, source_sha256: str) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute(
                "SELECT kind, value, precision, raw, origin, relative FROM source_dates WHERE source_sha256 = ? ORDER BY date_id",
                (source_sha256,))]

    def get_source(self, source_sha256: str) -> sqlite3.Row | None:
        with closing(self._connect()) as db:
            return db.execute("SELECT * FROM sources WHERE source_sha256 = ?", (source_sha256,)).fetchone()

    # -- entities ---------------------------------------------------------------------------

    def add_entity(self, name: str, kind: str = "company", domain: str = "",
                   domain_status: str | None = None, evidence: list[str] | None = None) -> int:
        """Get or create. Entities are immutable: a different domain for the same name is a new row."""
        domain = host_of(domain) if "/" in domain else domain.lower().removeprefix("www.")  # URL -> host
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO entities (name, kind, domain, domain_status, resolution_evidence, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (name, kind, domain, domain_status, json.dumps(evidence or []), utc_now()),
            )
            return db.execute("SELECT entity_id FROM entities WHERE name=? AND kind=? AND domain=?",
                              (name, kind, domain)).fetchone()[0]

    def find_entity(self, name: str) -> sqlite3.Row | None:
        """Entity with this name (case-insensitive): a confirmed domain beats an unconfirmed one."""
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT * FROM entities WHERE lower(name)=lower(?)"
                " ORDER BY (domain_status = 'confirmed') DESC, entity_id DESC LIMIT 1", (name,)).fetchone()

    def register_resolution(self, resolution: dict) -> list[int]:
        """Record domain candidates from primary.resolve_domain. Only code, never a model, sets the status."""
        return [self.add_entity(resolution["company"], domain=c["domain"], domain_status=c["status"],
                                evidence=c["evidence"]) for c in resolution["candidates"]]

    # -- claims -----------------------------------------------------------------------------

    def add_claim(self, entity_id: int, predicate: str, value: str, claim_text: str,
                  source_sha256: str, quote: str, as_of: str | None = None,
                  created_by: str = "agent") -> dict:
        """Store a claim only if its quote verifies against the source. Returns {ok, claim_id | reason}."""
        def reject(reason: str, **extra) -> dict:
            with closing(self._connect()) as db, db:
                ent = db.execute("SELECT name FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
                db.execute(
                    "INSERT INTO rejected_claims (attempted_at, created_by, entity_name, predicate, value,"
                    " claim_text, source_sha256, quote, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                    (utc_now(), created_by, ent["name"] if ent else None, predicate, value, claim_text,
                     source_sha256, quote, reason),
                )
            return {"ok": False, "reason": reason, **extra}

        source = self.get_source(source_sha256)
        if source is None:
            return reject("unknown source: fetch the page first and use its source_id")
        try:
            self._check_source_files(source)
        except ValueError as exc:
            return reject(f"source integrity failure: {exc}")
        text = Path(source["text_path"]).read_text(encoding="utf-8")
        check = verify_quote(text, quote)
        if not check["ok"]:
            return reject(check["reason"], closest_passage=check.get("closest_passage"))
        if normalize(value) and normalize(value) not in normalize(quote) and normalize(value) not in normalize(claim_text):
            return reject("value appears in neither the quote nor the claim text")

        with closing(self._connect()) as db, db:
            ent = db.execute("SELECT * FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
            if ent is None:
                return {"ok": False, "reason": f"unknown entity_id {entity_id}"}
            known_domain = [ent["domain"]] if ent["domain"] and ent["domain_status"] == "confirmed" else []
            if source["final_url"].startswith(UPLOAD_SCHEME):
                tier = "user_provided"     # the user's own file: never counts as independent or primary evidence
            else:
                tier = "primary" if is_official(source["final_url"], known_domain) else "secondary"
            cur = db.execute(
                "INSERT INTO claims (entity_id, predicate, value, claim_text, source_sha256, source_tier, quote,"
                " quote_start, quote_end, text_sha256, as_of, created_at, created_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (entity_id, predicate, value, claim_text, source_sha256, tier, normalize(quote),
                 check["start"], check["end"], source["text_sha256"], as_of, utc_now(), created_by),
            )
        return {"ok": True, "claim_id": cur.lastrowid, "source_tier": tier}

    @staticmethod
    def _check_source_files(source: sqlite3.Row) -> None:
        for label, path, expected in (("raw archive", source["archive_path"], source["source_sha256"]),
                                      ("extracted text", source["text_path"], source["text_sha256"])):
            if not Path(path).exists():
                raise ValueError(f"{label} file is missing: {path}")
            if _sha256_file(path) != expected:
                raise ValueError(f"{label} file no longer matches its hash: {path}")

    def get_claim(self, claim_id: int) -> sqlite3.Row | None:
        with closing(self._connect()) as db:
            return db.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()

    def record_support_check(self, claim_id: int, verdict: str, reasoning: str, unsupported_parts: list[str],
                             missing_tokens: list[str], checker: str, prompt_version: str) -> int:
        with closing(self._connect()) as db, db:
            return db.execute(
                "INSERT INTO support_checks (claim_id, verdict, reasoning, unsupported_parts, missing_tokens,"
                " checker, prompt_version, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (claim_id, verdict, reasoning, json.dumps(unsupported_parts), json.dumps(missing_tokens),
                 checker, prompt_version, utc_now())).lastrowid

    def support_checks_for(self, claim_id: int) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM support_checks WHERE claim_id = ? ORDER BY check_id", (claim_id,))]

    def retract(self, claim_id: int, reason: str, created_by: str = "agent") -> None:
        self._event(claim_id, "retracted", None, reason, created_by)

    def supersede(self, old_claim_id: int, new_claim_id: int, reason: str, created_by: str = "agent") -> None:
        self._event(old_claim_id, "superseded", new_claim_id, reason, created_by)

    def _event(self, claim_id, event, related, reason, created_by) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO claim_events (claim_id, event, related_claim_id, reason, created_at, created_by)"
                       " VALUES (?,?,?,?,?,?)", (claim_id, event, related, reason, utc_now(), created_by))

    def flag_contradiction(self, claim_a: int, claim_b: int, note: str, created_by: str = "agent") -> None:
        a, b = sorted((claim_a, claim_b))
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO contradictions (claim_a, claim_b, note, created_at, created_by)"
                       " VALUES (?,?,?,?,?)", (a, b, note, utc_now(), created_by))

    def claims_for(self, name: str, include_inactive: bool = False) -> list[dict]:
        view = "claims" if include_inactive else "current_claims"
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT c.*, e.name AS entity, e.domain, s.final_url AS source_url FROM {view} c"
                " JOIN entities e USING (entity_id) JOIN sources s ON s.source_sha256 = c.source_sha256"
                " WHERE lower(e.name) = lower(?) ORDER BY c.claim_id", (name,)).fetchall()
        return [dict(r) for r in rows]

    def list_claims(self, entity: str | None = None, limit: int = 100, offset: int = 0, include_inactive: bool = False) -> list[dict]:
        """Newest first, each with its source and its latest support verdict (None if never checked)."""
        view = "claims" if include_inactive else "current_claims"
        where, args = ("WHERE lower(e.name) = lower(?)", [entity]) if entity else ("", [])
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT c.claim_id, c.predicate, c.value, c.claim_text, c.quote, c.source_tier, c.source_sha256, c.created_at,"
                " e.name AS entity, s.final_url AS source_url,"
                " (SELECT verdict FROM support_checks k WHERE k.claim_id = c.claim_id ORDER BY check_id DESC LIMIT 1) AS verdict"
                f" FROM {view} c JOIN entities e USING (entity_id) JOIN sources s ON s.source_sha256 = c.source_sha256"
                f" {where} ORDER BY c.claim_id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return [dict(r) for r in rows]

    def claim_detail(self, claim_id: int, context: int = 300) -> dict | None:
        """One claim with the text around its quote and every support check, so a reader can judge it."""
        claim = self.get_claim(claim_id)
        if claim is None:
            return None
        source = self.get_source(claim["source_sha256"])
        text = normalize(Path(source["text_path"]).read_text(encoding="utf-8"))
        with closing(self._connect()) as db:
            entity = db.execute("SELECT name, domain, domain_status FROM entities WHERE entity_id = ?", (claim["entity_id"],)).fetchone()
            events = db.execute("SELECT event, reason, created_at FROM claim_events WHERE claim_id = ?", (claim_id,)).fetchall()
        lo, hi = claim["quote_start"], claim["quote_end"]
        return {**{k: claim[k] for k in ("claim_id", "predicate", "value", "claim_text", "quote", "source_tier", "source_sha256",
                                         "text_sha256", "as_of", "created_at", "created_by")},
                "entity": dict(entity), "source_url": source["final_url"], "fetched_at": source["first_fetched_at"],
                "before": text[max(0, lo - context):lo], "after": text[hi:hi + context],
                "support_checks": self.support_checks_for(claim_id), "status": [dict(e) for e in events],
                "source_dates": self.source_dates(claim["source_sha256"])}

    def counts(self) -> dict:
        with closing(self._connect()) as db:
            return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}

    def audit(self) -> list[str]:
        """Re-verify everything stored. An empty list means every claim still checks out."""
        problems = []
        with closing(self._connect()) as db:
            sources = {r["source_sha256"]: r for r in db.execute("SELECT * FROM sources")}
            claims = db.execute("SELECT * FROM claims").fetchall()
        bad_sources = set()
        for sha, src in sources.items():
            try:
                self._check_source_files(src)
            except ValueError as exc:
                bad_sources.add(sha)
                problems.append(f"source {sha[:12]}: {exc}")
        for c in claims:
            if c["source_sha256"] in bad_sources:
                problems.append(f"claim {c['claim_id']}: its source failed integrity check")
                continue
            text = Path(sources[c["source_sha256"]]["text_path"]).read_text(encoding="utf-8")
            if not verify_quote(text, c["quote"])["ok"]:
                problems.append(f"claim {c['claim_id']}: quote no longer verifies")
        return problems
