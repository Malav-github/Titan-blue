"""Arithmetic a statement must satisfy, checked by code on every column: assets equal liabilities plus equity, the parts of a
total add up to it, cash at the end equals cash at the start plus the change. A misread cell, a lost bracket or a wrong scale
fails these loudly.

Verdicts: `pass` (the numbers add up within rounding), `fail` (they do not), and nothing at all when a rule cannot run because
the table lacks one of its line items: a rule that did not run is never reported as passed.

Rounding: each printed number is off by at most half its last digit, so a total of n printed parts may differ from their
sum by up to (n + 1) / 2 of the last digit.
"""

from dataclasses import dataclass
from decimal import Decimal
from itertools import product

from ontology import financial_statements

from .tables import Table


@dataclass
class Check:
    rule: str
    column: int
    verdict: str            # pass | fail
    expected: str           # the total as printed
    computed: str           # the sum of the parts, as printed
    detail: dict

    def as_row(self) -> dict:
        return {"rule": self.rule, "column": self.column, "verdict": self.verdict, "expected": self.expected,
                "computed": self.computed, "detail": self.detail}


def _printed(table: Table, concept: str, col: int) -> tuple[Decimal, int] | None:
    rows = table.by_concept(concept)
    vals = {(v.amount.value, v.amount.decimals) for r in rows if (v := table.value_at(r.index, col))}
    return next(iter(vals)) if len(vals) == 1 else None      # two different printed values for one concept: do not choose


def run(table: Table) -> list[Check]:
    out = []
    for rule in financial_statements()["rules"]:
        if rule["statement"] != table.statement and not (rule["statement"] == "income" and table.statement == "comprehensive_income"):
            continue
        if any(table.by_concept(u) for u in rule.get("unless", [])):
            continue
        for col in table.columns:
            if col.kind == "change":
                continue
            total = _printed(table, rule["total"], col.index)
            if total is None:
                continue
            tried = []
            for way in [rule["parts"], *rule.get("or", [])]:
                parts = _parts(table, way, col.index)
                if parts:
                    tried.append(_check(rule["id"], col.index, total, parts))
            if tried:
                # one presentation that adds up is enough; when none does, the closest one is reported
                out.append(next((c for c in tried if c.verdict == "pass"), min(tried, key=lambda c: Decimal(c.detail["difference"]))))
    return out


def _parts(table: Table, way: list[str], col: int) -> list | None:
    parts = []
    for p in way:
        optional, minus, name = p.startswith("?"), p.startswith("-"), p.lstrip("-?")
        v = _printed(table, name, col)
        if v is None:
            if not optional:
                return None
            continue
        parts.append((name, minus, v))
    return parts or None


def _check(rule: str, col: int, total: tuple[Decimal, int], parts: list) -> Check:
    places = max([total[1]] + [p[2][1] for p in parts])
    tolerance = Decimal(len(parts) + 1) / 2 * Decimal(1).scaleb(-places)
    # A subtracted part may be printed as a positive expense or already in brackets: try both readings, keep the one used.
    readings = list(product(*[((-v[0], "subtracted"), (v[0], "printed negative")) if minus and v[0] < 0
                              else ((-v[0], "subtracted"),) if minus else ((v[0], "added"),) for _, minus, v in parts]))
    best = None
    for r in readings:
        s = sum(x for x, _ in r)
        diff = abs(s - total[0])
        if best is None or diff < best[0]:
            best = (diff, s, r)
    diff, computed, reading = best
    return Check(rule, col, "pass" if diff <= tolerance else "fail", str(total[0]), str(computed),
                 {"parts": {name: how for (name, _, _), (_, how) in zip(parts, reading)},
                  "difference": str(diff), "tolerance": str(tolerance)})
