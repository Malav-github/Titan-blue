"""Titan's ontologies (definitions/ontology/): what a source is (sources.json) and what kinds of company event are recorded
(company_events.json). Both are read by code. Loading validates them, so a broken file fails loudly at start-up rather than
mislabelling sources in a run."""

import json
import re
from functools import lru_cache

from config import PROJECT_ROOT

DIR = PROJECT_ROOT / "definitions" / "ontology"


def _read(name: str) -> dict:
    return json.loads((DIR / name).read_text(encoding="utf-8"))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


@lru_cache(maxsize=1)
def sources() -> dict:
    d = _read("sources.json")
    classes = {c["id"] for c in d["classes"]}
    venues = {v["id"] for v in d["venues"]}
    for v in d["venues"]:
        if v["class"] not in classes:
            raise ValueError(f"sources.json: venue {v['id']}: unknown class {v['class']}")
        if v.get("path"):
            re.compile(v["path"])
        for h in v.get("publishers", {}):
            if h not in v.get("hosts", []):
                raise ValueError(f"sources.json: venue {v['id']}: publisher given for {h}, which is not one of its hosts")
    types = set()
    for t in d["document_types"]:
        if t["id"] in types:
            raise ValueError(f"sources.json: document type {t['id']} appears twice")
        types.add(t["id"])
        if unknown := set(t["venues"]) - venues:
            raise ValueError(f"sources.json: document type {t['id']}: unknown venues {sorted(unknown)}")
        for k in ("text", "title", "url"):
            if t.get(k):
                re.compile(t[k])
    for venue, t in d["default_type"].items():
        if venue not in venues or t not in types:
            raise ValueError(f"sources.json: default_type {venue} -> {t} names an unknown venue or type")
    if venues - set(d["default_type"]):
        raise ValueError(f"sources.json: venues without a default type: {sorted(venues - set(d['default_type']))}")
    for k in ("registrant", "wire_source", "edgar_cik"):
        re.compile(d["issuer"][k])
    return d


@lru_cache(maxsize=1)
def company_events() -> dict:
    d = _read("company_events.json")
    kinds, types = set(d["rule_kinds"]), set(d["field_types"])
    groups = {g["id"] for g in d["groups"]}
    ids = [p["id"] for p in d["predicates"]]
    if len(ids) != len(set(ids)):
        raise ValueError("company_events.json: a predicate id appears twice")
    if "other.fact" not in ids:
        raise ValueError("company_events.json: the fallback predicate other.fact is missing")
    for p in d["predicates"]:
        where = f"company_events.json: {p['id']}"
        if p["group"] not in groups:
            raise ValueError(f"{where}: unknown group {p['group']}")
        names = [f["name"] for f in p["fields"]]
        for f in p["fields"]:
            if f["type"] not in types:
                raise ValueError(f"{where}: field {f['name']} has unknown type {f['type']}")
        if missing := set(p["key"]) - set(names):
            raise ValueError(f"{where}: key names unknown fields {sorted(missing)}")
        if p["cues"]:
            re.compile(p["cues"], re.I)
        ex = p["example"]
        if unknown := set(ex["fields"]) - set(names):
            raise ValueError(f"{where}: example fills unknown fields {sorted(unknown)}")
        if absent := [f["name"] for f in p["fields"] if f["required"] and f["name"] not in ex["fields"]]:
            raise ValueError(f"{where}: example leaves required fields empty: {absent}")
        if not_quoted := [k for k, v in ex["fields"].items() if _norm(v) not in _norm(ex["quote"])]:
            raise ValueError(f"{where}: example values not word for word in its quote: {not_quoted}")
        for r in p["rules"]:
            if r["kind"] not in kinds:
                raise ValueError(f"{where}: unknown rule kind {r['kind']}")
            if r["kind"] == "order" and r["after"] not in ids:
                raise ValueError(f"{where}: rule refers to unknown predicate {r['after']}")
            for name in r.get("key", []) + ([r["field"]] if r.get("field") else []):
                if name not in names:
                    raise ValueError(f"{where}: rule names unknown field {name}")
    return d


def predicate(pid: str) -> dict:
    return next(p for p in company_events()["predicates"] if p["id"] == pid)


def predicate_ids() -> list[str]:
    return [p["id"] for p in company_events()["predicates"]]
