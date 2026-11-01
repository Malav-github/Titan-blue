"""Merge ranked lists from several providers: dedupe by normalized URL, score by reciprocal rank fusion."""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

RRF_K = 60  # standard constant; damps the gap between rank 1 and rank 2
TRACKING = ("utm_", "fbclid", "gclid", "mc_", "ref_")


def normalize_url(url: str) -> str:
    p = urlparse(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(TRACKING)])
    scheme = "https" if p.scheme in ("http", "https") else p.scheme
    return urlunparse((scheme, host + port, p.path.rstrip("/") or "/", "", query, ""))


def fuse(ranked_lists: dict[str, list]) -> list[dict]:
    """ranked_lists maps provider -> hits in rank order. A URL several providers agree on rises."""
    merged: dict[str, dict] = {}
    for provider, hits in ranked_lists.items():
        for rank, hit in enumerate(hits, start=1):
            key = normalize_url(hit.url)
            entry = merged.setdefault(key, {
                "url": hit.url, "title": hit.title, "snippet": hit.snippet,
                "published": hit.published, "score": 0.0, "found_by": [],
            })
            entry["score"] += 1.0 / (RRF_K + rank)
            entry["found_by"].append({"provider": provider, "engine": hit.engine, "rank": rank})
            if len(hit.snippet) > len(entry["snippet"]):  # keep the richer description
                entry["snippet"] = hit.snippet
            entry["published"] = entry["published"] or hit.published
    ordered = sorted(merged.values(), key=lambda e: e["score"], reverse=True)
    for e in ordered:
        e["score"] = round(e["score"], 5)
    return ordered
