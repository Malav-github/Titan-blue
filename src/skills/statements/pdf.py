"""Tables in PDFs with a text layer (most annual and quarterly reports in India, the Philippines and elsewhere), turned into
raw grids by where the words sit on the page.

Statements print numbers right-aligned in columns, so the columns are found from where numbers end (their right edges line
up), headers are placed over the column nearest to them, and each printed line becomes a row: the words before the first
number are its label. A statement that runs over two pages is joined when the next page carries no title of its own and the
same columns. Scanned pages (no text layer) give nothing: they need OCR, and OCR values are not trusted without review.
"""

import io
import re
from statistics import median

from ontology import financial_statements

from .numbers import find_scale, parse_amount
from .tables import Cell, RawTable

LINE_TOLERANCE = 3.0          # words whose tops differ by less than this (points) are on one line
WORD_GAP = 3.5                # words closer than this (points) are one phrase
HEADER_BLOCK_GAP = 30.0       # header lines sit within this distance (points) of each other and of the first row
HEADER_WORD_GAP = 4.5        # in a header line, a word space; "Q2 FY26   Q2 FY25" side by side are two headers
COLUMN_TOLERANCE = 9.0        # right edges within this are one column
MIN_COLUMN_HITS = 3           # a column has numbers on at least this many lines
HEADER_REACH = 70.0           # a header word further than this from every column is not a column header
MAX_PAGES = 400


def _lines(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= LINE_TOLERANCE:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


_DASH = re.compile(r"^[-‒-―−]+$")
_DATE_LINE = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\b|\bended\b|\bas (?:at|of|on)\b|"
                        r"\bperiods?\b|\bquarters?\b|\bmonths?\b|\byears?\b|\bFY\s?\d|\bQ[1-4]\b|\bH[12]\b", re.I)
TITLE_WIDTH = 230.0           # a phrase wider than this is a title or a label, never a column header (one over two columns is ~160)
_CAPTION_LINE = re.compile(r"^(?:for the|as (?:at|of) |\(?all amounts|\(?amounts in|and of the)\b", re.I)


def _is_value(p: dict, line_text: str) -> bool:
    """A number that is a value in the table, not a year or day in a header line ("June 30, 2025 and 2024")."""
    if not p["num"]:
        return False
    t = p["text"].strip()
    return not (re.fullmatch(r"(?:19|20)\d{2}|\d{1,2},?", t) and _DATE_LINE.search(line_text))


def _phrases(line: list[dict]) -> list[dict]:
    """Words joined into phrases ("Cash and cash equivalents"), but a number is always its own phrase. A dash between words
    ("Trade receivables - net") is text, not a nil value."""
    out: list[dict] = []
    for k, w in enumerate(line):
        nxt = line[k + 1]["text"] if k + 1 < len(line) else ""
        num = parse_amount(w["text"]) is not None and not (_DASH.match(w["text"]) and nxt and parse_amount(nxt) is None)
        if out and not num and not out[-1]["num"] and w["x0"] - out[-1]["x1"] <= WORD_GAP * 2:
            out[-1]["text"] += " " + w["text"]
            out[-1]["x1"] = w["x1"]
        elif out and num and out[-1]["num"] and w["x0"] - out[-1]["x1"] <= 1.0:
            out[-1]["text"] += w["text"]                  # "(15,237," "703)" split by the PDF: one number
            out[-1]["x1"] = w["x1"]
        else:
            out.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"], "top": w["top"], "num": num})
    for p in out:
        if not p["num"]:
            continue
        p["num"] = parse_amount(p["text"]) is not None
    text = " ".join(p["text"] for p in out)
    for p in out:
        p["value"] = _is_value(p, text)
    return out


def _columns(lines: list[list[dict]]) -> list[float]:
    """Right edges where numbers line up, left to right. Leading note numbers ("4", "6.2") are kept as a column of their own
    and recognised later by their header or their content."""
    edges = sorted(p["x1"] for line in lines for p in line if p["value"] and not _is_label_number(p, line))
    clusters: list[list[float]] = []
    for e in edges:
        if clusters and e - clusters[-1][-1] <= COLUMN_TOLERANCE:
            clusters[-1].append(e)
        else:
            clusters.append([e])
    return [median(c) for c in clusters if len(c) >= MIN_COLUMN_HITS]


def _is_label_number(p: dict, line: list[dict]) -> bool:
    """A number inside the label ("2.  Expenses", "Q2 FY26" is not a number anyway) sits left of the label's last word."""
    first_text = next((q for q in line if not q["num"]), None)
    return first_text is not None and p["x1"] <= first_text["x0"] and re.fullmatch(r"\d{1,2}\.?|[ivx]+\.", p["text"]) is not None


def _nearest(x: float, cols: list[float], reach: float) -> int | None:
    best = min(range(len(cols)), key=lambda k: abs(cols[k] - x), default=None)
    return best if best is not None and abs(cols[best] - x) <= reach else None


def _titled(text: str) -> bool:
    return any((m := re.search(s["title"], text, re.I)) and m[0][:1].isupper() for s in financial_statements()["statements"])


def _page_tables(lines: list[list[dict]], page_no: int) -> list[tuple[list[list[dict]], str]]:
    """Split a page where a new statement title starts (two statements printed on one page)."""
    parts, current, caption = [], [], []
    for line in lines:
        text = " ".join(w["text"] for w in line)
        has_number = any(parse_amount(w["text"]) is not None for w in line[1:])
        if _titled(text) and not has_number and any(any(parse_amount(w["text"]) is not None for w in l) for l in current):
            parts.append((current, " ".join(caption)))
            current, caption = [], []
        current.append(line)
        if not any(any(parse_amount(w["text"]) is not None for w in l[1:]) for l in current[:-1]):
            caption.append(text)
    if current:
        parts.append((current, " ".join(caption)))
    return parts


def _grid(lines: list[list[dict]]) -> tuple[list[list[Cell]], int] | None:
    phrased = [_phrases(line) for line in lines]
    cols = _columns(phrased)
    if not cols:
        return None
    width = len(cols) + 1
    need = min(2, len(cols))                  # a callout above the table ("Rs 442 crores") fills one column, a row fills more
    first_data = next((i for i, line in enumerate(phrased)
                       if len({_nearest(p["x1"], cols, COLUMN_TOLERANCE * 1.5) for p in line if p["value"]} - {None}) >= need
                       and any(not p["num"] for p in line)), None)
    if first_data is None:
        return None
    widths: dict[int, list[float]] = {}
    for line in phrased[first_data:]:
        for p in line:
            if p["value"] and (k := _nearest(p["x1"], cols, COLUMN_TOLERANCE * 1.5)) is not None:
                widths.setdefault(k, []).append(p["x1"] - p["x0"])
    extent = [(cols[k] - max(median(widths.get(k, [40.0])), 30.0), cols[k]) for k in range(len(cols))]
    # Column headers are the lines just above the first row of numbers; anything further up (a title, a callout graphic with
    # "Rs 442 crores  5.2% YoY") is caption, not a header
    head_from = first_data
    while head_from > 0 and lines[head_from][0]["top"] - lines[head_from - 1][0]["top"] <= HEADER_BLOCK_GAP:
        head_from -= 1
    rows: list[list[Cell]] = []
    left_edge = extent[0][0]
    for i, line in enumerate(phrased):
        row = [Cell("") for _ in range(width)]
        header = i < first_data or not any(p["value"] for p in line)
        if i < head_from:
            row[0].text = " ".join(p["text"] for p in line)
            rows.append(row)
            continue
        if i < first_data and any(not p["num"] and (p["x0"] < left_edge - HEADER_REACH / 2 < left_edge < p["x1"]
                                                    or len(p["text"].split()) >= 5 or _CAPTION_LINE.match(p["text"]))
                                  for p in line):
            # a title line ("For the six-month periods ended June 30, 2025 and 2024", centred on the page, or running from
            # the label area into the columns) is the caption, years and all; column headers are short
            row[0].text = " ".join(p["text"] for p in line)
            rows.append(row)
            continue
        if i < first_data:
            line = _header_phrases(lines[i])
            heads = [p for p in line if p["x1"] - p["x0"] <= TITLE_WIDTH and not find_scale(p["text"])
                     and p["x1"] >= extent[0][0] - HEADER_REACH / 2]
            over = _over(heads, extent)
            for k, p in over.items():
                row[k + 1].text = (row[k + 1].text + " " + p["text"]).strip()
                row[k + 1].header = True
            used = {id(p) for p in over.values()}
            row[0].text = " ".join(p["text"] for p in line if id(p) not in used)
            rows.append(row)
            continue
        for p in line:
            if p["value"] and not header:
                k = _nearest(p["x1"], cols, COLUMN_TOLERANCE * 1.5)
                if k is not None and not _is_label_number(p, line):
                    row[k + 1].text = (row[k + 1].text + " " + p["text"]).strip()
                    continue
            row[0].text = (row[0].text + " " + p["text"]).strip()
        rows.append(row)
    return rows, first_data


def _headers(grid: list[list[Cell]], first_data: int | None) -> list[str]:
    """The column headers of a grid, to tell a statement continuing on the next page (same headers, or none repeated) from a
    new table with other columns."""
    rows = grid[:first_data] if first_data is not None else [r for r in grid if any(c.header for c in r)]
    heads = [" ".join(r[k].text for r in rows if r[k].header).strip() for k in range(1, len(grid[0]))]
    return heads


def _header_phrases(words: list[dict]) -> list[dict]:
    """In a header line, numbers belong to the words beside them: "Jun 30, 2025" is one header, not three."""
    out: list[dict] = []
    for w in words:
        if out and w["x0"] - out[-1]["x1"] <= HEADER_WORD_GAP:
            out[-1]["text"] += " " + w["text"]
            out[-1]["x1"] = w["x1"]
        else:
            out.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"], "top": w["top"], "num": False, "value": False})
    return out


def _over(heads: list[dict], extent: list[tuple[float, float]]) -> dict[int, dict]:
    """Which header phrase of one line each column sits under: the nearest one, within reach. A phrase centred over two
    columns ("Six-month periods ended June" over "2025  2024") is then the header of both; a year over one column, of that one."""
    out = {}
    for k, (a, b) in enumerate(extent):
        centre = (a + b) / 2
        best = min(heads, key=lambda p: abs((p["x0"] + p["x1"]) / 2 - centre), default=None)
        if best is not None and (abs((best["x0"] + best["x1"]) / 2 - centre) <= HEADER_REACH * 1.5
                                 or best["x0"] - COLUMN_TOLERANCE <= centre <= best["x1"] + COLUMN_TOLERANCE):
            out[k] = best
    return out


def tables(body: bytes) -> list[RawTable]:
    """Every statement-like table in a PDF's text layer, page by page, with pages that continue a statement joined to it."""
    import pdfplumber
    out: list[RawTable] = []
    try:
        pdf = pdfplumber.open(io.BytesIO(body))
    except Exception:
        return []
    with pdf:
        for page_no, page in enumerate(pdf.pages[:MAX_PAGES], start=1):
            try:
                words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
            except Exception:
                continue
            if not words:
                continue
            for n, (lines, caption) in enumerate(_page_tables(_lines(words), page_no)):
                built = _grid(lines)
                if built is None:
                    continue
                grid, first_data = built
                cap = " ".join(r[0].text for r in grid[:first_data] if r[0].text)   # the title lines; column headers stay in their columns
                prev = out[-1] if out else None
                if (prev and n == 0 and not _titled(cap) and prev.locator.get("last_page") == page_no - 1
                        and len(prev.rows[0]) == len(grid[0])
                        and (not any(_headers(grid, first_data)) or _headers(grid, first_data) == _headers(prev.rows, None))):
                    prev.rows.extend(grid[first_data:])                    # the statement continues on this page
                    prev.locator["last_page"] = page_no
                    continue
                out.append(RawTable(grid, cap or caption, {"page": page_no, "table": n, "last_page": page_no}, "pdf"))
    return out
