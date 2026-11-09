"""Dates a page carries, collected by code with their origin and precision.

Every date records where it came from (`meta:article:published_time`, `jsonld:datePublished`, `text:byline`, `http:Last-Modified`,
`url`, `search:...`) and how exact it is (year, month, day or datetime). Nothing is guessed: a date without a year is dropped,
a relative date ("3 days ago") is turned into a real one only against a known retrieval time and is marked `relative`, and
HTTP headers are kept but never trusted as the date of the content (servers often send the time of the request).
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dateutil import parser as dparser
from dateutil.relativedelta import relativedelta

MIN_YEAR = 1990
PRECISIONS = ("year", "month", "day", "datetime")
MAX_PER_PAGE = 40


@dataclass(frozen=True)
class DateFact:
    kind: str          # published | modified | created | event_start | event_end | time_element | url_date | search_result | server_date
    value: str         # YYYY, YYYY-MM, YYYY-MM-DD or an ISO datetime
    precision: str     # year | month | day | datetime
    raw: str           # the text it was read from
    origin: str        # where it was found
    relative: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


# --- reading one date --------------------------------------------------------------------------------------------

_YEAR = re.compile(r"^\d{4}$")
_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
_DAY = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_RELATIVE = re.compile(r"^(?:(\d+|an?|one)\s+(minute|hour|day|week|month|year)s?\s+ago|yesterday|today|just now)$")
_HAS_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
_RELATIVE_PRECISION = {"minute": "datetime", "hour": "datetime", "day": "day", "week": "day", "month": "month", "year": "year"}


def _in_bounds(year: int, anchor: datetime | None) -> bool:
    return MIN_YEAR <= year <= ((anchor.year + 1) if anchor else 2100)


def _fmt(d: datetime, precision: str) -> str:
    return {"year": f"{d.year:04d}", "month": f"{d.year:04d}-{d.month:02d}", "day": d.date().isoformat(),
            "datetime": d.isoformat(timespec="seconds")}[precision]


def parse_date(raw, anchor: datetime | None = None) -> tuple[str, str, bool] | None:
    """(value, precision, relative) or None when `raw` is not a usable date. `anchor` (an aware datetime, normally the
    retrieval time) is needed for relative dates and sets the upper bound on years."""
    text = " ".join(str(raw).split())
    if not text or len(text) > 80 or not any(c.isdigit() for c in text) and not _RELATIVE.match(text.lower()):
        return None
    low = text.lower()
    if m := _RELATIVE.match(low):
        if anchor is None:
            return None
        if low in ("today", "just now"):
            return _fmt(anchor, "day"), "day", True
        if low == "yesterday":
            return _fmt(anchor - timedelta(days=1), "day"), "day", True
        count = 1 if m.group(1) in ("a", "an", "one") else int(m.group(1))
        unit = m.group(2)
        precision = _RELATIVE_PRECISION[unit]
        return _fmt(anchor - relativedelta(**{unit + "s": count}), precision), precision, True
    try:
        if _YEAR.match(text):
            return (text, "year", False) if _in_bounds(int(text), anchor) else None
        if m := _MONTH.match(text):
            y, mo = int(m.group(1)), int(m.group(2))
            return (f"{y:04d}-{mo:02d}", "month", False) if 1 <= mo <= 12 and _in_bounds(y, anchor) else None
        if m := _DAY.match(text):
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return (d.date().isoformat(), "day", False) if _in_bounds(d.year, anchor) else None
        if _DATETIME.match(text):
            d = dparser.isoparse(text)
            return (d.isoformat(timespec="seconds"), "datetime", False) if _in_bounds(d.year, anchor) else None
        # free text such as "May 2007" or "12 Jun 2017": parse twice with different defaults to see which parts were really there
        a, b = dparser.parse(text, default=datetime(1900, 1, 1)), dparser.parse(text, default=datetime(1901, 2, 2))
    except (ValueError, OverflowError, dparser.ParserError):
        return None
    if a.year != b.year or not _in_bounds(a.year, anchor):
        return None                                           # no year in the text: not a usable date
    if a.month != b.month:
        return f"{a.year:04d}", "year", False
    if a.day != b.day:
        return f"{a.year:04d}-{a.month:02d}", "month", False
    if _HAS_TIME.search(text):
        return a.isoformat(timespec="seconds"), "datetime", False
    return a.date().isoformat(), "day", False


# --- finding dates in a page -------------------------------------------------------------------------------------

_META_KINDS = {
    "article:published_time": "published", "og:article:published_time": "published", "datepublished": "published",
    "pubdate": "published", "publishdate": "published", "publish_date": "published", "publication_date": "published",
    "citation_publication_date": "published", "citation_online_date": "published", "citation_date": "published",
    "dc.date": "published", "dc.date.issued": "published", "dcterms.issued": "published", "dcterms.created": "created",
    "dc.date.created": "created", "date": "published", "article:modified_time": "modified", "og:updated_time": "modified",
    "og:article:modified_time": "modified", "datemodified": "modified", "last-modified": "modified",
    "dcterms.modified": "modified", "dc.date.modified": "modified",
}
_JSONLD_KINDS = {"datePublished": "published", "uploadDate": "media_upload", "dateCreated": "created", "dateModified": "modified",
                 "startDate": "event_start", "endDate": "event_end"}
# Dates inside these objects describe the object (an embedded video's upload date), not the page that carries them.
_JSONLD_SKIP_TYPES = {"VideoObject", "AudioObject", "ImageObject", "MediaObject", "Comment", "Review", "Person", "Organization",
                      "Product", "Offer", "Rating", "Question", "Answer"}
_BYLINE = re.compile(
    r"(?P<verb>published|posted|released|filed|submitted on|last updated|updated|last modified|last revised|last edited on|revised)"
    r"\s*(?:on|:|-)?\s*(?P<date>(?:[A-Z][a-z]{2,8}\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4})|(?:\d{1,2}(?:st|nd|rd|th)? [A-Z][a-z]{2,8}\.?,? \d{4})|"
    r"(?:\d{4}-\d{2}-\d{2}))", re.I)
_BYLINE_KIND = {"published": "published", "posted": "published", "released": "published", "filed": "published",
                "submitted on": "published", "last updated": "modified", "updated": "modified", "last modified": "modified",
                "last revised": "modified", "last edited on": "modified", "revised": "modified"}
_URL_DAY = re.compile(r"/((?:19|20)\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:/|-|$|\.)")
_URL_MONTH = re.compile(r"/((?:19|20)\d{2})/(\d{2})/")


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    return {t} if isinstance(t, str) else {x for x in t if isinstance(x, str)} if isinstance(t, list) else set()


def _walk(node, found: list, depth: int = 0) -> None:
    if depth > 6 or len(found) >= MAX_PER_PAGE:
        return
    if isinstance(node, dict):
        if _types(node) & _JSONLD_SKIP_TYPES:
            return
        for key, value in node.items():
            if key in _JSONLD_KINDS and isinstance(value, str):
                found.append((_JSONLD_KINDS[key], value, f"jsonld:{key}"))
            else:
                _walk(value, found, depth + 1)
    elif isinstance(node, list):
        for item in node[:50]:
            _walk(item, found, depth + 1)


def collect_dates(html: str | None = None, headers: dict | None = None, url: str = "", hit_published: str | None = None,
                  retrieved_at: datetime | None = None) -> list[DateFact]:
    """Every usable date a fetched page offers, de-duplicated, with origin and precision. `headers` needs only
    Last-Modified and Date (lower-case keys); `retrieved_at` anchors relative dates ("3 days ago")."""
    anchor = retrieved_at or datetime.now(timezone.utc)
    found: list[tuple[str, str, str]] = []                  # (kind, raw text, origin)

    if html:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("meta"):
            name = (tag.get("name") or tag.get("property") or tag.get("itemprop") or tag.get("http-equiv") or "").strip().lower()
            content = (tag.get("content") or "").strip()
            if content and name in _META_KINDS:
                found.append((_META_KINDS[name], content, f"meta:{name}"))
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            from_script: list[tuple[str, str, str]] = []
            try:
                _walk(json.loads(script.string or script.get_text() or ""), from_script)
            except (ValueError, TypeError):
                continue
            found += from_script
        for tag in soup.find_all("time", attrs={"datetime": True})[:5]:
            found.append(("time_element", tag["datetime"].strip(), "time:datetime"))
        visible = " ".join(soup.get_text(" ").split())
        for chunk in (visible[:4000], visible[-2000:] if len(visible) > 4000 else ""):
            for m in _BYLINE.finditer(chunk):
                found.append((_BYLINE_KIND[m.group("verb").lower()], m.group("date"), "text:byline"))

    path = urlparse(url).path if url else ""
    if m := _URL_DAY.search(path):
        found.append(("url_date", f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "url"))
    elif m := _URL_MONTH.search(path):
        found.append(("url_date", f"{m.group(1)}-{m.group(2)}", "url"))

    for name, kind, origin in (("last-modified", "modified", "http:Last-Modified"), ("date", "server_date", "http:Date")):
        raw = (headers or {}).get(name)
        if raw:
            try:
                found.append((kind, parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat(timespec="seconds"), origin))
            except (TypeError, ValueError):
                continue
    if hit_published:
        found.append(("search_result", str(hit_published), "search"))

    facts, seen = [], set()
    for kind, raw, origin in found:
        parsed = parse_date(raw, anchor)
        if parsed is None:
            continue
        value, precision, relative = parsed
        key = (kind, value, origin)
        if key not in seen:
            seen.add(key)
            facts.append(DateFact(kind, value, precision, " ".join(raw.split())[:120], origin, relative))
    return facts[:MAX_PER_PAGE]


# --- choosing among them -----------------------------------------------------------------------------------------

CONTENT_KINDS = ("published", "created", "modified", "event_start", "event_end", "time_element", "url_date", "search_result")
_DISPLAY_ORDER = ("published", "created", "search_result", "url_date", "time_element", "modified")
_PRECISION_RANK = {"datetime": 3, "day": 2, "month": 1, "year": 0}
# Most authoritative first. The regulator's record of when a filing was filed outranks anything read from its text (a 10-Q
# quotes the dates of old debt notes near its top, and those read as bylines). A page's own structured "published" statement
# outranks a byline, which outranks weaker signals.
_ORIGIN_ORDER = ("edgar:filingDate", "jsonld:datePublished", "meta:article:published_time", "meta:og:article:published_time", "meta:datepublished",
                 "meta:publishdate", "meta:publish_date", "meta:pubdate", "meta:publication_date", "meta:citation_publication_date",
                 "meta:citation_date", "meta:dc.date.issued", "meta:dcterms.issued", "meta:dc.date", "meta:date", "text:byline",
                 "meta:citation_online_date")


def _origin_rank(origin: str) -> int:
    return _ORIGIN_ORDER.index(origin) if origin in _ORIGIN_ORDER else len(_ORIGIN_ORDER)


def is_content_date(fact: DateFact) -> bool:
    """A date that says something about the content. HTTP headers say when the server answered, not when it was written."""
    return fact.kind in CONTENT_KINDS and not fact.origin.startswith("http:")


def best_date(facts: list[DateFact]) -> DateFact | None:
    """The date to show as 'this source is dated': a published date if there is one, then weaker signals. Within a kind,
    the most authoritative origin wins; only then the most precise date, then the earliest."""
    for kind in _DISPLAY_ORDER:
        same = [f for f in facts if f.kind == kind and is_content_date(f)]
        if same:
            return sorted(same, key=lambda f: (_origin_rank(f.origin), -_PRECISION_RANK[f.precision], f.value))[0]
    return None


def latest_possible(fact: DateFact) -> date:
    """The last day the fact could mean: a year means 31 December, a month its last day. Used for age, so a source dated
    only '2024' is never called older than it might be."""
    parts = fact.value[:10].split("-")
    y = int(parts[0])
    if fact.precision == "year":
        return date(y, 12, 31)
    m = int(parts[1])
    if fact.precision == "month":
        return (date(y, m, 1) + relativedelta(months=1)) - timedelta(days=1)
    return date(y, m, int(parts[2]))


def newest_content_date(facts: list[DateFact]) -> DateFact | None:
    """The most recent published or modified date among the trusted ones."""
    pool = [f for f in facts if is_content_date(f) and f.kind in ("published", "created", "modified", "search_result", "url_date", "time_element")]
    return max(pool, key=latest_possible) if pool else None
