"""Why parts of a run failed, from the run's own log (definitions/guidance/failure_causes.json): each failure is named by its
kind (the local model's capability, a source, a search provider, Titan's own rules, the question), with what Titan already
does about it and what could be done next. Read by code; nothing here asks a model."""

import json
import re
from functools import lru_cache

from config import PROJECT_ROOT

PATH = PROJECT_ROOT / "definitions" / "guidance" / "failure_causes.json"


@lru_cache(maxsize=1)
def causes() -> dict:
    d = json.loads(PATH.read_text(encoding="utf-8"))
    for c in d["causes"]:
        if c["kind"] not in d["kinds"]:
            raise ValueError(f"failure_causes.json: {c['id']}: unknown kind {c['kind']}")
        if c.get("match"):
            re.compile(c["match"], re.I)
    return d


def _detail(e: dict) -> str:
    p = e["payload"]
    for key in ("reason", "error", "url", "why"):
        if p.get(key):
            return f"{p.get('claim', '')[:80] + ' | ' if p.get('claim') else ''}{str(p[key])[:160]}"
    return json.dumps(p, default=str)[:160]


def diagnose(events: list[dict]) -> list[dict]:
    """[{id, kind, kind_label, label, count, examples, handled_by, workaround}], model limits first."""
    d = causes()
    found: dict[str, dict] = {}
    for e in events:
        for c in d["causes"]:
            if e["type"] != c["event"]:
                continue
            if c.get("field"):
                items = [x.get("summary") or json.dumps(x, default=str) if isinstance(x, dict) else str(x)
                         for x in e["payload"].get(c["field"]) or []]
                if not items:
                    continue
                examples, n = items, len(items)
            else:
                text = json.dumps(e["payload"], default=str)
                if c.get("match") and not re.search(c["match"], text, re.I):
                    continue
                examples, n = [_detail(e)], 1
            entry = found.setdefault(c["id"], {"id": c["id"], "kind": c["kind"], "kind_label": d["kinds"][c["kind"]], "label": c["label"],
                                               "count": 0, "examples": [], "handled_by": c["handled_by"], "workaround": c["workaround"]})
            entry["count"] += n
            for x in examples:
                if x not in entry["examples"] and len(entry["examples"]) < 3:
                    entry["examples"].append(x)
            break
    order = list(d["kinds"])
    return sorted(found.values(), key=lambda x: (order.index(x["kind"]), -x["count"]))
