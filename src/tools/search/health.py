"""Search engine health, kept across runs and processes (data/search_health.json).

SearXNG scrapes other search engines, and those engines push back: in 220 searches (1 to 6 Nov 2026) DuckDuckGo answered with
a CAPTCHA 109 times and SearXNG's Brave engine was rate-limited 133 times. Hitting a blocked engine again on the next query
only gets it blocked for longer, so an engine that reports a CAPTCHA, a rate limit or a timeout is rested for a while and
left out of SearXNG queries until then. When SearXNG itself does not answer (its container or WSL stopped), it is started
again, at most once every few minutes.
"""

import json
import re
import subprocess
import threading
import time

import httpx

from config import DATA_DIR, SEARXNG_URL

PATH = DATA_DIR / "search_health.json"
REST_MINUTES = [(re.compile(r"captcha|suspended|too many requests|access denied|403|429", re.I), 30),
                (re.compile(r"timeout|timed out", re.I), 10)]
DEFAULT_REST = 5
RESTART_EVERY_SECONDS = 300
CONTAINER = "titan-searxng"
_lock = threading.Lock()
_engines_cache: dict = {}


def _load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"resting": {}, "last_restart": 0}


def _save(state: dict) -> None:
    try:
        PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")
    except OSError:
        pass


def rest(engine: str, reason: str, now: float | None = None) -> float:
    """Rest an engine after it failed; returns the time it may be used again."""
    now = now or time.time()
    minutes = next((m for pattern, m in REST_MINUTES if pattern.search(reason)), DEFAULT_REST)
    with _lock:
        state = _load()
        until = max(state["resting"].get(engine, {}).get("until", 0), now + minutes * 60)
        state["resting"][engine] = {"until": until, "reason": reason[:120], "since": now}
        _save(state)
    return until


def resting(now: float | None = None) -> dict[str, dict]:
    now = now or time.time()
    return {e: r for e, r in _load()["resting"].items() if r["until"] > now}


def general_engines(base: str = SEARXNG_URL, ttl: float = 3600) -> list[str] | None:
    """The engines SearXNG has enabled for general web search (from its /config), or None if it cannot say."""
    hit = _engines_cache.get(base)
    if hit and hit[0] > time.time() - ttl:
        return hit[1]
    try:
        cfg = httpx.get(f"{base}/config", timeout=5).json()
        names = [e["name"] for e in cfg.get("engines", []) if e.get("enabled") and "general" in (e.get("categories") or [])]
    except Exception:
        return None
    _engines_cache[base] = (time.time(), names)
    return names


def engines_to_use(base: str = SEARXNG_URL) -> list[str] | None:
    """The general engines minus those resting; None means 'let SearXNG choose' (nothing is resting, or the list is unknown)."""
    tired = resting()
    if not tired:
        return None
    names = general_engines(base)
    if not names:
        return None
    usable = [n for n in names if n not in tired]
    return usable or None


def restart_searxng(run=subprocess.run, now: float | None = None) -> str | None:
    """Start SearXNG's container again if it is time to try. Returns what happened, or None if it was tried too recently."""
    now = now or time.time()
    with _lock:
        state = _load()
        if now - state.get("last_restart", 0) < RESTART_EVERY_SECONDS:
            return None
        state["last_restart"] = now
        _save(state)
    try:
        out = run(["wsl", "-e", "docker", "start", CONTAINER], text=True, capture_output=True, timeout=40)
    except Exception as exc:
        return f"could not start SearXNG: {type(exc).__name__}: {exc}"
    if out.returncode != 0:
        return f"could not start SearXNG: {(out.stderr or out.stdout).strip()[:160]}"
    for _ in range(20):                                   # give it a moment to answer
        try:
            httpx.get(f"{SEARXNG_URL}/healthz", timeout=2)
            return "SearXNG was not answering and was started again"
        except Exception:
            time.sleep(1)
    return "SearXNG was started again but is not answering yet"
