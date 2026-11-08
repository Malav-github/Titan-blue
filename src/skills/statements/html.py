"""HTML tables, and the inline XBRL facts inside them, turned into raw grids.

Inline XBRL (SEC 10-Q and 10-K, ESEF in Europe, and every market that files in it) puts each reported number in an
<ix:nonFraction> element that names its concept, its period (through a context), its unit, its scale and its sign. Those
are read exactly, so a value from a filing never depends on reading a header. Plain HTML tables (earnings releases) get the
same grid without facts and are read from their printed headers.

Grids keep the printed layout: colspans are spread so a header sits over every column it covers, and the cells filings
split off a number ("$" before it, ")" or "%" after it) are joined back to it.
"""

import re
from decimal import Decimal, InvalidOperation

from lxml import etree, html as lhtml

from .numbers import parse_amount
from .tables import Cell, RawTable

MAX_CAPTION = 700
MAX_COLSPAN = 40


def _local(tag) -> str:
    return tag.split("}")[-1].split(":")[-1].lower() if isinstance(tag, str) else ""


def _text(el) -> str:
    return " ".join("".join(el.itertext()).replace("\xa0", " ").split())


# --- inline XBRL contexts and facts -----------------------------------------------------------------------------------

def _contexts(root) -> dict:
    out = {}
    for ctx in root.iter():
        if _local(ctx.tag) != "context":
            continue
        period, dims = {}, []
        for el in ctx.iter():
            name = _local(el.tag)
            if name in ("startdate", "enddate", "instant"):
                period[{"startdate": "start", "enddate": "end", "instant": "instant"}[name]] = (el.text or "").strip()[:10]
            elif name in ("explicitmember", "typedmember"):
                dims.append([el.get("dimension", ""), (el.text or _text(el)).strip()])
        out[ctx.get("id")] = {"period": period, "dims": dims}
    return out


def _units(root) -> dict:
    out = {}
    for u in root.iter():
        if _local(u.tag) == "unit":
            measures = [(m.text or "").strip().split(":")[-1] for m in u.iter() if _local(m.tag) == "measure"]
            out[u.get("id")] = "/".join(measures)
    return out


_FIXED_ZERO = re.compile(r"(?:fixed-?zero|zerodash|zero-?dash|nocontent)$", re.I)


def _fact_number(text: str, fmt: str) -> Decimal | None:
    """The number an <ix:nonFraction> shows, read by its declared format (num-dot-decimal: 1,234.5; num-comma-decimal: 1.234,5)."""
    fmt = (fmt or "").split(":")[-1].lower()
    t = text.strip()
    if _FIXED_ZERO.search(fmt) or re.fullmatch(r"[-‒-―−]+", t):
        return Decimal(0)
    if "comma-decimal" in fmt or "numcommadecimal" in fmt:
        t = re.sub(r"[.\s\xa0]", "", t).replace(",", ".")
    else:
        t = re.sub(r"[,\s\xa0]", "", t)
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _fact(el, contexts: dict, units: dict) -> dict | None:
    printed = _fact_number(_text(el), el.get("format", ""))
    if printed is None:
        return None
    if el.get("sign") == "-":
        printed = -printed
    scale = int(el.get("scale") or 0)
    ctx = contexts.get(el.get("contextref") or el.get("contextRef"), {})
    return {"name": el.get("name", ""), "context": el.get("contextref") or el.get("contextRef"), "id": el.get("id", ""),
            "unit": units.get(el.get("unitref") or el.get("unitRef"), ""), "decimals": el.get("decimals"), "scale": scale,
            "sign": el.get("sign", ""), "printed": format(printed, "f"), "value": format(printed.scaleb(scale), "f"),
            "period": ctx.get("period", {}), "dims": ctx.get("dims", [])}


# --- grids ------------------------------------------------------------------------------------------------------------

def _parse(body: bytes):
    """UTF-8 first (a page that declares no charset is read as Latin-1 by the HTML parser, which turns "—" into three
    characters), then the bytes as they are."""
    try:
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", body.decode("utf-8"))
        return lhtml.fromstring(text)
    except UnicodeDecodeError:
        pass
    except (etree.ParserError, ValueError):
        return None
    try:
        return lhtml.fromstring(body)
    except (etree.ParserError, ValueError):
        return None


def _caption(table) -> str:
    """The text printed just before a table: walk back through earlier siblings, then the parent's, until enough is read."""
    parts, size, node = [], 0, table
    while node is not None and size < MAX_CAPTION:
        for sib in node.itersiblings(preceding=True):
            if _local(sib.tag) == "table" or any(_local(d.tag) == "table" for d in sib.iterdescendants()):
                return " ".join(reversed(parts))[-MAX_CAPTION:]       # the previous table's numbers are not this one's title
            t = _text(sib)
            if t:
                parts.append(t)
                size += len(t)
            if size >= MAX_CAPTION:
                break
        node = node.getparent()
    return " ".join(reversed(parts))[-MAX_CAPTION:]


def _hidden(el) -> bool:
    for a in el.iterancestors():
        if re.search(r"display:\s*none", a.get("style", ""), re.I):
            return True
    return False


def _grid(table, contexts: dict, units: dict) -> list[list[Cell]]:
    rows: list[list[Cell]] = []
    pending: dict[int, tuple[int, Cell]] = {}           # column -> (rows still covered, cell) for rowspans
    for tr in table.iter():
        if _local(tr.tag) != "tr" or next((a for a in tr.iterancestors() if _local(a.tag) == "table"), None) is not table:
            continue
        row: list[Cell] = []
        col = 0
        for td in tr:
            if _local(td.tag) not in ("td", "th"):
                continue
            while col in pending:
                left, cell = pending.pop(col)
                row.append(Cell(cell.text, None, cell.header))
                if left > 1:
                    pending[col] = (left - 1, cell)
                col += 1
            facts = [f for f in (_fact(el, contexts, units) for el in td.iter() if _local(el.tag) == "nonfraction") if f]
            cell = Cell(_text(td), facts[0] if len(facts) == 1 else None, _local(td.tag) == "th")
            span = min(int(td.get("colspan", "1") or 1), MAX_COLSPAN) if str(td.get("colspan", "1")).isdigit() else 1
            rspan = int(td.get("rowspan", "1")) if str(td.get("rowspan", "1")).isdigit() else 1
            for k in range(span):
                # a heading spread over the columns it covers; a value only in its first column
                spread = not facts and (parse_amount(cell.text) is None or re.fullmatch(r"(?:19|20)\d{2}", cell.text.strip()))
                c = cell if k == 0 else Cell(cell.text if spread else "", None, cell.header)
                row.append(c)
                if rspan > 1:
                    pending[col] = (rspan - 1, c)
                col += 1
        while col in pending:
            left, cell = pending.pop(col)
            row.append(Cell(cell.text, None, cell.header))
            if left > 1:
                pending[col] = (left - 1, cell)
            col += 1
        rows.append(row)
    return [_join_split_cells(r) for r in rows if any(c.text for c in r)]


_NUMBERISH = re.compile(r"^\(?[-−]?[$₹₱]?\s?\d[\d,.\s]*\)?%?$")


def _is_number(text: str) -> bool:
    return bool(_NUMBERISH.match(text.strip()))


def _join_split_cells(row: list[Cell]) -> list[Cell]:
    """"$" | "3,678" | ")" printed as three cells become one cell "$3,678)" -> with the opening bracket, "(3,678)"."""
    out = [Cell(c.text, c.fact, c.header) for c in row]
    for k, c in enumerate(out):
        t = c.text.strip()
        if t in (")", "%", ")%", "%)") and k > 0:
            prev = next((p for p in range(k - 1, -1, -1) if out[p].text.strip()), None)
            if prev is not None and _is_number(out[prev].text.replace(")", "").replace("%", "") or "x"):
                out[prev].text = out[prev].text.rstrip() + t
                c.text = ""
        elif t in ("$", "₹", "₱", "Rs.", "Rs", "US$", "(", "($", "$(") and k + 1 < len(out):
            nxt = next((p for p in range(k + 1, len(out)) if out[p].text.strip()), None)
            if nxt is not None and _is_number(out[nxt].text):
                out[nxt].text = ("(" if "(" in t else "") + out[nxt].text.strip()
                c.text = ""
    return out


def tables(body: bytes, *, include_hidden: bool = False) -> list[RawTable]:
    """Every table in an HTML or inline XBRL document, in order, as raw grids with their captions."""
    root = _parse(body)
    if root is None:
        return []
    contexts, units = _contexts(root), _units(root)
    adapter = "ixbrl" if contexts else "html"
    out = []
    for n, t in enumerate(el for el in root.iter() if _local(el.tag) == "table"):
        if not include_hidden and _hidden(t):
            continue
        grid = _grid(t, contexts, units)
        if len(grid) >= 2:
            out.append(RawTable(grid, _caption(t), {"table": n}, adapter))
    return out


def facts(body: bytes) -> list[dict]:
    """Every inline XBRL number in the document, wherever it is printed (tables and sentences). Part 2 indexes these."""
    root = _parse(body)
    if root is None:
        return []
    contexts, units = _contexts(root), _units(root)
    return [f for f in (_fact(el, contexts, units) for el in root.iter() if _local(el.tag) == "nonfraction") if f]
