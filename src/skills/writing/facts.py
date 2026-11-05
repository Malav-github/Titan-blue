"""The established claims, each with the date its event happened, or the plain fact that it has none.

A page's date is not always the event's date: a Wikipedia article about a company is "published" in 2004 and edited every week,
and says nothing about when an acquisition happened. So every fact carries where its date came from (`basis`):
  event      the claim has a date for the event itself (announced, effective, closed ...) or an "as of" date
  stated     the claim's own words name the date or year
  page       a news or press page dated that day: the best available stand-in for when it was announced
  reference  a reference page that is edited continuously: its date means nothing about the event, so the fact is undated
  none       no date at all
"""

import re
from dataclasses import dataclass, field
from datetime import date as Date
from urllib.parse import urlparse

from skills.writing import setting

_ISO = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")
_MONTHS = ("january february march april may june july august september october november december").split()
_MONTH_RE = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
_STATED = re.compile(rf"\b(?:(?P<d1>\d{{1,2}})\s+)?(?P<m>{_MONTH_RE})\s*(?:(?P<d2>\d{{1,2}}),?\s+)?(?P<y>(?:19|20)\d{{2}})\b", re.I)
_YEAR = re.compile(r"\b(?:in|since|during|by|of|from|until)\s+((?:19|20)\d{2})\b", re.I)


@dataclass
class Fact:
    claim_id: int
    text: str
    value: str = ""
    quote: str = ""
    date: Date | None = None
    precision: str = ""          # day | month | year | ''
    basis: str = "none"          # event | stated | page | reference | none
    origin: str = ""             # what the date was read from, in words
    tier: str | None = None
    corroboration: str | None = None
    site: str = ""
    fact: int | None = None      # index of the needed fact this claim answers
    periods: list[str] = field(default_factory=list)

    @property
    def dated(self) -> bool:
        return self.date is not None

    @property
    def ordinal(self) -> int | None:
        """Days since year 1; a year-only date counts as the middle of the year, a month-only date as its 15th."""
        if not self.date:
            return None
        if self.precision == "year":
            return Date(self.date.year, 7, 1).toordinal()
        if self.precision == "month":
            return Date(self.date.year, self.date.month, 15).toordinal()
        return self.date.toordinal()

    def human(self) -> str:
        """'27 October 2026', 'October 2026' or '2026': how a person writes the date."""
        if not self.date:
            return ""
        month = _MONTHS[self.date.month - 1].capitalize()
        return {"year": str(self.date.year), "month": f"{month} {self.date.year}"}.get(self.precision, f"{self.date.day} {month} {self.date.year}")

    def iso(self) -> str:
        if not self.date:
            return ""
        return {"year": f"{self.date.year}", "month": f"{self.date.year}-{self.date.month:02d}"}.get(self.precision, self.date.isoformat())


def parse_date(value: str | None) -> tuple[Date | None, str]:
    """An ISO date or timestamp, or a date written in words, as (date, precision). (None, '') if it is neither."""
    text = (value or "").strip()
    if not text:
        return None, ""
    m = _ISO.match(text)
    if m:
        y, mo, d = int(m.group(1)), m.group(2), m.group(3)
        try:
            if d:
                return Date(y, int(mo), int(d)), "day"
            if mo:
                return Date(y, int(mo), 1), "month"
            return Date(y, 1, 1), "year"
        except ValueError:
            return None, ""
    return _from_words(text)


def _from_words(text: str) -> tuple[Date | None, str]:
    m = _STATED.search(text)
    if not m:
        return None, ""
    month = next(i for i, name in enumerate(_MONTHS, 1) if name.startswith(m.group("m")[:3].lower()))
    day = m.group("d1") or m.group("d2")
    try:
        return (Date(int(m.group("y")), month, int(day)), "day") if day else (Date(int(m.group("y")), month, 1), "month")
    except ValueError:
        return None, ""


def _site(url: str) -> str:
    labels = (urlparse(url).hostname or "").lower().split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else ""


def _is_reference(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in setting("reference_sites"))


def _date_of(row: dict) -> tuple[Date | None, str, str, str]:
    """(date, precision, basis, origin) for one claim row from the report."""
    kinds = setting("event_kinds")
    for t in row.get("times") or []:
        if t.get("kind") in kinds and t.get("value"):
            d, p = parse_date(t["value"])
            if d:
                return d, p, "event", f"{t['kind']} {t['value']}"
    d, p = _from_words(row.get("claim") or "")
    if d:
        return d, p, "stated", "named in the claim"
    m = _YEAR.search(row.get("claim") or "")
    if m:
        return Date(int(m.group(1)), 1, 1), "year", "stated", "year named in the claim"
    if _is_reference(row.get("source_url") or ""):
        return None, "", "reference", "reference page: its date is not the event's date"
    if row.get("source_date") and row.get("source_date_kind") in setting("page_date_kinds"):
        d, p = parse_date(row["source_date"])
        if d:
            return d, p, "page", f"page dated {row['source_date'][:10]}"
    return None, "", "none", ""


def build_facts(rows: list[dict]) -> list[Fact]:
    """Facts from the report's claim rows (`claim_id`, `claim`, `times`, `source_url`, `source_date`, `source_date_kind`...)."""
    out = []
    for r in rows:
        d, p, basis, origin = _date_of(r)
        out.append(Fact(claim_id=r["claim_id"], text=r.get("claim") or "", value=str(r.get("value") or ""), quote=r.get("quote") or "",
                        date=d, precision=p, basis=basis, origin=origin, tier=r.get("source_tier"),
                        corroboration=r.get("corroboration"), site=_site(r.get("source_url") or ""), fact=r.get("fact"),
                        periods=[t["value"] for t in r.get("times") or [] if t.get("kind") == "period"]))
    return out


def apart(a: Fact, b: Fact, window: int | None = None) -> bool | None:
    """Did the two happen at different times? True if so, False if close enough to be one moment, None if either has no date."""
    if not (a.dated and b.dated):
        return None
    window = setting("same_period_days") if window is None else window
    coarse = "year" in (a.precision, b.precision)
    if coarse:
        return a.date.year != b.date.year
    return abs(a.ordinal - b.ordinal) > window


def groups(facts: list[Fact], window: int | None = None) -> list[list[Fact]]:
    """Dated facts into moments, newest first (facts within the window of their neighbour share a moment); undated ones last, alone."""
    window = setting("same_period_days") if window is None else window
    dated = sorted((f for f in facts if f.dated), key=lambda f: (-f.ordinal, f.claim_id))
    moments: list[list[Fact]] = []
    for f in dated:
        if moments and apart(moments[-1][-1], f, window) is False:
            moments[-1].append(f)
        else:
            moments.append([f])
    undated = [f for f in facts if not f.dated]
    return moments + ([undated] if undated else [])


def age_text(f: Fact, today: Date) -> str:
    if not f.date:
        return ""
    days = (today - Date(f.date.year, f.date.month, f.date.day)).days
    if f.precision == "year":
        years = today.year - f.date.year
        return "this year" if years == 0 else f"{years} year{'s' if years != 1 else ''} before today"
    if days < 0:
        return f"{-days} days after today"
    if days < 60:
        return f"{days} day{'s' if days != 1 else ''} before today"
    if days < 730:
        return f"about {round(days / 30.4)} months before today"
    return f"about {round(days / 365.25)} years before today"
