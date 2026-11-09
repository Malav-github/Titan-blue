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


VERDICTS = {
    "verified": "inside a sum that adds up" + "; for a scanned page, both OCR engines read it the same",
    "agreed": "scanned page: both OCR engines read it the same, confidently, but no sum covers this line",
    "read": "text layer or XBRL, not covered by any sum",
    "doubt": "kept, with the reading it is taken as and why, for a person to check before it is relied on",
}


def _coverage(table: Table) -> tuple[dict, dict]:
    covered: dict[tuple[int, int], list[Check]] = {}
    members: dict[int, list] = {}
    for c in table.checks:
        for name in [*c.detail["parts"], *_totals(c.rule)]:
            for r in table.by_concept(name):
                covered.setdefault((r.index, c.column), []).append(c)
                if (v := table.value_at(r.index, c.column)) is not None:
                    members.setdefault(id(c), []).append(v)
    return covered, members


def grade(table: Table) -> None:
    """Give every value a verdict, with reasons, after the checks have run:
      verified  - inside a sum that adds up (and, if scanned, both OCR engines read it the same, confidently)
      agreed    - scanned; both engines read it the same, confidently; no sum covers it
      read      - text layer or XBRL; no sum covers it
      doubt     - the engines disagree, only one read it, either was unsure, its grouping or decimals are wrong for its
                  column, or it is in a sum that does not add up. Said loudly, never used silently.
    A figure in doubt is kept, never dropped: a scanned one is taken as its likeliest reading (`propose`), with why, and
    the checks are run again on those readings."""
    decimals = _column_decimals(table)
    if propose(table, decimals):
        table.checks = run(table)
    covered, members = _coverage(table)
    for v in table.values:
        engines = _ocr_doubts(v.ocr) if v.ocr else []          # disagreement, one engine only, an unsure engine
        reasons = []
        if v.ocr:
            if not valid_grouping(v.amount.raw):
                reasons.append(f"digit grouping '{v.amount.raw}' is neither Western (1,234,567) nor Indian (12,34,567)")
            if (d := decimals.get(v.col)) is not None and v.unit not in ("per_share", "ratio", "shares") and v.amount.decimals != d:
                reasons.append(f"{v.amount.decimals} decimal places where the column prints {d}")
        col = next((c for c in table.columns if c.index == v.col), None)
        if table.primary and table.statement != "equity" and col and col.kind != "change" and not col.end:
            reasons.append(f"its column's period could not be read ('{col.header[:40]}')")
        checks_here = covered.get((v.row, v.col), [])
        reasons += [f"in a sum that does not add up ({c.rule}: printed {c.expected}, parts give {c.computed})"
                    for c in checks_here if c.verdict == "fail"]
        # A sum that adds up settles one unconfirmed figure in it: with every other figure read the same by both engines,
        # this one is the only unknown, and the arithmetic fixes it. Two unconfirmed figures could hide each other's error.
        settled_by = next((c for c in checks_here if c.verdict == "pass"
                           and all(o is v or not o.ocr or (o.ocr.get("agree") and not _ocr_doubts(o.ocr)) for o in members.get(id(c), []))), None)
        if v.proposed and v.proposed["changed"]:
            # read otherwise than the layout engine printed it (the other engine's reading, or a repair): however well
            # it adds up, a person confirms it
            reasons.append(f"taken as {v.proposed['reading']} (printed '{v.proposed['printed']}'): {v.proposed['why']}")
        v.notes = []
        if engines and settled_by and not reasons:
            v.notes = engines + [f"settled by arithmetic ({settled_by.rule} adds up, every other figure in it read the same by both engines)"]
        elif engines:
            reasons = engines + reasons
        v.reasons = reasons
        v.verdict = ("doubt" if reasons else "verified" if any(c.verdict == "pass" for c in checks_here)
                     else "agreed" if v.ocr else "read")
        if v.verdict != "doubt":
            v.proposed = None


def propose(table: Table, decimals: dict[int, int] | None = None) -> bool:
    """For every scanned figure with an OCR problem, the reading it is taken as, from what the engines printed and what the
    column prints, in this order:
      1. a reading with which every sum the figure is in adds up (when one of them fails as read);
      2. a reading that fits the column: valid grouping and the column's decimal places. Either engine's reading, or one
         repaired by the column's format: "7.316.94" in a column printing two decimals is 7,316.94, the last separator
         being the decimal point;
      3. otherwise RapidOCR's reading as it stands (the stronger engine on scans).
    The figure's amount becomes that reading; `v.proposed` says which, why, and what was printed. Returns whether any
    amount changed (the checks must then run again)."""
    from .ocr import _norm
    decimals = _column_decimals(table) if decimals is None else decimals
    covered, _ = _coverage(table)
    changed = False
    for v in table.values:
        if not v.ocr or not _needs_reading(v, decimals.get(v.col)):
            continue
        d = None if v.unit in ("per_share", "ratio", "shares") else decimals.get(v.col)
        cands = _candidates(v, d, _norm)
        if not cands:
            continue
        here = [(c.rule, c.column) for c in covered.get((v.row, v.col), [])]
        failing = any(c.verdict == "fail" for c in covered.get((v.row, v.col), []))
        chosen, confirmed = None, []
        if failing:
            original = v.amount
            for cand in cands:
                v.amount = cand["amount"]
                after = {(c.rule, c.column): c for c in run(table) if (c.rule, c.column) in here}
                if after and all(c.verdict == "pass" for c in after.values()):
                    chosen, confirmed = cand, sorted({r for r, _ in after})
                    break
            v.amount = original
        if chosen is None:
            chosen = next((c for c in cands if c["fits"]), cands[0])
        why = chosen["why"]
        if confirmed:
            why += f"; with it {', '.join(confirmed)} add{'s' if len(confirmed) == 1 else ''} up"
        elif failing:
            why += "; no reading makes its sums add up"
        same = (chosen["amount"].value, chosen["amount"].decimals) == (v.amount.value, v.amount.decimals)
        v.proposed = {"reading": chosen["text"], "kind": chosen["kind"], "why": why, "printed": v.amount.raw,
                      "confirmed_by": confirmed, "changed": not same}
        if not same:
            _set_amount(table, v, chosen["amount"])
            changed = True
    return changed


def _needs_reading(v, d: int | None) -> bool:
    return bool(_ocr_doubts(v.ocr)) or not valid_grouping(v.amount.raw) or (
        d is not None and v.unit not in ("per_share", "ratio", "shares") and v.amount.decimals != d)


def _candidates(v, d: int | None, norm) -> list[dict]:
    """Readings of one figure, best first: RapidOCR's, Tesseract's, then each repaired to the column's format. A reading
    `fits` when its grouping is valid and its decimals are the column's."""
    from .numbers import Amount, parse_amount
    out, seen = [], set()

    def add(text: str, kind: str, why: str, raw: str):
        a = parse_amount(text)
        if a is None or (a.value, a.decimals) in seen:
            return
        seen.add((a.value, a.decimals))
        fits = valid_grouping(text) and (d is None or a.decimals == d)
        # an engine's reading is what was printed; a repair keeps what was printed and changes only how it is read
        out.append({"text": text, "kind": kind, "why": why, "fits": fits,
                    "amount": Amount(a.value, a.decimals, a.negative, raw if kind == "repair" else text, a.percent)})

    readings = [(e, norm(v.ocr[e])) for e in ("rapidocr", "tesseract") if v.ocr.get(e) and "?" not in v.ocr[e]]
    agree = len({t for _, t in readings}) == 1 and len(readings) == 2
    for engine, text in readings:
        add(text, "engine", "both engines read it so" if agree else f"{engine}'s reading", text)
    for engine, text in readings:
        if (rep := repair_separators(text, d)) and rep != text:
            why = (f"'{text}' ({engine}) is not a figure as printed: no market groups digits so" if d is None else
                   f"'{text}' ({engine}) does not fit its column, which prints {d} decimal place{'s' if d != 1 else ''}")
            add(rep, "repair", why + (", so the last separator is the decimal point and the others group digits" if d != 0
                                      else ", so every separator groups digits"), text)
    return sorted(out, key=lambda c: (not c["fits"], c["kind"] == "repair"))


def repair_separators(text: str, d: int | None) -> str | None:
    """A figure whose separators were misread, rewritten in the column's format, or None when that is not possible:
    "7.316.94" with 2 decimals -> "7,316.94"; "1.234" with 0 decimals -> "1,234"; "12,34,567.8" with 2 -> None (the digits
    after the last separator do not fit). Brackets and signs are kept. With the column's format unknown (`d` None), only a
    figure no market groups so is repaired: one separator printed twice or more with one or two digits after the last
    ("7.316.94"), which can only be grouping followed by decimals."""
    import re
    m = re.search(r"\d[\d.,]*\d", text)
    if not m or not re.search(r"[.,]", m[0]):
        return None
    core = m[0]
    groups = re.split(r"[.,]", core)
    if d is None:
        if len(groups) < 3 or len(groups[-1]) not in (1, 2):
            return None
        d = len(groups[-1])
    if d > 0:
        if len(groups) < 2 or len(groups[-1]) != d:
            return None
        whole, frac = groups[:-1], groups[-1]
        fixed = ",".join(whole) + "." + frac
    else:
        fixed = ",".join(groups)
    fixed_whole = fixed.split(".")[0]
    if not valid_grouping(fixed_whole):
        return None
    return text[:m.start()] + fixed + text[m.end():]


def _set_amount(table: Table, v, a) -> None:
    v.amount = a
    if v.unit in ("per_share", "ratio") or a.percent:
        v.full = a.value
    elif table.scale and table.scale.factor is not None:
        v.full = a.value * table.scale.factor
    else:
        v.full = None


def _totals(rule_id: str) -> list[str]:
    from ontology import financial_statements
    return [r["total"] for r in financial_statements()["rules"] if r["id"] == rule_id]


def _ocr_doubts(o: dict) -> list[str]:
    from .ocr import MIN_CONFIDENCE
    out = []
    if o.get("note"):
        out.append(o["note"])
    t, r = o.get("tesseract"), o.get("rapidocr")
    if t is None or r is None:
        out.append(f"read by one OCR engine only ({'tesseract' if t else 'rapidocr'}: '{t or r}')")
    elif not o.get("agree"):
        out.append(f"the OCR engines disagree (tesseract '{t}', rapidocr '{r}')")
    for engine, floor in MIN_CONFIDENCE.items():
        conf = (o.get("conf") or {}).get(engine)
        if conf is not None and conf < floor:
            out.append(f"{engine} unsure ({conf:.2f})")
    return out


def valid_grouping(text: str) -> bool:
    """1,234,567 (Western) and 12,34,567 (Indian) are valid; 1,2345 or 12,345,67 are not (a lost or extra digit)."""
    import re
    t = re.sub(r"[()\s$₹₱%+\-−]|Rs\.?", "", text)
    if re.fullmatch(r"\d+([.,])\d+(?:\1\d+)+", t) and len(re.split(r"[.,]", t)[-1]) != 3:
        return False            # "7.316.94": one separator twice is grouping, and grouping ends in three digits
    whole = re.split(r"\.(?=\d+$)", t)[0]
    if "," not in whole:
        return True
    groups = whole.split(",")
    western = 1 <= len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:])
    indian = len(groups[-1]) == 3 and 1 <= len(groups[0]) <= 2 and all(len(g) == 2 for g in groups[1:-1])
    return western or indian


def _column_decimals(table: Table) -> dict[int, int]:
    """The decimal places a column prints for amounts, when at least three quarters of its amounts agree."""
    out = {}
    for col in table.columns:
        ds = [v.amount.decimals for v in table.values if v.col == col.index and v.unit not in ("per_share", "ratio", "shares")
              and v.amount.value != 0]
        if len(ds) >= 4:
            top = max(set(ds), key=ds.count)
            if ds.count(top) >= 0.75 * len(ds):
                out[col.index] = top
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
