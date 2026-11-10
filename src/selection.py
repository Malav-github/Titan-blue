"""Which search results to read: one document once, from its best copy, and no single site filling the list
(definitions/guidance/source_selection.json). Plain code, before anything is fetched; the extraction step skips copies that
only show up once pages are read (Runner._read_already).

    diversify(candidates) -> (kept, merged, capped)
"""

import json
import re
from functools import lru_cache

import corroboration
from config import PROJECT_ROOT

PATH = PROJECT_ROOT / "definitions" / "guidance" / "source_selection.json"
_WORD = re.compile(r"[a-z0-9]+")
_FILLER = {"the", "a", "an", "of", "to", "and", "in", "for", "on", "with", "at", "by", "its", "as", "is", "from", "news", "inc", "corp"}


@lru_cache(maxsize=1)
def rules() -> dict:
    d = json.loads(PATH.read_text(encoding="utf-8"))
    if not 0 < d["same_article"]["title_similarity"] <= 1 or d["max_per_site"] < 1:
        raise ValueError("source_selection.json: title_similarity must be in (0, 1] and max_per_site at least 1")
    return d


def _headline(title: str, url: str) -> set[str]:
    """The words of a headline that say what it is about, without the site's own name ('... - Reuters', '| Nasdaq')."""
    site = corroboration.site(url).split(".")[0]
    t = re.split(r"\s[|\-–—]\s(?=[^|\-–—]{0,40}$)", title or "")[0]          # drop a trailing '- Site name'
    return {w for w in _WORD.findall(t.lower()) if w not in _FILLER and w != site and len(w) > 1}


_DISTINCT = re.compile(r"\d|^(?:first|second|third|fourth|q[1-4]|h[12]|jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|"
                       r"oct\w*|nov\w*|dec\w*|annual|quarterly|interim|half|full)$")


def _same(a: set[str], b: set[str]) -> bool:
    if len(a) < 3 or len(b) < 3:
        return False                                     # too short a headline to call two pages one article
    if any(_DISTINCT.search(w) for w in a ^ b):
        return False     # 'First Quarter 2026 Results' and 'Second Quarter 2026 Results' are two documents, not one
    return len(a & b) / len(a | b) >= rules()["same_article"]["title_similarity"]


def _rank(c: dict) -> int:
    order = rules()["prefer"]
    return order.index(c.get("class")) if c.get("class") in order else len(order)


def diversify(candidates: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(kept, merged, capped). `merged`: copies of an article already kept, each with `copy_of`. `capped`: pages over the
    per-site limit. The order of `kept` is the candidates' own order, with each article represented by its best copy."""
    r = rules()
    groups: list[list[dict]] = []
    for c in candidates:
        words = _headline(c.get("title") or "", c["url"])
        for g in groups:
            if _same(words, g[0]["_words"]):
                g.append({**c, "_words": words})
                break
        else:
            groups.append([{**c, "_words": words}])
    kept, merged = [], []
    for g in groups:
        best = min(g, key=lambda c: (_rank(c), g.index(c)))
        kept.append(best)
        merged += [{**c, "copy_of": best["url"]} for c in g if c is not best]
    per_site: dict[str, int] = {}
    out, capped = [], []
    for c in kept:
        site = corroboration.site(c["url"])
        if c.get("class") not in r["exempt_classes"]:
            per_site[site] = per_site.get(site, 0) + 1
            if per_site[site] > r["max_per_site"]:
                capped.append(c)
                continue
        out.append(c)
    clean = lambda xs: [{k: v for k, v in x.items() if k != "_words"} for x in xs]     # noqa: E731
    return clean(out), clean(merged), clean(capped)
