"""Figures in prose checked against the financial statements read by code: the statements serve as an index of the
company's reported numbers, and a sentence ("Revenue rose 9% to $3.68 billion in the third quarter of 2026") is checked
against it. No model takes part.

    mentions(text, context) -> [Mention]     every reported figure a sentence names: amount, line item, period
    check(text, figures, context) -> [dict]  each mention against the statement figures of the same company

Each result says what was compared and how it came out:
    matches            equal to a statement figure at the precision the sentence states ($3.68 billion = 3,678 million)
    matches_doubtful   equal only to a figure the statements mark doubt (a scanned figure to double-check)
    differs            the statements give the same line item for the same period, and it is not this
    not_covered        the statements read do not give that line item for that period
    found              no period in the sentence: the statement figures it equals, and their periods
    not_found          no period in the sentence, and no statement figure equals it
    not_compared       a different measure (adjusted, segment, EBITDA), a forecast, another currency, or a scale not stated

What the sentence leaves open stays open: a period it does not name is not assumed, a fiscal quarter is placed only from the
company's own financial year (read from its statements), and how it was placed is said. The words are in
definitions/ontology/financial_statements.json under "mentions".
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from ontology import financial_statements
from skills.temporal.periods import DATE, Period, _day, find_periods

VERSION = "figures-v1"

_SCALE = {"thousand": 3, "k": 3, "million": 6, "millions": 6, "mn": 6, "mln": 6, "m": 6, "billion": 9, "billions": 9, "bn": 9,
          "b": 9, "trillion": 12, "tn": 12, "lakh": 5, "lakhs": 5, "lac": 5, "lacs": 5, "crore": 7, "crores": 7, "cr": 7}
_CURRENCY = {"$": "USD", "us$": "USD", "usd": "USD", "dollars": "USD", "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR",
             "rupees": "INR", "₱": "PHP", "php": "PHP", "p": "PHP", "pesos": "PHP"}
_AMOUNT = re.compile(
    r"(?<![\w.])(?P<cur>US\$|\$|₹|Rs\.?|INR|₱|PHP|Php|P(?=\s?\d))?\s?"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<scale>thousand|millions?|mn|mln|billions?|bn|trillion|tn|lakhs?|lacs?|crores?|cr\.?|[mbk])(?![a-z]))?"
    r"(?:\s(?P<cur2>dollars|rupees|pesos))?"
    r"(?P<pct>\s?%|\s?percent\b|\s?per\s?cent\b)?", re.I)
_CHANGE_WORDS = re.compile(r"\b(?P<w>up|down|increased?|decreased?|grew|grow(?:th|ing)?|rose|rise|fell|fall|declined?|higher|lower|"
                           r"jump(?:ed)?|surged?|dropped|gain(?:ed)?)\b", re.I)
_DOWN = {"down", "decrease", "decreased", "fell", "fall", "decline", "declined", "lower", "dropped"}
_QOQ = re.compile(r"\b(?:quarter[- ]over[- ]quarter|sequential(?:ly)?|qoq|q/q)\b", re.I)
_AS_OF = re.compile(rf"\b(?:as\s+(?:of|at|on)|at)\s+(?P<d>{DATE})", re.I)
_BASIS = re.compile(r"\b(consolidated|stand-?alone)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"“])")


@dataclass
class Mention:
    raw: str                         # the figure as written ("$3.68 billion")
    start: int
    end: int
    sentence: str
    phrase: str = ""                 # the words that name the line item
    concepts: list = field(default_factory=list)
    value: Decimal | None = None     # in units (the scale word applied); for a change, the percentage
    half: Decimal = Decimal(0)       # half the last digit written: the sentence's own rounding
    currency: str | None = None
    change: str | None = None        # yoy | qoq for a percentage change ("up 9%"); None for an amount
    periods: list = field(default_factory=list)       # Period, from the sentence (or the context, said so)
    period_origin: str = ""          # sentence | context | ''
    as_of: str | None = None         # a balance date ("as of September 30, 2026")
    basis: str | None = None
    not_compared: str = ""           # why it is not compared, when it is not

    def as_dict(self) -> dict:
        return {"raw": self.raw, "phrase": self.phrase, "concepts": self.concepts, "value": str(self.value) if self.value is not None else None,
                "currency": self.currency, "change": self.change, "periods": [p.label for p in self.periods],
                "period_origin": self.period_origin, "as_of": self.as_of, "basis": self.basis}


def _phrases() -> list[tuple[re.Pattern, str, list[str]]]:
    m = financial_statements()["_mentions"]
    out = []
    for p, concepts in sorted(m["phrases"].items(), key=lambda kv: -len(kv[0])):
        out.append((re.compile(rf"(?<![\w-]){re.escape(p)}(?![\w-])", re.I), p, concepts))
    return out


def _named(sentence: str) -> list[tuple[int, int, str, list[str]]]:
    """Line items named in a sentence, longest phrase first, never overlapping."""
    spans: list[tuple[int, int, str, list[str]]] = []
    for regex, p, concepts in _phrases():
        for m in regex.finditer(sentence):
            if not any(a < m.end() and m.start() < b for a, b, *_ in spans):
                spans.append((m.start(), m.end(), p, concepts))
    return sorted(spans)


def _sentences(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for part in _SENTENCE.split(text):
        i = text.find(part, pos)
        out.append((i, part))
        pos = i + len(part)
    return out


def mentions(text: str, context: str = "") -> list[Mention]:
    """Every figure in `text` that names a line item: amounts with a currency or a scale word, and percentage changes
    ("up 9%"). A bare number (a year, a count) is not a figure. `context` (the text around a quote) supplies the period
    only when the sentence names none, and the result says so."""
    onto = financial_statements()["_mentions"]
    found: list[Mention] = []
    context_periods = find_periods(context) if context else []
    for offset, sentence in _sentences(text):
        named = _named(sentence)
        amounts = []
        for m in _AMOUNT.finditer(sentence):
            pct = bool(m["pct"])
            if not (m["cur"] or m["scale"] or m["cur2"] or pct):
                continue                                  # 2026, 12 stores: not a reported amount
            if m["scale"] and len(m["scale"]) == 1 and not m["cur"]:
                continue                                  # "3m" without a currency is too loose to read
            amounts.append((m, pct))
        money = [m.start() for m, pct in amounts if not pct]
        periods = find_periods(sentence)
        as_of = next((d for x in _AS_OF.finditer(sentence) if (d := _day(x["d"]))), None)
        basis = (b := _BASIS.search(sentence)) and ("standalone" if b[1].lower().startswith("stand") else "consolidated")
        spans = _period_spans(sentence, periods)
        previous: Mention | None = None
        for k, (m, pct) in enumerate(amounts):
            mention = Mention(m[0].strip(), offset + m.start(), offset + m.end(), sentence, as_of=as_of, basis=basis)
            nxt = amounts[k + 1][0].start() if k + 1 < len(amounts) else len(sentence)
            # the line item: the nearest one named before the figure, with no other amount between them (a percentage
            # may sit between: "revenue rose 9% to $3.68 billion"), or named right after it ("$3.68 billion in revenue")
            before = [s for s in named if s[1] <= m.start() and (pct or not any(s[1] <= a < m.start() for a in money))]
            after = [s for s in named if s[0] >= m.end() and re.fullmatch(r"\s*(?:in|of)\s+(?:its\s+|total\s+)?", sentence[m.end():s[0]])]
            span = after[0] if after and not pct else before[-1] if before else None
            if span is None and not pct and previous is not None and previous.change is None and _AGAINST.search(
                    sentence[previous.end - offset:m.start()]):
                # "₹414 crore in Q2 FY26 against ₹368 crore in Q2 FY25": the same line item, another period
                span = (0, 0, previous.phrase, previous.concepts)
            if span is None:
                continue
            mention.phrase, mention.concepts = span[2], span[3]
            window = sentence[max(0, min(span[0], m.start()) - 30):max(span[1], m.end())] if span[1] else m[0]
            if q := onto["qualifiers"].search(window):
                mention.not_compared = f"a different measure ('{q[0]}'), not the statement line item"
            elif f := onto["forward"].search(sentence):
                mention.not_compared = f"about the future ('{f[0]}'), not a reported figure"
            digits = m["num"].replace(",", "")
            places = len(digits.split(".")[1]) if "." in digits else 0
            if pct:
                w = [x for x in _CHANGE_WORDS.finditer(sentence) if abs(x.start() - m.start()) < 40]
                if not w:
                    continue                              # a margin or a share, not a change: not checked
                down = any(x["w"].lower() in _DOWN for x in w)
                mention.value = Decimal(digits) * (-1 if down else 1)
                mention.change = "qoq" if _QOQ.search(sentence) else "yoy"
                mention.half = Decimal(5).scaleb(-places - 1)
            else:
                exp = _SCALE.get((m["scale"] or "").lower().rstrip("."), 0)
                mention.value = Decimal(digits).scaleb(exp)
                mention.half = Decimal(5).scaleb(exp - places - 1)
                cur = (m["cur"] or m["cur2"] or "").lower().strip()
                mention.currency = _CURRENCY.get(cur) or (None if not cur else _CURRENCY.get(cur.rstrip(".")))
            if previous is not None and previous.periods and _YEAR_EARLIER.match(sentence[m.end():nxt]):
                # "compared with ₹73.17 crore a year earlier": the period of the figure it is set against, a year back
                mention.periods, mention.period_origin = [year_before(p) for p in previous.periods], "sentence"
            elif periods:
                mention.periods, mention.period_origin = _period_for(spans, m.start(), m.end(), nxt, previous), "sentence"
            elif context_periods and not as_of:
                mention.periods, mention.period_origin = context_periods, "context"
            found.append(mention)
            previous = mention
    return found


_YEAR_EARLIER = re.compile(r"[^.;]{0,25}?\b(?:a year (?:earlier|ago|before)|in the (?:year-ago|prior[- ]year|same) (?:quarter|period)|"
                           r"(?:last|the previous|the prior) year|in the corresponding (?:quarter|period) (?:of )?(?:the )?(?:previous|last|prior) year)\b", re.I)


def year_before(p: Period) -> Period:
    """The same period one year earlier: "Q2 FY26" -> "Q2 FY25", "three months ended 2025-09-30" -> ... 2024-09-30."""
    from dataclasses import replace
    from skills.temporal.periods import _label
    q = replace(p, fiscal_year=p.fiscal_year - 1 if p.fiscal_year else None,
                end=f"{int(p.end[:4]) - 1:04d}{p.end[4:]}" if p.end else None,
                start=f"{int(p.start[:4]) - 1:04d}{p.start[4:]}" if p.start else None,
                raw=f"a year before {p.label}", comparison=None, change=None)
    q.label = _label(q)
    return q


_AGAINST = re.compile(r"\b(?:against|compared (?:to|with)|versus|vs\.?|from|as against)\b", re.I)


def _period_spans(sentence: str, periods: list[Period]) -> list[tuple[int, Period]]:
    out, pos = [], 0
    for p in periods:
        i = sentence.find(p.raw, pos)
        out.append((i if i >= 0 else pos, p))
        pos = max(pos, i + 1)
    return out


def _period_for(spans: list[tuple[int, Period]], start: int, end: int, nxt: int, previous) -> list[Period]:
    """The period a figure belongs to when a sentence names several: the first one after it and before the next figure
    ("₹414 crore in Q2 FY26 against ₹368 crore in Q2 FY25"), else the nearest before it. One period applies to all."""
    if len(spans) == 1:
        return [spans[0][1]]
    after = [p for i, p in spans if end <= i < nxt]
    if after:
        return [after[0]]
    before = [p for i, p in spans if i < start]
    return [before[-1]] if before else [spans[0][1]]


# --- the statements as an index ----------------------------------------------------------------------------------------

def fiscal_year_end(figures: list[dict]) -> int | None:
    """The month the company's financial year ends, from its own statements: the end month of its twelve-month columns;
    else the day before its year-to-date columns start (a 10-Q's nine months from January 1 -> December); else the
    comparative balance-sheet date that ends no period of the statements (a half-year's 'as at March 31, 2026')."""
    from datetime import date, timedelta
    ends = Counter(f["period_end"][5:7] for f in figures if f.get("period_months") == 12 and f.get("period_end"))
    if ends:
        return int(ends.most_common(1)[0][0])
    starts = Counter((date.fromisoformat(f["period_start"][:10]) - timedelta(days=1)).month for f in figures
                     if f.get("period_months") in (6, 9) and f.get("period_start"))
    if starts:
        return starts.most_common(1)[0][0]
    period_ends = {f["period_end"][:7] for f in figures if f.get("period_kind") == "duration" and f.get("period_end")}
    instants = Counter(int(f["period_end"][5:7]) for f in figures
                       if f.get("period_kind") == "instant" and f.get("period_end") and f["period_end"][:7] not in period_ends)
    return instants.most_common(1)[0][0] if instants else None


def _shift(year: int, month: int, back: int) -> str:
    n = year * 12 + (month - 1) - back
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def place(p: Period, fye: int | None) -> tuple[str, int | None, str] | None:
    """A period as (end month YYYY-MM, length in months, how it was placed), or None when it cannot be placed without a
    guess. A fiscal quarter is placed from the company's financial year as its statements show it."""
    months = p.months or {"quarter": 3, "half": 6, "full_year": 12}.get(p.span or "")
    if p.end:
        return p.end[:7], months, "the end date the sentence states"
    if not p.fiscal_year or p.span not in ("quarter", "half", "full_year"):
        return None
    calendar = p.basis == "calendar" or (p.basis is None and fye == 12)
    if calendar:
        end_month, how = 12, "the calendar year"
    elif fye:
        end_month = fye
        how = f"the company's financial year, which its statements show ending in month {fye}"
        if p.basis is None:
            how += " (the sentence does not say fiscal or calendar; read as the company's own year)"
    else:
        return None
    back = 3 * (4 - p.quarter) if p.span == "quarter" else 6 * (2 - p.half) if p.span == "half" else 0
    return _shift(p.fiscal_year, end_month, back), months, how


def _table_half(f: dict) -> Decimal:
    amount, value = Decimal(f["amount"]), Decimal(f["value"])
    factor = abs(value / amount) if amount else Decimal(1)
    return Decimal(5).scaleb(-int(f["decimals"]) - 1) * factor


def _ref(f: dict) -> dict:
    out = {k: f.get(k) for k in ("value_id", "printed", "value", "unit", "verdict", "concept", "statement", "basis", "title",
                                 "period_end", "period_months", "source_sha256", "source_url")}
    if f.get("proposed"):
        out["proposed"] = f["proposed"]
    return out


def _period_text(f: dict) -> str:
    return f"the {f['period_months']} months ended {f['period_end']}" if f.get("period_months") else f"the balance date {f['period_end']}"


def check(text: str, figures: list[dict], context: str = "") -> list[dict]:
    """Every figure in `text` against `figures` (statement values of the same company, from EvidenceStore.statement_figures).
    One result per figure, in the order written."""
    fye = fiscal_year_end(figures)
    by_concept: dict[str, list[dict]] = {}
    for f in figures:
        if f.get("value") is not None and f.get("period_end"):
            by_concept.setdefault(f["concept"], []).append(f)
    out = []
    for m in mentions(text, context):
        out.append({"mention": m.as_dict(), **_judge(m, by_concept, fye)})
    return out


def _judge(m: Mention, by_concept: dict, fye: int | None) -> dict:
    if m.not_compared:
        return {"verdict": "not_compared", "why": m.not_compared, "compared_with": []}
    pool = [f for c in m.concepts for f in by_concept.get(c, [])]
    if m.basis:
        pool = [f for f in pool if f.get("basis") in (m.basis, None)]
    if not pool:
        return {"verdict": "not_covered", "why": f"the statements read give no '{m.phrase}' figure", "compared_with": []}
    if m.currency and (units := {f["unit"] for f in pool if f["unit"] not in ("per_share", "shares", "ratio", "")}) and m.currency not in units:
        return {"verdict": "not_compared", "why": f"written in {m.currency}; the statements report in {', '.join(sorted(units))}",
                "compared_with": []}
    targets, hows = [], []
    if m.as_of:
        targets.append((m.as_of[:7], None))
        hows.append("the balance date the sentence states")
    for p in m.periods:
        if placed := place(p, fye):
            targets.append(placed[:2])
            hows.append(placed[2] + (" (the period is named next to the quote, not in it)" if m.period_origin == "context" else ""))
    if m.change:
        return _judge_change(m, pool, targets, hows)
    if not targets:
        equal = [f for f in pool if _equal(m, f)]
        unread = f" ('{m.periods[0].label}' cannot be placed: the statements read show no financial year)" if m.periods else ""
        if equal:
            return {"verdict": "found" if any(f["verdict"] != "doubt" for f in equal) else "matches_doubtful",
                    "why": f"no period stated{unread}; equals the statement figure for "
                           + "; ".join(sorted({_period_text(f) for f in equal})), "compared_with": [_ref(f) for f in equal]}
        return {"verdict": "not_found", "why": f"no period stated{unread}; no statement figure for '{m.phrase}' equals it",
                "compared_with": []}
    same = [f for f in pool if any(f["period_end"][:7] == end and (months is None or f.get("period_months") in (months, None))
                                   for end, months in targets)]
    if not same:
        when = ", ".join(p.label for p in m.periods) if m.periods else f"{m.as_of}"
        return {"verdict": "not_covered", "why": f"the statements read give no '{m.phrase}' figure for {when}",
                "compared_with": [], "placed_by": hows}
    equal = [f for f in same if _equal(m, f)]
    if any(f["verdict"] != "doubt" for f in equal):
        best = [f for f in equal if f["verdict"] != "doubt"]
        return {"verdict": "matches", "why": f"equals the {best[0]['statement']} statement figure {best[0]['printed']} "
                f"(for {_period_text(best[0])}) at the precision written", "compared_with": [_ref(f) for f in equal], "placed_by": hows}
    if equal:
        return {"verdict": "matches_doubtful", "why": f"equals {_doubt_note(equal)}",
                "compared_with": [_ref(f) for f in equal], "placed_by": hows}
    return {"verdict": "differs", "why": f"the statements give {', '.join(sorted({_shown(f) for f in same}))} for "
            f"{_period_text(same[0])}; written {m.raw}", "compared_with": [_ref(f) for f in same], "placed_by": hows}


def _doubt_note(fs: list[dict]) -> str:
    """A statement figure in doubt, said with how it was taken: the sentence agrees with that reading, a person confirms it."""
    parts = []
    for f in fs:
        p = f.get("proposed") or {}
        taken = f", taken as {p['reading']} (printed '{p['printed']}')" if p.get("changed") else ""
        parts.append(f"the {f['statement']} statement figure {f['printed']} for {_period_text(f)}{taken}, which the statements mark "
                     f"doubt; please double-check")
    return "; ".join(parts)


def _shown(f: dict) -> str:
    unit = "" if f["unit"] in ("", "per_share") else f" {f['unit']}"
    return f"{f['printed']} (= {Decimal(f['value']):,f}{unit})" if f["unit"] != "per_share" else f"{f['printed']} per share"


def _equal(m: Mention, f: dict) -> bool:
    value = abs(Decimal(f["value"]))
    tolerance = m.half + _table_half(f)
    return abs(abs(m.value) - value) <= tolerance


def _judge_change(m: Mention, pool: list[dict], targets: list, hows: list) -> dict:
    """A percentage change ("up 9%") against the change the statements give between the period and the one before it."""
    if not targets:
        return {"verdict": "not_compared", "why": "a change with no period stated", "compared_with": []}
    back = 3 if m.change == "qoq" else 12
    for end, months in targets:
        cur = [f for f in pool if f["period_end"][:7] == end and f.get("period_months") == months]
        y, mo = int(end[:4]), int(end[5:7])
        prev_end = _shift(y, mo, back)
        prev = [f for f in pool if f["period_end"][:7] == prev_end and f.get("period_months") == months]
        if cur and prev:
            a, b = Decimal(cur[0]["value"]), Decimal(prev[0]["value"])
            if not b:
                continue
            pct = (a - b) / abs(b) * 100
            refs = [_ref(cur[0]), _ref(prev[0])]
            doubt = any(f["verdict"] == "doubt" for f in (cur[0], prev[0]))
            # the printed figures are rounded, so the change they give is a range; the company works it out unrounded
            ha, hb = _table_half(cur[0]), _table_half(prev[0])
            ends = [((a + x) - (b + y)) / abs(b + y) * 100 for x in (-ha, ha) for y in (-hb, hb) if b + y]
            lo, hi = min(ends) - m.half, max(ends) + m.half
            if lo <= m.value <= hi:
                return {"verdict": "matches_doubtful" if doubt else "matches", "why": f"the statements give a change of {pct:+.1f}% "
                        f"({cur[0]['printed']} against {prev[0]['printed']}; {min(ends):+.1f}% to {max(ends):+.1f}% allowing for "
                        f"their rounding)" + (f"; {_doubt_note([f for f in (cur[0], prev[0]) if f['verdict'] == 'doubt'])}" if doubt else ""),
                        "compared_with": refs, "placed_by": hows}
            return {"verdict": "differs", "why": f"the statements give a change of {pct:+.1f}% ({cur[0]['printed']} against "
                    f"{prev[0]['printed']}), not {m.value:+}%", "compared_with": refs, "placed_by": hows}
    return {"verdict": "not_covered", "why": "the statements read do not give both periods of the change", "compared_with": [],
            "placed_by": hows}


def summary(results: list[dict]) -> dict:
    """Counts by verdict, and a short label for a report row: what a reader should see first."""
    n = Counter(r["verdict"] for r in results)
    if n["differs"]:
        r = next(r for r in results if r["verdict"] == "differs")
        label = f"differs from the company's statements: {r['mention']['raw']} ({r['mention']['phrase']}); {r['why']}"
    elif n["matches_doubtful"]:
        label = "matches only a statement figure in doubt; please double-check"
    elif n["matches"] or n["found"]:
        label = "matches the company's statements"
    else:
        label = ""
    return {"counts": dict(n), "label": label}
