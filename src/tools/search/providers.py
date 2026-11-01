"""Search providers. Each returns ranked hits plus a health report; none of them is trusted as evidence."""

import time
from dataclasses import dataclass, field

import httpx

from config import BRAVE_API_KEY, SEARCH_TIMEOUT, SEARXNG_URL
from tools.archive import USER_AGENT

# recency -> each provider's own parameter
SEARXNG_RANGE = {"day": "day", "week": "week", "month": "month", "year": "year"}
BRAVE_FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}


@dataclass
class Hit:
    url: str
    title: str
    snippet: str
    engine: str  # provider, or provider/engine for metasearch
    published: str | None = None


@dataclass
class Report:
    """What one provider did for one query. Failures are data, never silently dropped."""
    provider: str
    ok: bool
    hits: list[Hit] = field(default_factory=list)
    error: str | None = None
    degraded: list[str] = field(default_factory=list)  # upstream engines that failed inside a metasearch
    latency_ms: int = 0


def _timed(provider: str, fn) -> Report:
    start = time.perf_counter()
    try:
        report = fn()
    except Exception as exc:  # network errors, bad status, malformed JSON
        report = Report(provider, ok=False, error=f"{type(exc).__name__}: {exc}")
    report.latency_ms = int((time.perf_counter() - start) * 1000)
    return report


def searxng(query: str, limit: int, recency: str | None = None) -> Report:
    def run() -> Report:
        params = {"q": query, "format": "json", "pageno": 1}
        if recency in SEARXNG_RANGE:
            params["time_range"] = SEARXNG_RANGE[recency]
        resp = httpx.get(f"{SEARXNG_URL}/search", params=params, timeout=SEARCH_TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        hits = [
            Hit(r["url"], r.get("title", ""), r.get("content", ""),
                f"searxng/{'+'.join(r.get('engines') or [r.get('engine', '?')])}",
                r.get("publishedDate"))
            for r in data.get("results", []) if r.get("url")
        ][:limit]
        degraded = [f"{engine}: {reason}" for engine, reason in data.get("unresponsive_engines", [])]
        return Report("searxng", ok=True, hits=hits, degraded=degraded)

    return _timed("searxng", run)


def brave(query: str, limit: int, recency: str | None = None) -> Report:
    def run() -> Report:
        if not BRAVE_API_KEY:
            raise RuntimeError("BRAVE_API_KEY is not set")
        params = {"q": query, "count": min(limit, 20)}
        if recency in BRAVE_FRESHNESS:
            params["freshness"] = BRAVE_FRESHNESS[recency]
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search", params=params, timeout=SEARCH_TIMEOUT,
            headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"},
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        hits = [
            Hit(r["url"], r.get("title", ""), r.get("description", ""), "brave", r.get("page_age"))
            for r in results if r.get("url")
        ][:limit]
        return Report("brave", ok=True, hits=hits)

    return _timed("brave", run)


def brave_context(query: str, limit: int, recency: str | None = None) -> Report:
    """Brave LLM Context: pages with pre-extracted passages. Passages help triage; they are not evidence."""
    def run() -> Report:
        if not BRAVE_API_KEY:
            raise RuntimeError("BRAVE_API_KEY is not set")
        params = {"q": query}
        if recency in BRAVE_FRESHNESS:
            params["freshness"] = BRAVE_FRESHNESS[recency]
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/llm/context", params=params, timeout=SEARCH_TIMEOUT,
            headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        sources = data.get("sources", {})
        hits = []
        for g in data.get("grounding", {}).get("generic", []):
            if not g.get("url"):
                continue
            age = (sources.get(g["url"], {}).get("age") or [None, None])
            hits.append(Hit(g["url"], g.get("title", ""), " … ".join(g.get("snippets", []))[:SNIPPET_CHARS],
                            "brave_context", age[1] if len(age) > 1 else None))
        return Report("brave_context", ok=True, hits=hits[:limit])

    return _timed("brave_context", run)


SNIPPET_CHARS = 2000
PROVIDERS = {"searxng": searxng, "brave": brave, "brave_context": brave_context}
