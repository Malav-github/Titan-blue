"""A common-sense check on the question itself, before any plan is drawn: does it ask for something that cannot exist yet, or that
contradicts itself? Plain rules over the question and today's date, so the same question gets the same concerns every time.

It raises concerns for the person and the planner to see; it never changes the question and never refuses by itself. (A search
that finds nothing reported is the other half of this check: see Runner._screen_premise.)
"""

import re
from datetime import date

from skills.temporal.clock import Now
from skills.temporal.intent import LATEST, TimeScope, YEAR

_EVENT = re.compile(r"\b(?:said|say|says|stated|wrote|written|published|announced|announce|reported|released|filed|delivered|gave|made|"
                    r"launched|acquired|signed|happened|remarks|letter|statement|speech|results|earnings|report|press release|"
                    r"decided|appointed|resigned|raised)\b", re.I)
_FORECAST = re.compile(r"\b(?:forecast\w*|project\w*|expect\w*|predict\w*|target\w*|plans?|planned|outlook|guidance|will|"
                       r"going to|roadmap|by (?:the end of )?(?:19|20)\d{2}|scheduled|upcoming|next)\b", re.I)
_QUARTER = re.compile(r"\bq([1-4])[ -]?(?:fy)?'?((?:19|20)\d{2})\b|\b((?:19|20)\d{2})[ -]?q([1-4])\b", re.I)
_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def check(question: str, scope: TimeScope, now: Now) -> list[dict]:
    """[{id, text}]: things in the question that do not fit today's date or each other."""
    q = " ".join(question.split())
    today = now.local.date()
    concerns: list[dict] = []
    forecast = bool(_FORECAST.search(q))

    future = sorted(y for y in {int(m) for m in YEAR.findall(q)} if y > today.year)
    if future and _EVENT.search(q) and not forecast:
        years = ", ".join(map(str, future))
        concerns.append({"id": "future_year", "text": (
            f"The question names {years}, which is after today ({today.isoformat()}). Something said, published or reported in "
            f"{years} cannot exist yet. Did you mean an earlier year?")})

    if not forecast:
        for m in _QUARTER.finditer(q):
            n, year = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(4)), int(m.group(3)))
            month, day = _QUARTER_END[n]
            end = date(year, month, day)
            if end >= today and year <= today.year + 1:
                concerns.append({"id": "period_not_over", "text": (
                    f"Q{n} {year} ends on {end.isoformat()} (calendar quarters; a company's fiscal quarter may differ), so its "
                    "results are not published yet. The latest reported quarter is the one to ask for.")})
                break

    if LATEST.search(q.lower()) and scope.years and min(scope.years) <= today.year - 2:
        concerns.append({"id": "latest_vs_year", "text": (
            f"The question asks for the latest but also names {min(scope.years)}. Which is meant: the newest available, or "
            f"{min(scope.years)}?")})
    return concerns
