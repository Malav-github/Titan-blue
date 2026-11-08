"""A table as printed, and what code can say about it: its value columns and the period of each, its rows and the line item
of each, its scale and currency, which statement it is and on what basis (consolidated or standalone).

Every format (inline XBRL, HTML, PDF) is first turned into the same `RawTable`, a grid of cells with an optional XBRL fact
on each, and then read by `interpret` alone. Nothing is guessed: a column whose period the headers do not state has no period,
a row whose label the ontology does not know has no concept, and a table that states no scale has none (its values are kept
as printed and are not usable as amounts until a person or a better source settles the scale).
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ontology import financial_statements, label_key
from skills.temporal.extract import parse_date
from skills.temporal.periods import find_periods

from .numbers import Amount, Scale, find_scale, parse_amount


@dataclass
class Cell:
    text: str
    fact: dict | None = None        # an inline XBRL fact in the cell: name, context, unit, decimals, scale, sign, id, value, period, dims
    header: bool = False            # a <th>, or a cell the source marks as a heading
    ocr: dict | None = None         # a scanned cell: both engines' readings and confidences, whether they agree, where on the page


@dataclass
class RawTable:
    rows: list[list[Cell]]
    caption: str                    # the text printed just before the table (its title, the scale line)
    locator: dict                   # where it is: {"table": 12} in a page, {"page": 17, "table": 0} in a PDF
    adapter: str                    # ixbrl | html | pdf


@dataclass
class Column:
    index: int
    header: str                     # every header line above it, joined
    kind: str                       # duration | instant | change | unknown
    end: str | None = None
    start: str | None = None        # only when stated (or exact from XBRL)
    months: int | None = None
    label: str = ""                 # as the period is named ("Three months ended September 30, 2026", "Q2 FY26")
    origin: str = ""                # xbrl | header
    derived: str = ""               # set when dates were worked out rather than read, with how


@dataclass
class Row:
    index: int
    label: str
    concept: str = ""
    concept_origin: str = ""        # xbrl_tag | label | ''
    section: str = ""               # the heading row above it ("Current assets")
    is_total: bool = False
    derived_label: str = ""         # set when code named an unlabelled line, with why


@dataclass
class Value:
    row: int
    col: int
    amount: Amount
    full: Decimal | None            # the amount in units (scale applied), None when the scale is not stated
    unit: str                       # a currency code, 'per_share', 'shares', 'ratio' or '' when unknown
    fact: dict | None = None
    ocr: dict | None = None
    verdict: str = ""               # verified | agreed | doubt | read  (set by grade() after the checks)
    reasons: list = field(default_factory=list)      # why it is in doubt
    notes: list = field(default_factory=list)        # what was noticed but settled (an engine misread, settled by a sum)


@dataclass
class Table:
    raw: RawTable
    statement: str                  # income | comprehensive_income | balance | cash_flow | equity | other
    title: str
    basis: str | None
    standard: str | None
    scale: Scale | None
    columns: list[Column]
    rows: list[Row]
    values: list[Value] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # what code could not settle, said plainly
    primary: bool = False           # titled as one of the statements, not a note or a summary that shares its line items
    checks: list = field(default_factory=list)       # arithmetic checks (checks.Check), filled by extract()

    def value_at(self, row: int, col: int) -> Value | None:
        return next((v for v in self.values if v.row == row and v.col == col), None)

    def by_concept(self, concept: str) -> list[Row]:
        return [r for r in self.rows if r.concept == concept]


# --- reading headers ----------------------------------------------------------------------------------------------------

_NOTE_HEADER = re.compile(r"^\s*notes?\s*$|^\s*note\s*(?:no\.?|ref\.?)\s*$", re.I)
_DATE_WORDS = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
               r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_DATE_IN = re.compile(rf"{_DATE_WORDS}\.?\s+\d{{1,2}}(?:,\s*|\s+)\d{{4}}|\d{{1,2}}\s+{_DATE_WORDS}\.?,?\s+\d{{4}}|"
                      r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", re.I)
_MONTH_DAY = re.compile(rf"({_DATE_WORDS}\.?\s+\d{{1,2}})(?!\d),?(?!\s*\d{{4}})", re.I)
_YEAR = re.compile(r"(?<![\d.,])(?:19|20)\d{2}(?![\d.,])")


def _numeric_date(text: str) -> str | None:
    """30.09.2025 (day first: how India, the Philippines and most of the world print dates with dots). With slashes or
    hyphens the order is taken only when one part is over 12; 04/05/2026 is left unread rather than guessed."""
    m = re.fullmatch(r"(\d{1,2})([./-])(\d{1,2})\2(\d{4})", text)
    if not m:
        return None
    a, sep, b, y = int(m[1]), m[2], int(m[3]), int(m[4])
    if sep == "." or a > 12:
        d, mo = a, b
    elif b > 12:
        mo, d = a, b
    else:
        return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _dates(text: str) -> list[str]:
    out = []
    for m in _DATE_IN.finditer(text):
        raw = m[0]
        d = _numeric_date(raw) if re.match(r"\d{1,2}[./-]", raw) else None
        if d is None and not re.match(r"\d{1,2}[./-]\d{1,2}[./-]", raw):
            parsed = parse_date(raw)
            d = parsed[0] if parsed and parsed[1] == "day" else None
        if d:
            out.append(d)
    return out


_MONTH_YEAR = re.compile(rf"\b({_DATE_WORDS})\.?\s*(?:['’`\-]\s?(\d{{2}})\b|\s(\d{{4}})\b)", re.I)
_MONTH_NO = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _year4(y: str) -> int:
    return 2000 + int(y) if len(y) == 2 else int(y)


_CAPTION_ENDED = re.compile(rf"\bended\s+({_DATE_WORDS}\.?\s+\d{{1,2}}|\d{{1,2}}\s+{_DATE_WORDS})\b", re.I)


def _fiscal_dates(fy: int, quarter: int | None, half: int | None, span: str | None, fye: str) -> tuple[str, str] | None:
    """Start and end of a fiscal period named "Q2 FY26" in a market whose financial year ends on `fye` ("03-31" in India):
    FY26 runs 1 April 2025 to 31 March 2026, so its Q2 is July to September 2025."""
    from dateutil.relativedelta import relativedelta
    month, day = (int(x) for x in fye.split("-"))
    year_end = date(fy, month, day)
    year_start = year_end - relativedelta(years=1) + relativedelta(days=1)
    if quarter:
        start = year_start + relativedelta(months=3 * (quarter - 1))
        return start.isoformat(), (start + relativedelta(months=3) - relativedelta(days=1)).isoformat()
    if half:
        start = year_start + relativedelta(months=6 * (half - 1))
        return start.isoformat(), (start + relativedelta(months=6) - relativedelta(days=1)).isoformat()
    if span == "full_year":
        return year_start.isoformat(), year_end.isoformat()
    return None


def read_header(header: str, ended: str | None = None, caption_months: int | None = None,
                fiscal_year_end: str | None = None) -> Column:
    """The period a column header names. "Three Months Ended September 30, 2026" -> 3 months ending 2026-09-30; "June 30,
    2025 (Unaudited)" -> an instant; "Quarter ended 30.09.2025" -> 3 months; "Q2 FY26" -> a named quarter without dates;
    "YoY (%)" -> a change column, not a period. `ended` is the day the table's caption says its periods end ("Three and nine
    months ended September 30, 2026 and 2025"): a header that gives only "Three Months 2026" is then read against it."""
    vocab = financial_statements()["periods"]
    text = " ".join(header.split())
    col = Column(-1, text, "unknown")
    if not text:
        return col
    if re.search(vocab["not_periods"], text, re.I):
        col.kind = "change"
        return col
    if (m := _MONTH_DAY.search(text)) and (y := _YEAR.search(text[m.end():])):
        text_dated = text[:m.end()] + " " + y[0] + text[m.end():]   # "September 30, ... 2026" printed on two header lines
    else:
        text_dated = text
    dates = _dates(text_dated)
    low = text.lower()
    months = next((n for w, n in sorted(vocab["duration_words"].items(), key=lambda kv: -len(kv[0])) if w in low), None)
    if not dates and ended and (y := _YEAR.search(text)):
        bare = next((n for w, n in sorted(vocab["bare_duration_words"].items(), key=lambda kv: -len(kv[0])) if w in low), None)
        if bare is None and caption_months and re.fullmatch(r"(?:19|20)\d{2}", re.sub(r"\(?\b(?:un)?audited\b\)?|\(?restated\)?", "", text, flags=re.I).strip()):
            bare = caption_months                    # a header of just "2026" under "Nine months ended September 30, 2026 and 2025"
        if bare and (d := _dates(f"{ended}, {y[0]}" if ended[0].isalpha() else f"{ended} {y[0]}")):
            dates, months = d, months or bare
            text = f"{text} (ended {ended}, per the table's caption)"
    if months and dates:
        col.kind, col.months, col.end = "duration", months, dates[-1]
    elif dates and len(dates) == 2 and re.search(r"\b(?:from|to|through|–|-)\b", low):
        col.kind, col.start, col.end = "duration", dates[0], dates[1]
    elif dates:
        col.kind, col.end = "instant", dates[-1]
    elif m := _MONTH_YEAR.search(text):
        # "Sept'25", "Mar-25", "March 2025": the month is stated, the day is not, so the date stays at month precision
        month = _MONTH_NO[m[1][:3].lower()]
        col.end = f"{_year4(m[2] or m[3]):04d}-{month:02d}"
        col.kind = "duration" if months else "instant"
        col.months = months
    else:
        periods = find_periods(text)
        if periods:
            p = periods[0]
            col.kind = "duration"
            col.months = p.months or {"quarter": 3, "half": 6, "full_year": 12}.get(p.span or "")
            col.end, col.start = p.end, p.start
            col.label = p.label
            if fiscal_year_end and p.fiscal_year and p.basis == "fiscal" and not col.end:
                if dates_ := _fiscal_dates(p.fiscal_year, p.quarter, p.half, p.span, fiscal_year_end):
                    col.start, col.end = dates_
                    col.derived = (f"dates worked out from '{p.raw}' and the market's statutory financial year ending "
                                   f"{fiscal_year_end} (MM-DD); the header itself states no dates")
    if not col.label:
        col.label = text
    col.origin = "header"
    return col


# --- interpreting a raw table --------------------------------------------------------------------------------------------

_TOTAL = re.compile(r"^(?:total|net\b|sub-?total)", re.I)
_PER_SHARE = re.compile(r"per (?:equity )?share|\beps\b|^basic\b|^diluted\b|\(in (?:rs|₹|inr|php|\$)[^)]*\)\s*$", re.I)
_SHARES = re.compile(r"\bshares?\b.*\b(?:outstanding|weighted|number)\b|\bweighted[- ]average\b.*\bshares?\b", re.I)


def _amount(cell: Cell, decimal_mark: str | None) -> Amount | None:
    """The number as the page presents it. For an XBRL cell the printed brackets decide the sign shown (capital expenditure
    is tagged +93 and printed "(93)"); the fact's own value stays on the fact."""
    a = parse_amount(cell.text, decimal_mark)
    if cell.fact and cell.fact.get("printed") is not None:
        fact_abs = abs(Decimal(cell.fact["printed"]))
        if a is None or abs(a.value) != fact_abs:
            neg = Decimal(cell.fact["printed"]) < 0
            return Amount(-fact_abs if neg else fact_abs, a.decimals if a else 0, neg, cell.text)
    return a


TITLE_WITHIN = 260        # a statement's title is printed just above it; a mention further back is someone else's text


def _statement_of(caption: str) -> tuple[str | None, str]:
    """The statement a caption titles. A title is a heading: capitalised ("Consolidated Statements of Income") and close to
    the table. "... as shown in the consolidated statements of income" in a note is a mention, not a title."""
    tail = caption[-TITLE_WITHIN:]
    best = None
    for s in financial_statements()["statements"]:
        for m in re.finditer(s["title"], tail, re.I):
            if m[0][:1].isupper() and (best is None or m.start() > best[2]):
                best = (s["id"], m[0], m.start())
    return (best[0], best[1]) if best else (None, "")


def _cue(items: list[dict], text: str) -> str | None:
    return next((i["id"] for i in items if re.search(i["cues"], text, re.I)), None)


def interpret(raw: RawTable, decimal_mark: str | None = None, fiscal_year_end: str | None = None) -> Table | None:
    """Read a raw table. None when it holds no values (a layout table, a table of text)."""
    onto = financial_statements()
    grid = raw.rows
    width = max((len(r) for r in grid), default=0)
    amounts = [[_amount(c, decimal_mark) if c.text.strip() else None for c in r] + [None] * (width - len(r)) for r in grid]
    for i, r in enumerate(amounts):
        nums = [c for c, a in zip(grid[i], r) if a is not None]
        if _is_year_row(grid[i], r) or (nums and all(c.header for c in nums)):
            amounts[i] = [None] * width                # "2026  2025" over the columns is a header, not values
    numeric_rows = [i for i, r in enumerate(amounts) if any(a is not None for a in r[1:])]
    if not numeric_rows:
        return None
    need = 1 if len(grid) < 4 else 2
    value_cols = [j for j in range(1, width) if sum(1 for i in numeric_rows if amounts[i][j] is not None) >= need]
    if not value_cols:
        return None
    first_data = min(i for i in numeric_rows if any(amounts[i][j] is not None for j in value_cols))
    head_rows = grid[:first_data]

    def header_text(j: int) -> str:
        parts = []
        for r in head_rows:
            t = r[j].text.strip() if j < len(r) else ""
            if t and (not parts or parts[-1] != t):
                parts.append(t)
        return " ".join(parts)

    note_cols = {j for j in value_cols if _NOTE_HEADER.match(header_text(j)) or _looks_like_notes(j, value_cols, numeric_rows, amounts, grid)}
    value_cols = [j for j in value_cols if j not in note_cols]
    if not value_cols:
        return None
    # a column with a stray number (a dash printed in the "$" cell) joins the value column beside it under the same header
    strays = [j for j in range(1, width) if j not in value_cols and j not in note_cols
              and any(amounts[i][j] is not None for i in numeric_rows)]
    groups = [g for g in _merge_columns(sorted(value_cols + strays), numeric_rows, amounts, header_text)
              if any(j in value_cols for j in g)]
    for g in groups:
        g.sort(key=lambda j: j not in value_cols)       # the real value column first: it names the group
    for g in groups:                                   # one printed column split over two cells: read it as one
        for i in numeric_rows:
            if amounts[i][g[0]] is None and (src := next((j for j in g[1:] if amounts[i][j] is not None), None)) is not None:
                amounts[i][g[0]] = amounts[i][src]
                grid[i][g[0]] = grid[i][src]
    value_cols = [g[0] for g in groups]

    # columns and their periods: exact from XBRL contexts when the cells carry facts, else from the printed headers
    columns = []
    tail = raw.caption[-TITLE_WITHIN:]
    ended = m[1] if (m := _CAPTION_ENDED.search(tail)) else None
    stated = {n for w, n in onto["periods"]["duration_words"].items() if re.search(rf"\b{re.escape(w)}", tail, re.I)}
    caption_months = stated.pop() if len(stated) == 1 else None      # one length named, not "three and nine months"
    for j in value_cols:
        col = read_header(header_text(j), ended, caption_months, fiscal_year_end)
        col.index = j
        facts = [grid[i][j].fact for i in numeric_rows if j < len(grid[i]) and grid[i][j].fact and not grid[i][j].fact.get("dims")]
        periods = [(f["period"].get("start"), f["period"].get("end") or f["period"].get("instant"), "instant" in f["period"]) for f in facts if f.get("period")]
        if periods:
            durations = [p for p in periods if not p[2]]
            pick = max(set(durations or periods), key=(durations or periods).count)
            col.kind = "instant" if pick[2] else "duration"
            col.start, col.end = (None, pick[1]) if pick[2] else (pick[0], pick[1])
            col.months = _months_between(col.start, col.end) if col.start else None
            col.origin = "xbrl"
            col.label = col.label if col.label and col.label != col.header else _period_label(col)
        columns.append(col)

    caption = " ".join(raw.caption.split())
    context = f"{caption} {' '.join(header_text(j) for j in value_cols)} " + " ".join(c.text for r in head_rows for c in r[:1])
    statement, title = _statement_of(caption) if caption else (None, "")
    primary = statement is not None             # titled as a statement; one known only by its tags or rows may be a note
    scale = find_scale(caption[-300:] + " " + " ".join(c.text for r in head_rows for c in r))
    basis = _cue(onto["bases"], caption[-400:])
    standard = _cue(onto["standards"], caption[-400:])
    if standard is None and any(c.fact for r in grid for c in r):
        prefixes = {c.fact["name"].split(":")[0] for r in grid for c in r if c.fact}
        standard = "us_gaap" if "us-gaap" in prefixes else "ifrs" if "ifrs-full" in prefixes else None

    # rows: label, section heading, concept
    rows, section, before = [], "", ""
    label_end = value_cols[0]
    text_row = None                            # the text-only line just above, if the previous line was one
    for i in range(first_data, len(grid)):
        r = grid[i]
        label = _label(r[:label_end])
        if not any(amounts[i][j] is not None for j in value_cols):
            if label:
                before, section, text_row = section, label, i
            continue
        if label[:1].islower() and text_row == i - 1:
            # a label wrapped onto two lines ("Effect of exchange rate changes on cash and cash" / "equivalents  7,212,935"):
            # the first line was not a heading
            label, section = f"{section} {label}", before
        text_row = None
        rows.append(Row(i, label, section=section, is_total=bool(_TOTAL.match(label.strip()))))

    _name_unlabelled_totals(rows, amounts, value_cols)
    if statement is None:
        # a statement known by its rows: an equity statement opens and closes with "Balance as of ..."
        for s in onto["statements"]:
            if s.get("rows") and sum(1 for r in rows if re.search(s["rows"], r.label, re.I)) >= 2:
                statement = s["id"]
                break
    by_label, by_tag = onto["_by_label"], onto["_by_tag"]
    tag_statements = {}
    for row in rows:
        tags = [grid[row.index][j].fact["name"] for j in value_cols
                if j < len(grid[row.index]) and grid[row.index][j].fact and not grid[row.index][j].fact.get("dims")]
        if tags and (concept := by_tag.get(max(set(tags), key=tags.count))):
            row.concept, row.concept_origin = concept, "xbrl_tag"
            tag_statements[onto["_concepts"][concept]["statement"]] = tag_statements.get(onto["_concepts"][concept]["statement"], 0) + 1
    if statement is None and tag_statements:
        statement = max(tag_statements, key=tag_statements.get)
    if statement is None:
        found = {}
        for row in rows:
            for s in ("income", "balance", "cash_flow"):
                if (s, label_key(row.label)) in by_label:
                    found[s] = found.get(s, 0) + 1
        if found and max(found.values()) >= 3:
            statement = max(found, key=found.get)
    tagged = any(c.fact for r in grid for c in r)
    for row in rows:
        key = label_key(row.label)
        if row.concept and row.concept_origin == "xbrl_tag":
            continue
        if tagged:
            # In a tagged table the tag says what a row is; a label only tells apart rows that share a tag. "Cash provided
            # by operating activities" under discontinued operations is not the company's operating cash flow.
            tags = {grid[row.index][j].fact["name"] for j in value_cols if j < len(grid[row.index]) and grid[row.index][j].fact}
            if (cid := by_label.get(("cash_flow", key))) in ("cash_beginning", "cash_end") and any("Cash" in t for t in tags):
                row.concept, row.concept_origin = cid, "xbrl_tag+label"
            continue
        if statement and (cid := by_label.get((statement, key))):
            row.concept, row.concept_origin = cid, "label"
        elif statement == "comprehensive_income" and (cid := by_label.get(("income", key))):
            row.concept, row.concept_origin = cid, "label"
        elif statement and (cid := next((c for c, rx in onto["_patterns"].items()
                                         if onto["_concepts"][c]["statement"] == statement and rx.search(key)), None)):
            row.concept, row.concept_origin = cid, "label"
    _one_row_per_concept(rows)

    if statement and next(s for s in onto["statements"] if s["id"] == statement)["period"] == "instant":
        for col in columns:
            if col.kind == "duration" and col.end and col.origin != "xbrl":
                # "Fiscal Year Ended Dec 31, 2024" or "Six months ended Sep 30, 2025" over a balance sheet: at that date
                col.kind, col.start, col.months = "instant", None, None
    table = Table(raw, statement or "other", title, basis, standard, scale, columns, rows, primary=primary)
    if primary and statement != "equity":
        unread = [c for c in columns if c.kind == "unknown" or (c.kind != "change" and not c.end)]
        if unread:
            table.notes.append(f"{len(unread)} column(s) without a readable period ({', '.join(repr(c.header[:40]) for c in unread)}): "
                               "their figures are not usable until a person settles the period")
    if scale is None or scale.factor is None:
        table.notes.append("the table does not state its scale, so its values are kept as printed and not as amounts")

    currency = scale.currency if scale else None
    if currency is None and any("$" in c.text for r in grid for c in r):
        currency = "USD"
    for row in rows:
        kind = onto["_concepts"][row.concept]["kind"] if row.concept else (
            "shares" if _SHARES.search(row.label) or _SHARES.search(row.section) else
            "per_share" if _PER_SHARE.search(row.label) or _PER_SHARE.search(row.section) else "money")
        for col in columns:
            cell = grid[row.index][col.index] if col.index < len(grid[row.index]) else None
            a = amounts[row.index][col.index]
            if cell is None or a is None:
                continue
            fact = cell.fact
            if fact and fact.get("value") is not None:
                full = a.value.scaleb(int(fact.get("scale") or 0))          # as presented; the fact's value stays on the fact
                unit = _unit_of(fact.get("unit", ""), kind, currency)
            elif kind == "per_share" or a.percent:
                full, unit = a.value, ("ratio" if a.percent else "per_share")
            elif kind == "shares":
                full, unit = (a.value * scale.factor if scale and scale.factor else None), "shares"
            else:
                full = a.value * scale.factor if scale and scale.factor is not None else None
                unit = currency or ""
            table.values.append(Value(row.index, col.index, a, full, unit, fact, cell.ocr))
    return table


_HEADER_LABEL = re.compile(r"^\(?\s*(?:un)?audited\s*\)?$|^\(.*\)$|^(?:particulars|description|notes?)$", re.I)


def _is_year_row(cells: list[Cell], amounts: list) -> bool:
    """A row of years over the columns ("2026  2025"), with nothing or a header word ("(unaudited)", "Particulars") as its label."""
    nums = [(c, a) for c, a in zip(cells, amounts) if a is not None]
    label = _label([c for c, a in zip(cells, amounts) if a is None][:3])
    return bool(nums) and all(re.fullmatch(r"(?:19|20)\d{2}", c.text.strip()) and not c.fact for c, a in nums) \
        and (not label or bool(_HEADER_LABEL.match(label)))


def _label(cells: list[Cell]) -> str:
    parts = []
    for c in cells:
        t = c.text.strip()
        if t and parse_amount(t) is None and (not parts or parts[-1] != t):     # a label spread over spanned cells, once
            parts.append(t)
    return " ".join(parts)


def _name_unlabelled_totals(rows: list[Row], amounts: list, cols: list[int]) -> None:
    """A total printed without a label under its section ("Revenues" / three lines / an unlabelled sum) is named after the
    section, but only when code has checked that it is the sum of the section's lines in every column. The name says so."""
    for k, row in enumerate(rows):
        if row.label or not row.section:
            continue
        parts = []
        for prev in reversed(rows[:k]):
            if prev.section != row.section or not prev.label:
                break
            parts.append(prev)
        if len(parts) < 2:
            continue
        ok = True
        for j in cols:
            total = amounts[row.index][j]
            vals = [amounts[p.index][j] for p in parts]
            if total is None or any(v is None for v in vals):
                ok = ok and total is None and all(v is None for v in vals)
                continue
            places = max([total.decimals] + [v.decimals for v in vals])
            if abs(sum(v.value for v in vals) - total.value) > Decimal(len(vals) + 1) / 2 * Decimal(1).scaleb(-places):
                ok = False
        if ok:
            row.label = f"Total {row.section.rstrip(':').strip()}"
            row.derived_label = f"unlabelled line: the sum of the {len(parts)} lines under '{row.section}' in every column"
            row.is_total = True


def _looks_like_notes(j: int, cols: list[int], rows: list[int], amounts: list, grid: list) -> bool:
    """A notes column without a "Notes" header: the first value column, holding only note references (4, 6.2, 20.1, never
    bracketed, never grouped) beside columns of real amounts."""
    if j != cols[0] or len(cols) < 3:
        return False
    cells = [grid[i][j].text.strip() for i in rows if amounts[i][j] is not None]
    others = [amounts[i][k] for i in rows for k in cols[1:] if amounts[i][k] is not None]
    return len(cells) >= 3 and all(re.fullmatch(r"\d{1,2}(?:\.\d{1,2})?", c) for c in cells) \
        and sum(1 for a in others if abs(a.value) >= 1000) >= len(others) / 2


def _merge_columns(cols: list[int], rows: list[int], amounts: list, header_text) -> list[list[int]]:
    """Neighbouring columns that never hold a number in the same row and sit under the same header are one printed column
    (filings put "$3,678" in one cell on the first row and "1,034" one cell to the left on the next)."""
    groups: list[list[int]] = []
    for j in cols:
        if groups:
            g = groups[-1]
            clash = any(amounts[i][j] is not None and any(amounts[i][k] is not None for k in g) for i in rows)
            same = header_text(j) == header_text(g[0]) or not header_text(j) or not header_text(g[0])
            if not clash and same and j - g[-1] <= 2:
                g.append(j)
                continue
        groups.append([j])
    return groups


def _one_row_per_concept(rows: list[Row]) -> None:
    """A concept named by its label on two rows ("Interest-bearing loans" under current and non-current liabilities) is not
    either of them: the label alone cannot say which is which. XBRL tags settle this; labels do not."""
    seen: dict[str, list[Row]] = {}
    for r in rows:
        if r.concept and r.concept_origin == "label":
            seen.setdefault(r.concept, []).append(r)
    for same in seen.values():
        if len(same) > 1:
            # "Net income" again under "Amounts attributable to the parent's shareholders" is the parent's share: the row
            # outside such a section is the company's. If that does not single one out, none of them is used.
            plain = [r for r in same if not _SUBSET_SECTION.search(r.section)]
            keep = plain[0] if len(plain) == 1 else None
            for r in same:
                if r is not keep:
                    r.concept, r.concept_origin = "", ""


_SUBSET_SECTION = re.compile(r"attributable|per (?:common |equity )?share|discontinued|non-?controlling|segment", re.I)


def _unit_of(unit: str, kind: str, currency: str | None) -> str:
    u = unit.lower()
    if "shares" in u and ("usd" in u or "/" in u or "per" in u):
        return "per_share"
    if u in ("shares",):
        return "shares"
    if u in ("pure", "number"):
        return "ratio"
    m = re.search(r"(usd|inr|php|eur|gbp|jpy|idr|vnd|ngn|kes)", u)
    return m[1].upper() if m else (currency or "")


def _months_between(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return round(((e - s).days + 1) / 30.4375)


def _period_label(col: Column) -> str:
    if col.kind == "instant":
        return f"as at {col.end}"
    if col.months and col.end:
        return f"{col.months} months ended {col.end}"
    return col.header
