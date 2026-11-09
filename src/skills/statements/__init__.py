"""Financial statements read by code from the documents companies publish: inline XBRL filings, HTML earnings releases and PDF
reports. A skill that covers for the local model, which cannot read wide tables reliably: no model touches a number here.

    extract(body, content_type) -> [Table]       every table in the document, interpreted and checked

Each table says which statement it is (and whether it is titled as one), its basis (consolidated or standalone), the period
of every column, the line item of every row it could name, its scale and currency, and the arithmetic checks it passed or
failed. What could not be settled is said in `notes`, never filled in.

The definitions live in definitions/ontology/financial_statements.json (statements, line items, tags, labels, scale words,
rules) and definitions/skills/statements.json (settings). A new market is added there: its labels, scale words and tags.
"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from config import PROJECT_ROOT

from . import checks, html, pdf
from .tables import Table, interpret

VERSION = "statements-v1"


@lru_cache(maxsize=1)
def manifest() -> dict:
    return json.loads((PROJECT_ROOT / "definitions" / "skills" / "statements.json").read_text(encoding="utf-8"))


def raw_tables(body: bytes, content_type: str = "", report: dict | None = None):
    if content_type == "application/pdf" or body[:5] == b"%PDF-":
        return pdf.tables(body, report, manifest()["settings"]["max_ocr_pages"])
    if "html" in content_type or "xml" in content_type or body.lstrip()[:1] == b"<":
        return html.tables(body)
    return []


def extract(body: bytes, content_type: str = "", market: str | None = None, report: dict | None = None) -> list[Table]:
    """Every table with values, interpreted, with its checks in `table.checks`. A statement that does not state its scale
    takes the one the same document states on another statement printed under the same heading, said in its notes; it is
    never given one otherwise. `market` (US, IN, PH) brings the market's conventions: its decimal mark and, where the law
    fixes one, its financial year (India: April to March), used only for headers that name a fiscal period without dates."""
    settings = manifest()["settings"]
    decimal_mark = settings["decimal_mark_by_market"].get(market or "")
    fye = settings["fiscal_year_end_by_market"].get(market or "")
    out = []
    for raw in raw_tables(body, content_type, report):
        t = interpret(raw, decimal_mark, fye)
        if t is not None:
            out.append(t)
    _share_document_facts(out)
    if market is None and (found := market_of_currency(out)) and (fye := settings["fiscal_year_end_by_market"].get(found)):
        for t in out:
            date_fiscal_columns(t, fye, f"the document's currency ({found})")
    for t in out:
        t.checks = checks.run(t)
        checks.grade(t)
    return out


def titles_a_statement(text: str) -> bool:
    """Whether a document's text titles a financial statement ("Consolidated Statements of Financial Position"): cheap, so
    every fetched page can be asked before its tables are read."""
    import re
    from ontology import financial_statements
    head = text[:400_000]
    return any(m[0][:1].isupper() for s in financial_statements()["statements"] for m in re.finditer(s["title"], head, re.I | re.M))


def market_of_host(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for suffix, market in manifest()["settings"]["market_by_host"].items():
        if host == suffix or host.endswith("." + suffix):
            return market
    return None


def market_of_currency(tables: list[Table]) -> str | None:
    """The market a document's statements are reported in, from the currency they state (INR -> IN). Only when every
    primary statement that states a currency states the same one."""
    codes = {t.scale.currency for t in tables if t.primary and t.scale and t.scale.currency}
    by_currency = manifest()["settings"]["market_by_currency"]
    return by_currency.get(codes.pop()) if len(codes) == 1 else None


def date_fiscal_columns(table: Table, fye: str, why: str) -> None:
    """Date the columns that name a fiscal period without dates ("Q2 FY26") from a statutory financial year."""
    from .tables import read_header
    for c in table.columns:
        if c.kind == "duration" and not c.end and c.origin == "header":
            again = read_header(c.header, fiscal_year_end=fye)
            if again.end:
                c.start, c.end, c.months = again.start, again.end, c.months or again.months
                c.derived = f"{again.derived}; the market was taken from {why}"


def read_source(store, source_sha256: str, url: str = "", market: str | None = None) -> dict:
    """Read the statements in an archived source and record them in the evidence store. The archived bytes are checked
    against their hash first, as for any claim. Returns a summary for the run's log."""
    src = store.get_source(source_sha256)
    if src is None:
        return {"ok": False, "reason": "unknown source"}
    body = Path(src["archive_path"]).read_bytes()
    if hashlib.sha256(body).hexdigest() != source_sha256:
        return {"ok": False, "reason": "archive file no longer matches its hash"}
    market = market or market_of_host(url or src["final_url"])
    report: dict = {}
    tables = [t for t in extract(body, src["content_type"] or "", market, report) if t.primary or t.values]
    keep = [t for t in tables if t.primary] + [t for t in tables if not t.primary][:manifest()["settings"]["max_other_tables"]]
    ids = store.record_statements(source_sha256, keep, VERSION)
    primary = [t for t in keep if t.primary]
    doubts = [doubt_row(t, v) for t in primary for v in t.values if v.verdict == "doubt"]
    return {"ok": True, "tables": len(keep), "statements": [
        {"statement": t.statement, "title": t.title, "basis": t.basis, "currency": t.scale.currency if t.scale else None,
         "scale": t.scale.raw if t.scale else None, "locator": t.raw.locator, "reading": t.raw.adapter,
         "periods": [c.label for c in t.columns if c.kind != "change"],
         "checks_passed": sum(c.verdict == "pass" for c in t.checks), "checks_failed": sum(c.verdict == "fail" for c in t.checks),
         "verdicts": {k: sum(v.verdict == k for v in t.values) for k in ("verified", "agreed", "read", "doubt")},
         "notes": t.notes} for t in primary], "table_ids": ids, "market": market,
        "doubt_count": len(doubts), "doubts": doubts[:manifest()["settings"]["max_doubts_reported"]],
        "ocr_pages": report.get("ocr_pages", []), "unread_pages": report.get("unread_pages", [])}


def doubt_row(t: Table, v) -> dict:
    """One figure in doubt, said so a person can check it: where it is, what each reader saw, the reading it is taken as,
    and why it is doubted."""
    row = next(r for r in t.rows if r.index == v.row)
    col = next(c for c in t.columns if c.index == v.col)
    where = f", page {t.raw.locator['page']}" if t.raw.locator.get("page") else ""
    p = v.proposed
    printed = p["printed"] if p else v.amount.raw
    taken = ("" if p and p["changed"] else f"kept as read ({p['why']}); " if p else "")      # a change is in the reasons
    return {"summary": f"{row.label or '(unlabelled line)'}, {col.label or 'period unread'}{where}: printed '{printed}'; "
                       f"{taken}{'; '.join(v.reasons)}; please double-check",
            "statement": t.statement, "line": row.label, "period": col.label, "printed": printed,
            "taken_as": p["reading"] if p else v.amount.raw, "value": str(v.full) if v.full is not None else None,
            "readings": {k: v.ocr.get(k) for k in ("tesseract", "rapidocr")} if v.ocr else None,
            "page": t.raw.locator.get("page"), "box": (v.ocr or {}).get("page_box"), "reasons": v.reasons}


def _share_document_facts(tables: list[Table]) -> None:
    from decimal import Decimal

    primaries = [t for t in tables if t.primary]
    stated = [t for t in primaries if t.scale and t.scale.factor is not None]
    if not stated:
        return
    for t in primaries:
        # only from a statement printed under the same heading (the same company line): a summary form elsewhere in the
        # filing may be in thousands while the statements are in units
        first = next((s for s in stated if s is not t and _heading(s) and _heading(s) == _heading(t)), None)
        if first is None:
            continue
        if t.scale is None or t.scale.factor is None:
            t.scale = first.scale
            where = first.raw.locator
            t.notes = [n for n in t.notes if "does not state its scale" not in n]
            t.notes.append(f"scale '{first.scale.raw}' taken from the {first.statement} statement of the same document "
                           f"({', '.join(f'{k} {v}' for k, v in where.items() if k != 'last_page')}), which states it")
            for v in t.values:
                if v.full is None and v.unit in ("", first.scale.currency or "") and v.fact is None:
                    v.full = v.amount.value * (first.scale.factor or Decimal(1))
                    v.unit = first.scale.currency or v.unit
        if t.basis is None and first.basis:
            t.basis = first.basis
            t.notes.append(f"basis '{first.basis}' taken from the statement printed under the same heading")


def _heading(t: Table) -> str:
    """The company line a statement is printed under: the caption up to its title."""
    cap = " ".join(t.raw.caption.split())
    i = cap.find(t.title) if t.title else -1
    return cap[:i].strip()[-120:].lower() if i > 0 else ""
