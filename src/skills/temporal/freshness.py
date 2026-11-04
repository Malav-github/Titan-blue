"""How old is what an answer rests on? For a question about the current state, an answer whose newest source is old
(or carries no date at all) is flagged, never silently accepted or silently hidden."""

from skills.temporal.clock import Now
from skills.temporal.extract import DateFact, latest_possible, newest_content_date


def assess(sources: dict[str, list[DateFact]], scope: dict, now: Now, max_age_days: int) -> dict:
    """`sources` maps each source an answer rests on to its dates. Returns what the report shows: today, the newest dated
    source and its age, and whether that is too old for a question that asks for the current state."""
    newest: DateFact | None = None
    undated = 0
    for facts in sources.values():
        n = newest_content_date(facts)
        if n is None:
            undated += 1
        elif newest is None or latest_possible(n) > latest_possible(newest):
            newest = n
    applies = bool(scope.get("recent"))
    age = (now.date - latest_possible(newest)).days if newest else None
    return {"today": now.date.isoformat(), "scope": scope.get("intent"), "applies": applies,
            "newest_source_date": newest.value if newest else None, "newest_origin": newest.origin if newest else None,
            "age_days": age, "max_age_days": max_age_days, "undated_sources": undated, "sources": len(sources),
            "stale": bool(applies and sources and (newest is None or age > max_age_days))}


def warning(a: dict) -> str | None:
    """One sentence for the report, or None when there is nothing to flag."""
    if not a["stale"]:
        return None
    if a["newest_source_date"] is None:
        return (f"None of the sources behind this answer carries a date, but the question asks for the current state "
                f"as of {a['today']}; it may be out of date.")
    return (f"The newest dated source behind this answer is from {a['newest_source_date']} ({a['age_days']} days before "
            f"today, {a['today']}); the question asks for the current state, so the answer may be out of date.")
