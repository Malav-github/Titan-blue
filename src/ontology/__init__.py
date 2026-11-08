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


def label_key(text: str) -> str:
    """A printed row label reduced for matching: lower case, no note numbers, markers or trailing colons. "Total current
    assets:" , "TOTAL CURRENT ASSETS" and "(b) Total current assets" are one key."""
    t = _norm(text).replace("’", "'").replace("&amp;", "&")
    for _ in range(3):                                                  # "1 (a) Net Sales": serial number, then item letter
        t = re.sub(r"^(?:\(?[a-z]\)|\(?[ivx]{1,4}\)|[ivx]{1,4}\.|\d{1,2}[.)]|\d{1,2}(?=\s+[(a-z])|[-–•:])\s*", "", t)
    t = t.strip(" :;.~=")
    t = re.sub(r"\s*\(?\s*(?:refer )?notes? [\d.]+(?:\s*(?:,|&|and)\s*[\d.]+)*\s*\)?$", "", t)   # (Note 4), Note 4.1
    t = re.sub(r"\s*\(\s*\d+\s*(?:[+\-±]\s*\d+\s*)+\)\s*$", "", t)                           # (1+2), (7-8): how the line is worked out
    t = re.sub(r"\s*/\s*", "/", t)                                                              # "Net Sales/ Income" = "Net Sales / Income"
    return t.strip(" :;.")


@lru_cache(maxsize=1)
def financial_statements() -> dict:
    d = _read("financial_statements.json")
    statements = {s["id"] for s in d["statements"]}
    for s in d["statements"]:
        re.compile(s["title"], re.I)
    for b in d["bases"] + d["standards"]:
        re.compile(b["cues"], re.I)
    re.compile(d["scales"]["whole_units"])
    re.compile(d["periods"]["not_periods"], re.I)
    ids, tags, labels = set(), {}, {}
    for c in d["concepts"]:
        where = f"financial_statements.json: concept {c['id']}"
        if c["id"] in ids:
            raise ValueError(f"{where} appears twice")
        ids.add(c["id"])
        if c["statement"] not in statements or c["kind"] not in d["kinds"]:
            raise ValueError(f"{where}: unknown statement or kind")
        for t in c["tags"]:
            if t in tags:
                raise ValueError(f"{where}: tag {t} is also given to {tags[t]}")
            tags[t] = c["id"]
        for label in c["labels"]:
            key = (c["statement"], label_key(label))
            if key in labels and labels[key] != c["id"]:
                raise ValueError(f"{where}: label '{label}' is also a label of {labels[key]}")
            labels[key] = c["id"]
    for r in d["rules"]:
        names = [r["total"], *(p.lstrip("-?") for parts in [r["parts"], *r.get("or", [])] for p in parts), *r.get("unless", [])]
        if unknown := [n for n in names if n not in ids]:
            raise ValueError(f"financial_statements.json: rule {r['id']} names unknown concepts {unknown}")
    d["_patterns"] = {}
    for cid, pattern in d.get("patterns", {}).items():
        if cid == "note":
            continue
        if cid not in ids:
            raise ValueError(f"financial_statements.json: pattern for unknown concept {cid}")
        d["_patterns"][cid] = re.compile(pattern, re.I)
    d["_by_tag"], d["_by_label"] = tags, labels
    d["_concepts"] = {c["id"]: c for c in d["concepts"]}
    return d


def predicate(pid: str) -> dict:
    return next(p for p in company_events()["predicates"] if p["id"] == pid)


def predicate_ids() -> list[str]:
    return [p["id"] for p in company_events()["predicates"]]
