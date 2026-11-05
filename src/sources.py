"""What Titan knows about each data source, kept as data in definitions/sources/*.json so the planner, the router and the tests
all read the same thing (and so it travels with the project, not with anyone's notes).

A profile says what a source covers, what it is weak at, how to query it, and when it is worth offering. The router is
deterministic: a connector is offered for a question only when the question (or its checklist) matches the profile's
`route_when` pattern. A connector without a profile is always offered, so a newly added connector works before it is described.
Web search always runs; routing only decides which connectors are worth a model call.
"""

import json
import re
from functools import lru_cache

from config import PROJECT_ROOT

SOURCES_DIR = PROJECT_ROOT / "definitions" / "sources"
REQUIRED = ("id", "kind", "status", "title", "covers", "query_style", "weak_at", "time_behavior", "evidence_tier", "limits", "route_when")


@lru_cache(maxsize=1)
def profiles() -> dict[str, dict]:
    out = {}
    for path in sorted(SOURCES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if missing := [k for k in REQUIRED if k not in data]:
            raise ValueError(f"{path.name}: missing {', '.join(missing)}")
        if data["id"] != path.stem:
            raise ValueError(f"{path.name}: id {data['id']!r} must match the file name")
        if data["route_when"]:
            re.compile(data["route_when"])
        out[data["id"]] = data
    return out


def route(connector_ids: list[str], question: str, checklist: str = "") -> dict:
    """{"offered": [...], "hidden": [{"id", "reason"}]} for the given active connectors."""
    text = f"{question} {checklist}"
    offered, hidden = [], []
    for cid in connector_ids:
        profile = profiles().get(cid)
        if profile is None or not profile["route_when"]:
            offered.append(cid)
        elif m := re.search(profile["route_when"], text, re.I):
            offered.append(cid)
        else:
            hidden.append({"id": cid, "reason": f"nothing in the question points to {profile['title']}"})
    return {"offered": offered, "hidden": hidden}


def search_advice() -> str:
    """One paragraph for the planner about how to query the web and what it is bad at."""
    web = profiles()["web"]
    return f"{web['query_style']} Web search is weak at: {'; '.join(web['weak_at'])}."


PRIORITY_PATH = PROJECT_ROOT / "definitions" / "guidance" / "source_priority.json"


@lru_cache(maxsize=1)
def priority() -> dict:
    data = json.loads(PRIORITY_PATH.read_text(encoding="utf-8"))
    ranks = [r["rank"] for r in data["ranks"]]
    if ranks != sorted(set(ranks)) or not ranks:
        raise ValueError("source_priority.json: ranks must be unique and in order")
    return data


def priority_advice() -> str:
    """The source ranking as text for the scout's planner call."""
    p = priority()
    lines = [f"{r['rank']}. {r['kind']} ({r['examples']}): {r['why']}" for r in p["ranks"]]
    return "Prefer sources in this order, best first:\n" + "\n".join(lines) + f"\nNever {p['never']}."
