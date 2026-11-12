"""The reporting period and the announcement window a question asks for, as dates a filing and a table column can be
matched against. Code only, built on the period parser (periods.find_periods).

    target(question, today)          -> Target: the period asked (quarter, half, ytd, full year, months ended, as of, a month)
                                        and the window the results were announced or filed in; `latest` when neither is named
    period_end(target, fye)          -> (nominal end date, months) for the company's fiscal year end ('1231', '0927'), or None
    column_fits(column, end, months) -> does a statement column cover that period

Fiscal periods are worked out from the company's own fiscal year end (SEC: the registrant's 'fiscalYearEnd'; a market's
statutory year, India April to March), with a fiscal year named by the calendar year it ends in, which is how SEC filers
tag it (dei:DocumentFiscalYearFocus). A company on a 52/53-week year ends its periods near, not on, the month end: a
column ending within `TOLERANCE_DAYS` of the nominal end is the same period. When the fiscal year end is not known and the
question says 'fiscal', nothing is matched by guess: the target says so (`needs_fye`).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from .periods import DATE, _day, find_periods

TOLERANCE_DAYS = 10
_MONTH_NAMES = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)"
_ANNOUNCE = r"(?:announced|reported|released|published|filed|issued|disclosed|out)"
_ANNOUNCED_DAY = re.compile(rf"\b{_ANNOUNCE}\s+(?:on\s+)?(?P<d>{DATE})", re.I)
_ANNOUNCED_MONTH = re.compile(rf"\b{_ANNOUNCE}\s+(?:in|during)\s+(?P<m>{_MONTH})\.?\s+(?P<y>\d{{4}})\b", re.I)
_ANNOUNCED_YEAR = re.compile(rf"\b{_ANNOUNCE}\s+(?:in|during)\s+(?P<y>(?:19|20)\d{{2}})\b", re.I)
_AS_OF = re.compile(rf"\b(?:as\s+(?:of|at)|at)\s+(?P<d>{DATE})", re.I)
_MONTH_YEAR = re.compile(rf"\b(?:for\s+|in\s+|ending\s+|ended\s+)?(?:the\s+)?(?P<m>{_MONTH})\.?\s+(?P<y>\d{{4}})\b(?:\s+quarter)?", re.I)
_LAST = re.compile(r"\b(?:last|previous|prior)\s+(?P<w>fiscal\s+year|year|quarter)\b", re.I)


@dataclass
class Target:
    latest: bool = True
    span: str | None = None             # quarter | half | ytd | full_year | months | instant | month
    months: int | None = None
    quarter: int | None = None
    half: int | None = None
    fiscal_year: int | None = None
    basis: str | None = None            # fiscal | calendar | None (unsaid)
    end: str | None = None              # a stated end date ('three months ended September 30, 2026', 'as of June 30, 2026')
    month: str | None = None            # YYYY-MM: a period ending in that month ('the September 2026 quarter')
    announced_from: str | None = None   # the window the results were announced or filed in
    announced_to: str | None = None
    label: str = ""
    raw: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", [])}


def _iso(d: date) -> str:
    return d.isoformat()


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def target(question: str, today: date | None = None) -> Target:
    """The period and the announcement window a question names. A question naming neither wants the latest reported."""
    today = today or date.today()
    q = " ".join(question.split())
    t = Target()
    rest = q
    # when the results came out: taken out of the text first, so 'announced in October 2026' is not read as a period
    if m := _ANNOUNCED_DAY.search(rest):
        day = _day(m["d"])
        t.announced_from = t.announced_to = day
        t.raw.append(m[0])
        rest = rest.replace(m[0], " ")
    elif m := _ANNOUNCED_MONTH.search(rest):
        y, mo = int(m["y"]), _month_number(m["m"])
        t.announced_from, t.announced_to = _iso(date(y, mo, 1)), _iso(_month_end(y, mo))
        t.raw.append(m[0])
        rest = rest.replace(m[0], " ")
    elif m := _ANNOUNCED_YEAR.search(rest):
        t.announced_from, t.announced_to = f"{m['y']}-01-01", f"{m['y']}-12-31"
        t.raw.append(m[0])
        rest = rest.replace(m[0], " ")
    if m := _AS_OF.search(rest):                          # a balance sheet date
        t.latest, t.span, t.end = False, "instant", _day(m["d"])
        t.raw.append(m[0])
    elif periods := find_periods(rest):
        p = periods[0]
        t.latest = False
        t.span, t.months, t.quarter, t.half = p.span, p.months, p.quarter, p.half
        t.fiscal_year, t.basis, t.end = p.fiscal_year, p.basis, p.end
        if t.span == "ytd" and not (t.end or t.fiscal_year):
            t.latest = True                               # 'year to date' alone: the latest year-to-date column
        if not t.basis and re.search(r"\bfiscal\b|\bFY\s*'?\d", rest, re.I):
            t.basis = "fiscal"                            # 'fiscal Q3 2026': the word is said, even where the pattern skipped it
        t.raw.append(p.raw)
    elif m := _LAST.search(rest):
        w = m["w"].lower()
        t.latest = False
        if "quarter" in w:
            t.latest, t.span = True, "quarter"            # the last reported quarter is the latest quarter
        else:
            t.span, t.months, t.fiscal_year, t.basis = "full_year", 12, today.year - 1, "fiscal" if "fiscal" in w else None
        t.raw.append(m[0])
    elif m := _MONTH_YEAR.search(rest):
        mo = _month_number(m["m"])
        t.latest, t.span, t.month = False, "month", f"{int(m['y']):04d}-{mo:02d}"
        t.raw.append(m[0])
    elif years := re.findall(r"\b(?:in|for|of|during)\s+((?:19|20)\d{2})\b", rest):
        t.latest, t.span, t.months, t.fiscal_year = False, "full_year", 12, int(years[0])
        t.raw.append(years[0])
    t.label = describe(t)
    return t


def _month_number(word: str) -> int:
    w = word.lower().rstrip(".")
    return _MONTH_NAMES.get(w) or _MONTH_NAMES[w[:3]]


def describe(t: Target) -> str:
    """The period in words, as asked: 'the third quarter of fiscal 2026', 'the three months ended 2026-09-30'."""
    basis = f"{t.basis} " if t.basis else ""
    if t.span == "instant" and t.end:
        what = f"as of {t.end}"
    elif t.end:
        what = f"the {t.months or ''} months ended {t.end}".replace("the  months", "the period")
    elif t.span == "quarter" and t.quarter and t.fiscal_year:
        what = f"the {['first', 'second', 'third', 'fourth'][t.quarter - 1]} quarter of {basis}{t.fiscal_year}"
    elif t.span == "half" and t.half and t.fiscal_year:
        what = f"the {['first', 'second'][t.half - 1]} half of {basis}{t.fiscal_year}"
    elif t.span == "full_year" and t.fiscal_year:
        what = f"{basis or ''}year {t.fiscal_year}".replace("fiscal year", "fiscal year").strip()
    elif t.span == "month" and t.month:
        y, m = t.month.split("-")
        what = f"the period ended in {calendar.month_name[int(m)]} {y}"
    elif t.span == "ytd" and t.fiscal_year:
        what = f"year to date {t.fiscal_year}"
    else:
        what = "the latest reported period"
    if t.announced_from:
        when = t.announced_from if t.announced_from == t.announced_to else f"{t.announced_from} and {t.announced_to}"
        what += f", announced {'on' if t.announced_from == t.announced_to else 'between'} {when}"
    return what


def fiscal_year_end(fye: str | None) -> tuple[int, int] | None:
    """'0927' -> (9, 27); '1231' -> (12, 31); None or malformed -> None."""
    if not fye or not re.fullmatch(r"\d{4}", fye):
        return None
    m, d = int(fye[:2]), int(fye[2:])
    return (m, d) if 1 <= m <= 12 else None


def period_end(t: Target, fye: str | None) -> tuple[date, int | None, str] | None:
    """The nominal end of the asked period, its length in months (None for a balance sheet date or an unknown length) and
    how it was worked out. None when it cannot be worked out without a guess."""
    if t.end:
        return date.fromisoformat(t.end), (None if t.span == "instant" else t.months), "stated in the question"
    if t.span == "month" and t.month:
        y, m = map(int, t.month.split("-"))
        return _month_end(y, m), None, "the month the question names"
    if not t.fiscal_year or t.span in ("ytd", "ttm", "months"):
        return None                                      # year to date of a year: its newest year-to-date column, chosen later
    if t.basis == "calendar":
        fy_month, how = 12, "calendar year"
    elif (parsed := fiscal_year_end(fye)) is not None:
        fy_month, how = parsed[0], f"the company's fiscal year end ({fye[:2]}-{fye[2:]}), a fiscal year named by the year it ends in"
    elif t.basis == "fiscal":
        return None                                      # 'fiscal' with no known year end: never guessed
    else:
        fy_month, how = 12, "no fiscal year end known: the calendar year"
    year_end = _month_end(t.fiscal_year, fy_month)
    back = {"quarter": 3 * (4 - (t.quarter or 4)), "half": 6 * (2 - (t.half or 2))}.get(t.span or "", 0)
    y, m = year_end.year, year_end.month - back
    while m <= 0:
        m, y = m + 12, y - 1
    months = {"quarter": 3, "half": 6, "full_year": 12}.get(t.span or "", t.months)
    return _month_end(y, m), months, how


def near(a: str | date | None, b: date, days: int = TOLERANCE_DAYS) -> bool:
    if not a:
        return False
    d = a if isinstance(a, date) else date.fromisoformat(str(a)[:10])
    return abs((d - b).days) <= days


def column_fits(col: dict, end: date, months: int | None) -> bool:
    """A statement column covers the period: it ends near the period's end and, for a duration, runs as many months."""
    if not near(col.get("end"), end):
        return False
    if months is None:
        return True
    return col.get("kind") == "instant" or col.get("months") == months


def in_window(day: str | None, start: str | None, end: str | None) -> bool:
    if not start:
        return True
    if not day:
        return False
    return start <= str(day)[:10] <= (end or "9999-12-31")


def filed_after(end: date, days: int = 120) -> tuple[date, date]:
    """When the statements for a period ending `end` are filed: after it ends, within about four months (a 10-K's deadline)."""
    return end + timedelta(days=1), end + timedelta(days=days)
