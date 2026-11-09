"""Time attributes of a claim, read by code from the claim's own quote: the period it covers (fiscal year, quarter, half, months
ended, year over year change) and the dates of events (announced, effective, filed, closed, deal start and end).

Nothing here uses a model and nothing is guessed. A boundary the source does not state stays empty: "three months ended
September 26, 2026" gives an end date and a length, never an invented start. An unparsed phrase stays unparsed. Statements about
the future ("will become", "plans to") are recorded as `pending`, so that an announcement is not read as a completed change.
"""

import json
import re
from dataclasses import asdict, dataclass, field

from .extract import parse_date

_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December|"
           "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec")
DATE = (rf"(?:(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\.?,?\s+\d{{4}}"
        rf"|\d{{4}}-\d{{2}}-\d{{2}})")
_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4}
_NUM_WORDS = {"three": 3, "six": 6, "nine": 9, "twelve": 12, "3": 3, "6": 6, "9": 9, "12": 12}
_SPAN_FOR_MONTHS = {3: "quarter", 6: "half", 9: "ytd", 12: "full_year"}


@dataclass
class Period:
    basis: str | None = None          # fiscal | calendar | None when the text does not say
    span: str | None = None           # full_year | half | quarter | ytd | ttm | months
    label: str = ""
    fiscal_year: int | None = None    # the year as the text names it (a fiscal year is named by the text, never worked out)
    quarter: int | None = None
    half: int | None = None
    months: int | None = None
    start: str | None = None          # only when the text states it
    end: str | None = None
    comparison: str | None = None     # yoy | qoq
    change: str | None = None         # as written: "+12%", "down 3.5%"
    raw: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and v != ""}


@dataclass
class TimeItem:
    kind: str          # period | announced | effective | filed | closed | completed | as_of | start | end | round_date | pending
    value: str         # a date (YYYY, YYYY-MM, YYYY-MM-DD) or, for a period, its label; '' for pending
    precision: str     # year | month | day | datetime | period | ''
    raw: str           # the words it was read from
    origin: str        # quote | context | source_date | edgar_xbrl
    derived: bool = False
    detail: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        return {"kind": self.kind, "value": self.value, "precision": self.precision, "raw": self.raw, "origin": self.origin,
                "derived": int(self.derived), "detail": json.dumps(self.detail, sort_keys=True) if self.detail else ""}


def _year(text: str) -> int:
    n = int(text)
    return 2000 + n if n < 100 else n


def _day(raw: str) -> str | None:
    parsed = parse_date(raw)
    return parsed[0] if parsed and parsed[1] == "day" else None


def _basis(word: str | None) -> str | None:
    w = (word or "").lower()
    return "fiscal" if w in ("fy", "fiscal") else "calendar" if w in ("cy", "calendar") else None


def _label(p: Period) -> str:
    year = f"{p.fiscal_year}" if p.fiscal_year else ""
    tag = {"fiscal": "FY", "calendar": "CY"}.get(p.basis or "", "")
    if p.quarter:
        return f"{tag}{year} Q{p.quarter}".strip() if tag else f"Q{p.quarter} {year}".strip()
    if p.half:
        return f"{tag}{year} H{p.half}".strip() if tag else f"H{p.half} {year}".strip()
    if p.span == "full_year" and p.end and not year:
        return f"{'fiscal year' if p.basis else 'year'} ended {p.end}"
    if p.span == "full_year" and year:
        return f"{tag}{year}" if tag else f"full year {year}"
    if p.months and p.end and not year and p.span != "full_year":
        return f"{p.months} months ended {p.end}"
    if p.span == "ytd":
        return f"year to date {year}".strip()
    if p.span == "ttm":
        return "trailing twelve months" + (f" to {p.end}" if p.end else "")
    if p.months:
        return f"{p.months} months ended {p.end}" if p.end else f"{p.months} months"
    return f"{tag}{year}" if year else p.raw


# (pattern, builder). Each match is a (start, end, Period); a match inside a longer one is dropped afterwards.
_B = r"(?P<b>FY|CY|fiscal|calendar)?"
_Y = r"'?(?P<y>\d{4}|\d{2})\b"
_PATTERNS: list[tuple[re.Pattern, callable]] = []


def _pat(regex: str):
    def wrap(fn):
        _PATTERNS.append((re.compile(regex, re.I), fn))
        return fn
    return wrap


@_pat(rf"\b(?P<n>three|six|nine|twelve|3|6|9|12)[- ]months?\s+(?:period\s+)?ended\s+(?P<d>{DATE})")
def _months_ended(m):
    n = _NUM_WORDS[m["n"].lower()]
    return Period(span=_SPAN_FOR_MONTHS[n], months=n, end=_day(m["d"]), raw=m[0])


@_pat(rf"\b(?P<w>quarter|half[- ]year)\s+(?:ended|ending)\s+(?P<d>{DATE})")
def _quarter_ended(m):
    n = 3 if m["w"].lower() == "quarter" else 6
    return Period(span=_SPAN_FOR_MONTHS[n], months=n, end=_day(m["d"]), raw=m[0])


@_pat(rf"\b(?P<b>fiscal\s+)?years?\s+ended\s+(?P<d>{DATE})")
def _year_ended(m):
    return Period(basis="fiscal" if m["b"] else None, span="full_year", months=12, end=_day(m["d"]), raw=m[0])


@_pat(rf"\bQ(?P<q>[1-4])\s*(?:of\s+)?{_B}\s*{_Y}")
def _q_then_year(m):
    if not m["b"] and len(m["y"]) == 2:   # "Q3 25 million" is not a period
        return None
    return Period(basis=_basis(m["b"]), span="quarter", quarter=int(m["q"]), fiscal_year=_year(m["y"]), raw=m[0])


@_pat(rf"\b{_B}\s*{_Y}\s*Q(?P<q>[1-4])\b")
def _year_then_q(m):
    if not m["b"] and len(m["y"]) == 2:
        return None
    return Period(basis=_basis(m["b"]), span="quarter", quarter=int(m["q"]), fiscal_year=_year(m["y"]), raw=m[0])


@_pat(rf"\b(?P<o>first|second|third|fourth)[\s-]+quarter(?:\s+of)?\s+(?:(?P<b>fiscal|calendar)\s+)?(?:year\s+)?(?P<y>\d{{4}})\b")
def _quarter_of_year(m):
    return Period(basis=_basis(m["b"]), span="quarter", quarter=_ORD[m["o"].lower()], fiscal_year=int(m["y"]), raw=m[0])


@_pat(rf"\b(?:(?P<b>fiscal|calendar)\s+)?(?P<y>\d{{4}})\s+(?P<o>first|second|third|fourth)[\s-]+quarter\b")
def _year_quarter_words(m):
    return Period(basis=_basis(m["b"]), span="quarter", quarter=_ORD[m["o"].lower()], fiscal_year=int(m["y"]), raw=m[0])


@_pat(rf"\bH(?P<h>[12])\s*{_B}\s*{_Y}")
def _h_year(m):
    if not m["b"] and len(m["y"]) == 2:   # "Q3 25 million" is not a period
        return None
    return Period(basis=_basis(m["b"]), span="half", half=int(m["h"]), months=6, fiscal_year=_year(m["y"]), raw=m[0])


@_pat(rf"\b(?P<o>first|second)[\s-]+half(?:\s+of)?\s+(?:(?P<b>fiscal|calendar)\s+)?(?:year\s+)?(?P<y>\d{{4}})\b")
def _half_of_year(m):
    return Period(basis=_basis(m["b"]), span="half", half=_ORD[m["o"].lower()], months=6, fiscal_year=int(m["y"]), raw=m[0])


@_pat(r"\b(?:fiscal\s+(?:year\s+)?|FY\s*)'?(?P<y>\d{4}|\d{2})\b")
def _fiscal_year(m):
    return Period(basis="fiscal", span="full_year", fiscal_year=_year(m["y"]), raw=m[0])


@_pat(r"\b(?:calendar\s+year|CY)\s*(?P<y>\d{4})\b")
def _calendar_year(m):
    return Period(basis="calendar", span="full_year", fiscal_year=int(m["y"]), raw=m[0])


@_pat(r"\b(?:full[- ]year\s+(?P<y>\d{4})|(?P<y2>\d{4})\s+full[- ]year)\b")
def _full_year(m):
    return Period(span="full_year", fiscal_year=int(m["y"] or m["y2"]), raw=m[0])


@_pat(r"\b(?:trailing[- ]twelve[- ]months|TTM|LTM)\b")
def _ttm(m):
    return Period(span="ttm", months=12, raw=m[0])


@_pat(r"\b(?:year[- ]to[- ]date|YTD)\b")
def _ytd(m):
    return Period(span="ytd", raw=m[0])


_COMPARE = re.compile(r"\b(year[- ]over[- ]year|YoY|y/y|quarter[- ]over[- ]quarter|QoQ|q/q|"
                      r"compared (?:to|with) the (?:same|prior|previous)[^.,;]{0,40}?(?:year|quarter|period))\b", re.I)
_CHANGE = re.compile(r"(?:(?P<w>up|down|increased?|decreased?|grew|rose|fell|declined|higher|lower)\s+(?:by\s+)?)?"
                     r"(?P<s>[+\-−])?\s?(?P<n>\d+(?:\.\d+)?)\s?%", re.I)
_DOWN = {"down", "decrease", "decreased", "fell", "declined", "lower"}


def find_periods(text: str) -> list[Period]:
    """Every period named in `text`, in order. A match inside a longer one ("Q3 FY26" also contains "FY26") is dropped."""
    found: list[tuple[int, int, Period]] = []
    for regex, build in _PATTERNS:
        for m in regex.finditer(text):
            p = build(m)
            if p is not None:
                found.append((m.start(), m.end(), p))
    found = [f for f in found if not any(o is not f and o[0] <= f[0] and f[1] <= o[1] and (o[1] - o[0]) > (f[1] - f[0]) for o in found)]
    found.sort(key=lambda f: f[0])
    periods = []
    for i, (start, end, p) in enumerate(found):
        p.label = _label(p)
        limit = found[i + 1][0] if i + 1 < len(found) else len(text)
        tail = text[end:min(limit, end + 200)]
        if cmp := _COMPARE.search(tail):
            word = cmp[0].lower()
            p.comparison = "qoq" if ("quarter-over" in word.replace(" ", "-") or word in ("qoq", "q/q")) else "yoy"
            if chg := _CHANGE.search(tail[cmp.end():cmp.end() + 80]) or _CHANGE.search(tail[max(0, cmp.start() - 80):cmp.start()]):
                sign = "-" if (chg["s"] in ("-", "−") or (chg["w"] or "").lower() in _DOWN) else "+" if (chg["s"] == "+" or chg["w"]) else ""
                p.change = f"{sign}{chg['n']}%"
        periods.append(p)
    return periods


# --- events -----------------------------------------------------------------------------------------------------------

_EVENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("effective", re.compile(rf"\b(?:effective(?:\s+as\s+of)?|takes?\s+effect|will\s+take\s+effect)\s+(?:on\s+)?(?P<d>{DATE})", re.I)),
    ("announced", re.compile(rf"\bannounce[sd]?\s+(?:on\s+)?(?P<d>{DATE})", re.I)),
    ("announced", re.compile(rf"\bOn\s+(?P<d>{DATE}),[^.]{{0,80}}?\bannounce", re.I)),
    ("filed", re.compile(rf"\bfiled\s+(?:on\s+)?(?P<d>{DATE})", re.I)),
    ("closed", re.compile(rf"\bclosed\s+(?:on\s+)?(?P<d>{DATE})", re.I)),
    ("completed", re.compile(rf"\b(?:completed|finalized)\s+(?:on\s+)?(?P<d>{DATE})", re.I)),
    ("as_of", re.compile(rf"\bas\s+of\s+(?P<d>{DATE})", re.I)),
    ("round_date", re.compile(rf"\b(?:funding\s+round|seed\s+round|Series\s+[A-Z]\w*)[^.]{{0,80}}?\b(?:on|in)\s+(?P<d>{DATE})", re.I)),
]
_RANGE = re.compile(rf"\bfrom\s+(?P<a>{DATE})\s+(?:to|through|until)\s+(?P<b>{DATE})", re.I)
_START = re.compile(rf"\b(?:beginning|starting|commencing|commenced)\s+(?:on\s+)?(?P<d>{DATE})", re.I)
_END = re.compile(rf"\b(?:until|through|ending|expir(?:es|ing|ed))\s+(?:on\s+)?(?P<d>{DATE})", re.I)
_TODAY_ANNOUNCED = re.compile(r"\b(?:today,?\s+(?:we|[A-Z]\w+)\s+announced|announced\s+today)\b", re.I)
_PENDING = re.compile(r"\b(will\s+(?:become|be|step|assume|join|take)|plans?\s+to|is\s+planning\s+to|(?:is|are)\s+(?:expected|set|scheduled)\s+to|"
                      r"expected\s+to|intends?\s+to|following\s+a\s+transition(?:\s+period)?)\b", re.I)


def find_events(text: str, published: str | None = None, published_precision: str | None = None) -> list[TimeItem]:
    """Dated events in `text`. "Today, we announced" is dated by the page's own publication date, marked `derived`.
    A statement about the future gets a `pending` item: it says an event is planned, not that it happened."""
    items: list[TimeItem] = []

    def add(kind, raw_date, raw, origin="quote", derived=False):
        if (day := _day(raw_date)) and not any(i.kind == kind and i.value == day for i in items):
            items.append(TimeItem(kind, day, "day", " ".join(raw.split())[:160], origin, derived))

    for kind, regex in _EVENT_PATTERNS:
        for m in regex.finditer(text):
            add(kind, m["d"], m[0])
    for m in _RANGE.finditer(text):
        add("start", m["a"], m[0])
        add("end", m["b"], m[0])
    if not any(i.kind == "start" for i in items):
        for m in _START.finditer(text):
            add("start", m["d"], m[0])
    if not any(i.kind == "end" for i in items):
        for m in _END.finditer(text):
            add("end", m["d"], m[0])
    if published and published_precision in ("day", "datetime") and _TODAY_ANNOUNCED.search(text) \
            and not any(i.kind == "announced" for i in items):
        items.append(TimeItem("announced", published[:10], "day", "announced today, on the page's publication date", "source_date", True))
    if m := _PENDING.search(text):
        items.append(TimeItem("pending", "", "", " ".join(m[0].split()), "quote"))
    return items


def analyse(quote: str, context: str = "", published: str | None = None, published_precision: str | None = None) -> list[TimeItem]:
    """All time attributes for one claim: from its quote first, and from the text around it only for what the quote lacks
    (marked `context`, so a reader can see it did not come from the quoted words)."""
    items: list[TimeItem] = []

    def periods(text, origin):
        for p in find_periods(text):
            items.append(TimeItem("period", p.label, "period", p.raw[:160], origin, False, p.as_dict()))

    periods(quote, "quote")
    items += find_events(quote, published, published_precision)
    if context:
        if not any(i.kind == "period" for i in items):
            periods(context, "context")
        have = {i.kind for i in items}
        for ev in find_events(context, published, published_precision):
            if ev.kind not in have and ev.kind != "pending":
                ev.origin = "context" if ev.origin == "quote" else ev.origin
                items.append(ev)
    return items


# --- exact periods from structured data (SEC XBRL facts) ---------------------------------------------------------------

_XBRL_KEY = re.compile(r'"(start|end|fy|fp|form|filed|frame|accn)"\s*:\s*("[^"]*"|\d+)')


def period_from_xbrl(text: str, quote_start: int, quote_end: int) -> list[TimeItem]:
    """An SEC companyfacts fact carries its own period: {"start","end","val","accn","fy","fp","form","filed"}. When the quote sits
    inside such an object the period is read straight from it: exact, no parsing of prose, nothing derived. Returns [] otherwise."""
    lo, hi = text.rfind("{", 0, quote_start + 1), text.find("}", quote_end)
    if lo < 0 or hi < 0:
        return []
    keys = {k: v.strip('"') for k, v in _XBRL_KEY.findall(text[lo:hi + 1])}
    if "end" not in keys or not (_day(keys["end"]) and ("fp" in keys or "fy" in keys)):
        return []
    fp, fy = keys.get("fp", ""), keys.get("fy")
    p = Period(basis="fiscal", fiscal_year=int(fy) if fy and fy.isdigit() else None, start=_day(keys["start"]) if "start" in keys else None,
               end=_day(keys["end"]), raw=text[lo:hi + 1][:160])
    if fp.upper() == "FY":
        p.span = "full_year"
    elif re.fullmatch(r"Q[1-4]", fp.upper()):
        p.span, p.quarter = "quarter", int(fp[1])
    p.label = _label(p)
    detail = p.as_dict()
    if "form" in keys:
        detail["form"] = keys["form"]
    items = [TimeItem("period", p.label, "period", p.raw, "edgar_xbrl", False, detail)]
    if "filed" in keys and (day := _day(keys["filed"])):
        items.append(TimeItem("filed", day, "day", f'"filed":"{keys["filed"]}"', "edgar_xbrl"))
    return items


# --- does the claim's period match the quote's? ------------------------------------------------------------------------

def _compatible(a: Period, b: Period) -> bool:
    """Two periods agree unless they name a different value for something both state."""
    for attr in ("basis", "fiscal_year", "quarter", "half"):
        x, y = getattr(a, attr), getattr(b, attr)
        if x is not None and y is not None and x != y:
            return False
    return True


def period_conflicts(claim_text: str, quote: str) -> list[str]:
    """Periods the claim names that the quote contradicts (the claim says Q3 and the quote only speaks of the fourth quarter).
    Empty when the claim names no period or the quote names none, because then there is nothing to compare."""
    mine, theirs = find_periods(claim_text), find_periods(quote)
    if not mine or not theirs:
        return []
    return [f"claim says {p.label} but the quote speaks of {', '.join(sorted({t.label for t in theirs}))}"
            for p in mine if not any(_compatible(p, t) for t in theirs)]
