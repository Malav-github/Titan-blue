"""How well is a fact backed, and do the sources agree? Labels, never filters: nothing is hidden from the reader.

Two claims about the same needed fact conflict only when both give a numeric value, the values differ, and nothing about
their time frames explains the difference: a figure for FY2025 and a figure for FY2026 are both true, and so are a headcount
as of 2024 and one as of 2026. Names are never compared (the CEO before and after a change are not a contradiction).
"""

import re
from itertools import combinations
from urllib.parse import urlparse

_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu"}
_DATED = ("period", "as_of", "announced", "effective", "closed", "completed", "round_date")


def site(url: str) -> str:
    """The owner of a page, roughly: blog.dropbox.com and www.dropbox.com are both dropbox.com."""
    labels = (urlparse(url).hostname or "").lower().split(".")
    keep = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


def label(row: dict, sites_for_fact: set[str], disputed: bool) -> str:
    if disputed:
        return "sources disagree"
    if row.get("source_tier") == "primary":
        return "primary source"
    if len(sites_for_fact) >= 2:
        return f"{len(sites_for_fact)} independent sources"
    return "single source, unconfirmed"


def _norm(value: str) -> str:
    return re.sub(r"[\s,$%]", "", str(value or "")).lower()


def _periods(row: dict) -> set[str]:
    return {t["value"] for t in row.get("times", []) if t["kind"] == "period"}


def _moments(row: dict) -> set[tuple[str, str]]:
    return {(t["kind"], t["value"]) for t in row.get("times", []) if t["kind"] in _DATED and t["kind"] != "period" and t["value"]}


def disagree(a: dict, b: dict) -> str | None:
    """A note saying why the two claims conflict, or None when they do not."""
    va, vb = _norm(a.get("value")), _norm(b.get("value"))
    if not va or not vb or va == vb or not (re.search(r"\d", va) and re.search(r"\d", vb)):
        return None
    pa, pb = _periods(a), _periods(b)
    if pa or pb:
        if pa != pb:           # different periods, or only one states its period: nothing to compare
            return None
    elif _moments(a) and _moments(b) and _moments(a) != _moments(b):
        return None            # stated as of different dates
    return f"{a['value']} (claim {a['claim_id']}) vs {b['value']} (claim {b['claim_id']}) for the same fact"


def find_conflicts(rows: list[dict]) -> list[dict]:
    """Rows are claims with `claim_id`, `fact` (the needed-fact index), `value`, `times`. Returns [{claims, note}]."""
    out = []
    for a, b in combinations(rows, 2):
        if a.get("fact") is None or a.get("fact") != b.get("fact") or a["claim_id"] == b["claim_id"]:
            continue
        if note := disagree(a, b):
            out.append({"claims": sorted((a["claim_id"], b["claim_id"])), "note": note})
    return out
