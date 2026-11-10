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


_SENTENCE = re.compile(r"(?<=[.!?])\s+")
SHINGLE_CHARS = 60         # a sentence at least this long, word for word, is not shared by chance
COPY_SHARED = 2            # sharing this many such sentences makes two pages copies of one document


def fingerprint(text: str, head_chars: int = 6000) -> set[int]:
    """Hashes of the long sentences near the top of a page. Two pages that share several are copies of one document (a
    company release on its newsroom and on PR Newswire, or a story syndicated to Yahoo Finance)."""
    body = re.sub(r"\s+", " ", (text or "")[:head_chars]).strip().lower()
    return {hash(s) for s in _SENTENCE.split(body) if len(s) >= SHINGLE_CHARS}


def copy_groups(prints: dict[str, set[int]]) -> dict[str, str]:
    """Source id -> the id that stands for its group of copies (the first in the given order). A page shares a group with
    any page it shares COPY_SHARED long sentences with, directly or through another copy."""
    keys = list(prints)
    parent = {k: k for k in keys}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if len(prints[a] & prints[b]) >= COPY_SHARED:
                ra, rb = root(a), root(b)
                if ra != rb:
                    parent[rb if keys.index(rb) > keys.index(ra) else ra] = ra if keys.index(rb) > keys.index(ra) else rb
    return {k: root(k) for k in keys}


def label(row: dict, sites_for_fact: set[str], disputed: bool) -> str:
    if disputed:
        return "sources disagree"
    if strong := row.get("strength"):                 # Tier 1 or 2 (settling.py): settles on its own; say its tier and kind
        return f"{strong['tier_label']}, {strong['label']}"
    if row.get("source_tier") == "primary":           # a row built without the tier rule (older callers)
        return "primary source"
    blog = f" ({row['blog']['why']})" if (row.get("blog") or {}).get("why") else ""     # why a blog post is not Tier 1
    if len(sites_for_fact) >= 2:
        return f"Tier 3: {len(sites_for_fact)} independent sources{blog}"
    return f"Tier 3: single source, unconfirmed{blog}"


def _norm(value: str) -> str:
    return re.sub(r"[\s,$%]", "", str(value or "")).lower()


_MONTH = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", re.I)


def _calendar_day(value: str):
    """The date a value states in words ('October 7, 2024', '7 Oct 2024'), or None. Needs a month name and a day and a year."""
    text = str(value or "")
    if not _MONTH.search(text) or not re.search(r"\b\d{1,2}\b", text) or not re.search(r"\b\d{4}\b", text):
        return None
    try:
        from dateutil import parser
        return parser.parse(text, fuzzy=False).date()
    except (ValueError, OverflowError):
        return None


def _periods(row: dict) -> set[str]:
    return {t["value"] for t in row.get("times", []) if t["kind"] == "period"}


def _moments(row: dict) -> set[tuple[str, str]]:
    return {(t["kind"], t["value"]) for t in row.get("times", []) if t["kind"] in _DATED and t["kind"] != "period" and t["value"]}


def disagree(a: dict, b: dict) -> str | None:
    """A note saying why the two claims conflict, or None when they do not."""
    va, vb = _norm(a.get("value")), _norm(b.get("value"))
    if not va or not vb or va == vb or not (re.search(r"\d", va) and re.search(r"\d", vb)):
        return None
    day_a, day_b = _calendar_day(a.get("value")), _calendar_day(b.get("value"))
    if day_a and day_a == day_b:
        return None            # the same calendar day written two ways
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
