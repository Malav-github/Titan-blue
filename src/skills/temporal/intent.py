"""The time scope of a question, decided by code.

Is the question about a fixed past fact, a date it names, the current state of things, a reporting period, or a sequence of
events? That decides how to search (recency window, year terms), how to choose sources, and how to judge them. It is a
set of plain rules, not a model call: a small model's idea of "now" is out of date, and the same question must get
the same scope every time.
"""

import re
from dataclasses import dataclass
from datetime import date

from skills.temporal import setting
from skills.temporal.clock import Now

INTENTS = ("historical", "point_in_time", "latest", "period", "timeline")

YEAR = re.compile(r"\b(19[5-9]\d|20\d{2}|21\d{2})\b")
_LATEST = (r"latest|current(?:ly)?|now|at present|presently|ongoing|nowadays|as it stands|today|tonight|recent(?:ly)?|newest|most recent|up[- ]to[- ]date|so far|these days|"
           r"as of now|this (?:year|quarter|month|week|season)|last (?:year|quarter|month|week)|"
           r"past (?:year|quarter|month|week|\d+ (?:days?|weeks?|months?|years?))")
LATEST = re.compile(rf"\b(?:{_LATEST})\b")
ROLE = re.compile(r"\bwho (?:is|are|'s) (?:the )?(?:current )?(?:ceo|cfo|coo|cto|chair(?:man|woman|person)?|president|head|director|leader|owner)\b")
PERIOD = re.compile(
    r"\b(?:fy ?'?\d{2,4}|fiscal(?: year| quarter| \d{4})?|q[1-4]|[1-4]q|h[12]|quarterly|quarter|full[- ]year|half[- ]year|"
    r"year[- ]over[- ]year|yoy|qoq|quarter[- ]over[- ]quarter|annual|earnings|revenue|net sales|net income|profit|ebitda|eps|"
    r"guidance|margin|capex|cash flow)\b")
TIMELINE = re.compile(
    r"\b(?:timeline|chronolog\w*|history of|over time|key events|major events|milestones|year[- ]by[- ]year|evolution of|"
    r"since (?:19|20)\d{2}|between (?:19|20)\d{2} and (?:19|20)\d{2}|from (?:19|20)\d{2} (?:to|until|through) (?:19|20)\d{2})\b")
_BETWEEN = re.compile(r"between ((?:19|20)\d{2}) and ((?:19|20)\d{2})|from ((?:19|20)\d{2}) (?:to|until|through) ((?:19|20)\d{2})")
_SINCE = re.compile(r"since ((?:19|20)\d{2})")


@dataclass(frozen=True)
class TimeScope:
    intent: str                        # one of INTENTS
    recent: bool                       # the answer depends on how recent the source is
    recency: str | None                # day | week | month | year, for the search providers; None when age does not matter
    years: tuple[int, ...]             # years the question names
    window_start: str | None           # ISO dates, when the question names or implies a window
    window_end: str | None
    period_sensitive: bool             # the facts belong to a reporting period (earnings, fiscal year, quarter ...)
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"intent": self.intent, "recent": self.recent, "recency": self.recency, "years": list(self.years),
                "window_start": self.window_start, "window_end": self.window_end,
                "period_sensitive": self.period_sensitive, "reasons": list(self.reasons)}

    def guidance(self, now: Now) -> str:
        """What the planner and the page chooser should keep in mind, in one short paragraph."""
        window = f" ({self.window_start or '...'} to {self.window_end or '...'})" if self.window_start or self.window_end else ""
        text = {
            "historical": "A fixed past fact: the age of a source matters little; prefer authoritative sources.",
            "point_in_time": f"About {', '.join(map(str, self.years))}: prefer sources that state that date or period.",
            "latest": f"Wants the CURRENT state as of today ({now.date}): prefer sources from the last {self.recency or 'year'}; "
                      "older pages may be out of date.",
            "period": "The facts belong to a reporting period (fiscal or calendar year, half, quarter, trailing twelve months, "
                      "year over year). Name the period in the queries and checklist labels, and say whether it is fiscal or calendar.",
            "timeline": f"Wants events in date order{window}: cover the whole window and look for dates, not only the newest items.",
        }[self.intent]
        if self.intent == "period" and self.recent:
            text += f" No period was named, so the latest reported period as of {now.date} is wanted."
        return text


def _recency(q: str) -> str:
    if re.search(r"\b(?:today|tonight|right now|breaking|this morning|(?:past|last) 24 hours)\b", q):
        return "day"
    if re.search(r"\b(?:this week|last week|past week|past [1-7] days)\b", q):
        return "week"
    if re.search(r"\b(?:this month|last month|past month|past [1-3] months?|past \d+ weeks)\b", q):
        return "month"
    return setting("default_recency_for_latest")


def _window(q: str, years: tuple[int, ...], today: date) -> tuple[str | None, str | None]:
    if m := _BETWEEN.search(q):
        first, second = [g for g in m.groups() if g]
        return f"{first}-01-01", f"{second}-12-31"
    if m := _SINCE.search(q):
        return f"{m.group(1)}-01-01", today.isoformat()
    if years:
        return f"{min(years)}-01-01", f"{max(years)}-12-31"
    return None, None


def classify(question: str, now: Now) -> TimeScope:
    q = " ".join(question.lower().split())
    years = tuple(sorted({int(y) for y in YEAR.findall(q)}))
    latest, period, timeline = bool(LATEST.search(q) or ROLE.search(q)), bool(PERIOD.search(q)), bool(TIMELINE.search(q))
    if timeline:
        intent = "timeline"
    elif period:
        intent = "period"
    elif latest:
        intent = "latest"
    elif years:
        intent = "point_in_time"
    else:
        intent = "historical"
    reasons = []
    if latest:
        reasons.append("the question asks for the current or recent state")
    # a period question that names neither a year nor "latest" most plausibly means the latest reported period
    recent = (latest or (intent == "period" and not years)) and not years and intent != "timeline"
    if intent == "period" and not years and not latest:
        reasons.append("no period is named, so the latest reported period is assumed")
    if years:
        reasons.append(f"the question names {', '.join(map(str, years))}")
    start, end = _window(q, years, now.date)
    return TimeScope(intent, recent, _recency(q) if recent else None, years, start, end, period, tuple(reasons))


def apply_year_terms(queries: list[str], scope: TimeScope, now: Now) -> list[str]:
    """For a question about the current state, put today's year in queries that name no year, so results are not the
    ones a model trained earlier would think of as 'latest'. Other questions' queries are left alone."""
    if not scope.recent or scope.years:
        return list(queries)
    return [q if YEAR.search(q) else f"{q} {now.year}" for q in queries]
