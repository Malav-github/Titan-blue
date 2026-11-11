"""The planning skill: before anything is searched, work out what kind of information a question wants, which source types and
document types hold it (best first), what time the answer should come from, and which metadata to expect.

It is planning only. It never searches, reads a page or judges evidence; it hands the planner a short brief and checks that the
queries the planner wrote actually reach the best places. Everything it knows is data in definitions/ (source_catalog.json,
filing_types.json, skills/planning.json), so a new source or form type is a data edit. Rules are plain code, not a model call:
the same question gets the same brief every time.
"""

import json
import re
from functools import lru_cache

from config import PROJECT_ROOT

MANIFEST_PATH = PROJECT_ROOT / "definitions" / "skills" / "planning.json"
CATALOG_PATH = PROJECT_ROOT / "definitions" / "guidance" / "source_catalog.json"
FILINGS_PATH = PROJECT_ROOT / "definitions" / "guidance" / "filing_types.json"


@lru_cache(maxsize=1)
def manifest() -> dict:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for key in ("name", "version", "settings"):
        if key not in data:
            raise ValueError(f"{MANIFEST_PATH.name}: {key} is missing")
    return data


def setting(name: str):
    return manifest()["settings"][name]


@lru_cache(maxsize=1)
def catalog() -> list[dict]:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    for t in data["types"]:
        for key in ("id", "label", "route_when", "places", "metadata"):
            if key not in t:
                raise ValueError(f"source_catalog.json: {t.get('id', '?')} is missing {key}")
        re.compile(t["route_when"])
        for p in t["places"]:
            if p.get("role") not in ("primary", "keeps_original", "secondary"):
                raise ValueError(f"source_catalog.json: {t['id']}/{p.get('site')}: role must be primary, keeps_original or secondary")
    return data["types"]


@lru_cache(maxsize=1)
def documents() -> list[dict]:
    data = json.loads(FILINGS_PATH.read_text(encoding="utf-8"))
    for d in data["documents"]:
        for key in ("id", "label", "route_when", "holds", "period_kind", "lag_days", "metadata"):
            if key not in d:
                raise ValueError(f"filing_types.json: {d.get('id', '?')} is missing {key}")
        if d["period_kind"] not in ("year", "quarter", "event"):
            raise ValueError(f"filing_types.json: {d['id']}: period_kind must be year, quarter or event")
        re.compile(d["route_when"])
    return data["documents"]


from skills.planning.brief import PlanningBrief, build, ensure_sources  # noqa: E402,F401
from skills.planning.queries import diversify, site_rules, the_subject  # noqa: E402,F401
from skills.planning.enumerate import enumeration  # noqa: E402,F401
