"""Usage rules of the sites and APIs Titan talks to, enforced in code.

Each provider's rules live in definitions/policies/*.json: the numbers Titan enforces and, for every rule, the
exact words of the provider's own page that state it (re-checked by check_policies.py). Every outbound request
path (page fetcher, connector caller, browser, site discovery) calls before_request()/after_response() here,
so no tool and no model can forget a rule.

The request budget and any pause after a block are kept in a small SQLite file, so the CLI, the API server and
a running research job all draw from one shared budget.
"""

import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import config
from tools.archive import USER_AGENT   # the exact header every request carries

POLICY_DIR = config.PROJECT_ROOT / "definitions" / "policies"
STATE_DB = config.DATA_DIR / "policy_state.db"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
MAX_WAIT_SECONDS = 10.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (policy TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS requests_by_policy ON requests (policy, ts);
CREATE TABLE IF NOT EXISTS blocks (policy TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT NOT NULL);
"""


class PolicyError(ValueError):
    """A request was refused because it would break a provider's usage rules."""


@dataclass(frozen=True)
class Policy:
    id: str
    name: str
    hosts: tuple[str, ...]
    enforced: dict
    rules: tuple[dict, ...]

    def covers(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return any(host == h or host.endswith("." + h) for h in self.hosts)


def load_policies(directory: Path = POLICY_DIR) -> list[Policy]:
    out = []
    for f in sorted(directory.glob("*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        for key in ("max_requests_per_second", "cooldown_minutes_after_block", "block_statuses",
                    "require_contact_in_user_agent", "allow_crawling", "allow_browser_rendering"):
            if key not in raw["enforced"]:
                raise ValueError(f"{f.name}: enforced.{key} is missing")
        for rule in raw["rules"]:
            if not rule.get("quote") or not rule.get("source_url", "").startswith("https://"):
                raise ValueError(f"{f.name}: rule {rule.get('id')} needs a quote and an https source_url")
        out.append(Policy(raw["id"], raw["name"], tuple(h.lower() for h in raw["hosts"]), raw["enforced"], tuple(raw["rules"])))
    return out


POLICIES: list[Policy] = load_policies()


def policy_for(url_or_host: str) -> Policy | None:
    host = urlparse(url_or_host).hostname if "/" in url_or_host else url_or_host
    return next((p for p in POLICIES if host and p.covers(host)), None)


class Limiter:
    """Sliding one-second window plus a pause switch, shared by every process through one SQLite file."""

    def __init__(self, path=None, clock=time.time, sleep=time.sleep):
        self.path, self.clock, self.sleep = str(path or STATE_DB), clock, sleep
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(SCHEMA)

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    def acquire(self, policy: Policy, max_wait: float = MAX_WAIT_SECONDS) -> None:
        """Take one request slot, waiting briefly if the second is full. Raises if paused or the wait is too long."""
        limit = int(policy.enforced["max_requests_per_second"])
        waited = 0.0
        while True:
            now = self.clock()
            with closing(self._db()) as db:
                db.execute("BEGIN IMMEDIATE")
                paused = db.execute("SELECT until, reason FROM blocks WHERE policy = ?", (policy.id,)).fetchone()
                if paused and paused[0] > now:
                    db.execute("ROLLBACK")
                    minutes = max(1, round((paused[0] - now) / 60))
                    raise PolicyError(f"{policy.name}: requests are paused for about {minutes} more minute(s) after "
                                      f"{paused[1]}. The site's own rules ask for a wait before trying again.")
                db.execute("DELETE FROM requests WHERE policy = ? AND ts <= ?", (policy.id, now - 1.0))
                count, oldest = db.execute("SELECT COUNT(*), MIN(ts) FROM requests WHERE policy = ?", (policy.id,)).fetchone()
                if count < limit:
                    db.execute("INSERT INTO requests VALUES (?, ?)", (policy.id, now))
                    db.execute("COMMIT")
                    return
                db.execute("ROLLBACK")
            pause = max(oldest + 1.0 - now, 0.01)
            if waited + pause > max_wait:
                raise PolicyError(f"{policy.name}: the request budget ({limit} per second) is exhausted; try again shortly")
            self.sleep(pause)
            waited += pause

    def block(self, policy: Policy, reason: str) -> None:
        until = self.clock() + float(policy.enforced["cooldown_minutes_after_block"]) * 60
        with closing(self._db()) as db:
            db.execute("INSERT OR REPLACE INTO blocks VALUES (?, ?, ?)", (policy.id, until, reason))

    def clear_block(self, policy_id: str) -> None:
        with closing(self._db()) as db:
            db.execute("DELETE FROM blocks WHERE policy = ?", (policy_id,))


_limiter: Limiter | None = None


def limiter() -> Limiter:
    global _limiter
    if _limiter is None:
        _limiter = Limiter()
    return _limiter


def before_request(url: str, kind: str = "fetch") -> None:
    """Call before every outbound request. kind: fetch | crawl | browser. No-op for sites without a policy."""
    policy = policy_for(url)
    if policy is None:
        return
    e = policy.enforced
    if kind == "crawl" and not e["allow_crawling"]:
        raise PolicyError(f"{policy.name} does not allow crawling by automated tools; fetch named documents "
                          "or use its data APIs instead")
    if kind == "browser" and not e["allow_browser_rendering"]:
        raise PolicyError(f"{policy.name}: browser rendering is not allowed (it loads many resources per page); "
                          "use fetch_url or the data APIs")
    if e["require_contact_in_user_agent"] and not EMAIL.search(USER_AGENT):
        raise PolicyError(f"{policy.name} requires every automated request to declare a contact email; "
                          "set TITAN_CONTACT in .env to an email address")
    limiter().acquire(policy)


def after_response(url: str, status: int) -> None:
    """Call with every response status. A refusal from the site pauses all requests to it."""
    policy = policy_for(url)
    if policy is not None and status in policy.enforced["block_statuses"]:
        limiter().block(policy, f"HTTP {status} from {urlparse(url).hostname}")


def manifest_problems(host: str, auth_type: str, requests_per_minute: int) -> list[str]:
    """Rules a connector manifest must satisfy before it can even be proposed."""
    policy = policy_for(host)
    if policy is None:
        return []
    e, problems = policy.enforced, []
    if not e.get("allow_api_key", True) and auth_type != "none":
        problems.append(f"{policy.name} needs no API key, so the connector must use auth type none (it declares {auth_type})")
    allowed = int(e["max_requests_per_second"]) * 60
    if requests_per_minute > allowed:
        problems.append(f"{policy.name} allows at most {e['max_requests_per_second']} requests a second "
                        f"({allowed} a minute); the manifest asks for {requests_per_minute} a minute")
    return problems


def request_headers(url: str) -> dict[str, str]:
    """Headers the provider asks for, beyond the User-Agent that every request already carries."""
    policy = policy_for(url)
    encoding = policy.enforced.get("accept_encoding") if policy else None
    return {"Accept-Encoding": encoding} if encoding else {}
