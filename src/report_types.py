"""Report types (definitions/guidance/report_types.json): what the person asked for (a brief, a general report, a deep
research report, a white paper) decides the plan's sections, the budgets, when a fact is settled, how relevance is judged,
and the deadline the run keeps to.

    choose(report_type, question) -> the type's profile
"""

import json
import re
from functools import lru_cache

from config import PROJECT_ROOT

PATH = PROJECT_ROOT / "definitions" / "guidance" / "report_types.json"


@lru_cache(maxsize=1)
def data() -> dict:
    d = json.loads(PATH.read_text(encoding="utf-8"))
    ids = [t["id"] for t in d["types"]]
    if d["default"] not in ids or len(set(ids)) != len(ids):
        raise ValueError("report_types.json: the default must be one of the types, and ids unique")
    for t in d["types"]:
        if t.get("route_when"):
            re.compile(t["route_when"])
        if t["writing_reserve_seconds"] >= t["deadline_seconds"]:
            raise ValueError(f"report_types.json: {t['id']} leaves no time for research")
        if t["relevance"] not in ("checked", "at_extraction"):
            raise ValueError(f"report_types.json: {t['id']}: relevance is 'checked' or 'at_extraction'")
    return d


def ids() -> list[str]:
    return [t["id"] for t in data()["types"]]


def get(type_id: str) -> dict:
    return next(t for t in data()["types"] if t["id"] == type_id)


def choose(report_type: str | None, question: str = "") -> dict:
    """The person's choice; else the first type whose wording the question uses ('a deep dive', 'white paper'); else the default."""
    if report_type:
        if report_type not in ids():
            raise ValueError(f"unknown report type {report_type!r}; one of {ids()}")
        return get(report_type)
    for t in reversed(data()["types"]):          # the most specific first: 'a deep research report' is deep, not general
        if t.get("route_when") and re.search(t["route_when"], question or "", re.I):
            return get(t["id"])
    return get(data()["default"])


def outline_instruction(t: dict, subject: str) -> str:
    """What the planner is asked for instead of a single-fact checklist, for a report."""
    sec = t["sections"]
    angles = "; ".join(sec["angles"])
    return (f"checklist: this is a {t['label'].lower()}, so the checklist is its OUTLINE: between {sec['min']} and {sec['max']} section "
            f"questions about {subject or 'the subject'}, one per angle, in this order of angles: {angles}. Write each as a short "
            f"question a section of the report answers (for example 'Which AI products did S&P Global launch in 2026, and when?'), "
            f"never an answer or a value. Queries: one for each section at least, each from a different place (the company's own "
            f"releases and filings, wire services, top-tier publishers, industry or analyst sources), never two phrasings of one search.")
