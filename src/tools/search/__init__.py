"""Search: fan a query out to the configured providers in parallel, fuse the results, log everything."""

from concurrent.futures import ThreadPoolExecutor

from config import SEARCH_PROVIDERS
from tools.search import ledger
from tools.search.fusion import fuse
from tools.search.providers import PROVIDERS, Report

RECENCY = ("day", "week", "month", "year")


def search(query: str, max_results: int = 10, recency: str | None = None,
           site: str | None = None, providers: list[str] | None = None,
           ledger_path=ledger.LEDGER_PATH) -> dict:
    """Results are leads, not evidence: fetch and archive a page before relying on it."""
    names = providers or SEARCH_PROVIDERS
    unknown = [n for n in names if n not in PROVIDERS]
    if unknown:
        raise ValueError(f"Unknown search provider(s): {', '.join(unknown)}")
    if recency and recency not in RECENCY:
        raise ValueError(f"recency must be one of {RECENCY}")

    full_query = f"{query} site:{site}" if site else query
    # Over-fetch from each provider so fusion has room to reorder
    per_provider = max(max_results * 2, 10)
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        futures = {n: pool.submit(PROVIDERS[n], full_query, per_provider, recency) for n in names}
        reports: dict[str, Report] = {n: f.result() for n, f in futures.items()}

    results = fuse({n: r.hits for n, r in reports.items() if r.ok})[:max_results]
    health = [
        {"provider": r.provider, "ok": r.ok, "result_count": len(r.hits), "error": r.error,
         "degraded_engines": r.degraded, "latency_ms": r.latency_ms}
        for r in reports.values()
    ]
    warnings = [f"{h['provider']} failed: {h['error']}" for h in health if not h["ok"]]
    warnings += [f"{h['provider']} degraded engine {d}" for h in health for d in h["degraded_engines"]]
    if not any(h["ok"] for h in health):
        warnings.append("ALL providers failed: an empty result here says nothing about the web")

    search_id = ledger.record(full_query, recency, health, results, path=ledger_path)
    return {"search_id": search_id, "query": full_query, "results": results,
            "providers": health, "warnings": warnings}
