"""A financial statement as the answer: which table read in a run answers 'the income statement, latest earnings', and the
table itself, laid out as printed. Code only; no model chooses or copies a figure.

    choose(tables, statement, basis, years) -> (table, why) | (None, why)
    view(table, source)                     -> {title, columns, rows, checks, ...} for the report, the app and Word

The table is the one the filing prints, row for row: every figure is the cell text as read (skills/statements), with its
verdict (verified against inline XBRL, agreed by the arithmetic, read, or in doubt). Nothing is recomputed or rounded.
"""

from __future__ import annotations

import re
from datetime import date

from skills.temporal import reporting

_READING = {"ixbrl": 3, "html": 2, "pdf": 1}
NAMES = {"income": "income statement", "comprehensive_income": "statement of comprehensive income", "balance": "balance sheet",
         "cash_flow": "cash flow statement", "equity": "statement of equity"}
READING_LABEL = {"ixbrl": "inline XBRL in the filing", "html": "the document's HTML table", "pdf": "the PDF's table (by where its words sit)"}


def _newest_end(t: dict) -> str:
    return max((c.get("end") or "" for c in t["columns"] if c.get("kind") in ("duration", "instant")), default="")


def _failed(t: dict) -> int:
    return sum(c["verdict"] == "fail" for c in t.get("checks") or [])


def _target(target: dict | None) -> reporting.Target:
    return reporting.Target(**{k: v for k, v in (target or {}).items() if k in reporting.Target.__dataclass_fields__})


def _periods(t: dict) -> str:
    """The periods a table's columns cover, in words: 'the 3 months to 2026-09-30, the 9 months to 2026-09-30'."""
    seen = []
    for c in t["columns"]:
        if c.get("end") and c.get("kind") in ("duration", "instant"):
            w = f"{c['months']} months to {c['end']}" if c.get("months") else f"{c['end']}"
            if w not in seen:
                seen.append(w)
    return ", ".join(seen[:4]) or "no dated column"


def fitting_columns(t: dict, target: dict | None, fye: str | None) -> list[int]:
    """The columns of a table that are the asked period (by the temporal skill); every dated column when none is asked."""
    want = _target(target)
    pe = None if want.latest else reporting.period_end(want, fye)
    if pe:
        return [c["index"] for c in t["columns"] if reporting.column_fits(c, pe[0], pe[1])]
    if not want.latest and want.fiscal_year:            # a year with no computable end: columns ending in that year
        return [c["index"] for c in t["columns"] if (c.get("end") or "")[:4] == str(want.fiscal_year)]
    return []


def choose(tables: list[dict], statement: str, basis: str = "consolidated", target: dict | None = None,
           fye: str | None = None, today: date | None = None) -> tuple[dict | None, str]:
    """The table that answers: the asked statement, titled as one (primary), of the asked basis where the document says it,
    for the asked period and announcement window (skills/temporal/reporting.py: a column must cover the period; the
    document must have come out in the window), else the newest; then the table whose own current period is the asked one
    (a later filing prints it only as a comparative), and the cleanest reading: no failed arithmetic, inline XBRL before
    HTML before PDF, the most figures. Each table may carry `came_out` (filing or publication date)."""
    want = _target(target)
    name = NAMES.get(statement, statement.replace("_", " "))
    pool = [t for t in tables if t["statement"] == statement and t.get("is_primary") and t.get("values")]
    if not pool:
        return None, f"no {name} was read from the documents of this run"
    same_basis = [t for t in pool if t.get("basis") == basis]
    pool = same_basis or [t for t in pool if not t.get("basis")] or pool
    if want.announced_from:
        pool = [t for t in pool if reporting.in_window(t.get("came_out"), want.announced_from, want.announced_to)]
        if not pool:
            return None, f"no {name} read came out between {want.announced_from} and {want.announced_to}"
    pe = None if want.latest else reporting.period_end(want, fye)
    if not want.latest and pe is None and want.basis == "fiscal" and not fye and want.span not in ("ytd", "ttm"):
        return None, (f"{want.label} cannot be dated: the company's fiscal year end is not known, and it is never guessed")
    if not want.latest:
        fits = {id(t): fitting_columns(t, target, fye) for t in pool}
        dated = [t for t in pool if fits[id(t)]]
        if not dated:
            newest = max(pool, key=_newest_end)
            if pe and pe[0] >= (today or date.today()):
                return None, (f"{want.label} has not ended yet (it ends about {pe[0]}), so no statement for it exists; "
                              f"the newest read covers {_periods(newest)}")
            return None, (f"no {name} read covers {want.label}"
                          f"{f' (ending about {pe[0]}, {pe[2]})' if pe else ''}; the newest read covers {_periods(newest)}")

        def own(t: dict) -> bool:                       # the asked period is the table's current one, not a comparative
            return reporting.near(_newest_end(t), pe[0]) if pe else True
        best = max(dated, key=lambda t: (own(t), _failed(t) == 0, _READING.get(t.get("adapter"), 0), len(t["values"])))
        period = f"{want.label}{f' (ending about {pe[0]}: {pe[2]})' if pe and want.span != 'instant' else ''}"
    else:
        best = max(pool, key=lambda t: (_newest_end(t), _failed(t) == 0, _READING.get(t.get("adapter"), 0), len(t["values"])))
        period = f"the newest period (to {_newest_end(best) or 'an unread date'})" + (
            f", from a document that came out between {want.announced_from} and {want.announced_to}" if want.announced_from else "")
    why = (f"the {best.get('basis') or 'stated'} {name} {'' if period.startswith('as of') else 'for '}{period}, read from {READING_LABEL.get(best.get('adapter'), best.get('adapter'))}"
           f"{'' if same_basis else '; the document does not state the asked basis'}"
           f"{f'; {_failed(best)} arithmetic check(s) failed' if _failed(best) else ''}")
    return best, why


def title_of(t: dict) -> str:
    """The statement's title as the document prints it ('Consolidated Statements of Income (Unaudited)'), from its caption."""
    caption = " ".join((t.get("caption") or "").split())
    core = re.escape(t.get("title") or "")
    if core:
        hits = list(re.finditer(rf"(?:(?:Condensed|Consolidated|Standalone|Separate|Combined)\s+)*{core}(?:\s*\((?:Unaudited|Audited)\))?",
                                caption, re.I))
        if hits:
            return hits[-1].group(0)
    basis = (t.get("basis") or "").capitalize()
    return f"{basis} {t.get('title') or 'Statement'}".strip()


def view(t: dict, source: dict, target: dict | None = None, fye: str | None = None) -> dict:
    """The table as printed, for every reader of the report. `source`: url, label, date, form, filed. The columns that are
    the asked period are marked `asked`, so they can be told apart from the comparatives the filing prints beside them."""
    asked = set(fitting_columns(t, target, fye))
    by_row: dict[int, dict] = {}
    used_cols = sorted({v["col_index"] for v in t["values"]})
    cols = [c for c in sorted(t["columns"], key=lambda c: c["index"]) if c["index"] in used_cols]
    for v in t["values"]:
        r = by_row.setdefault(v["row_index"], {"label": v["label"], "section": v.get("section") or "", "cells": {}})
        r["cells"][v["col_index"]] = {"printed": v["printed"], "verdict": v.get("verdict") or "read"}
    rows, section = [], None
    for i in sorted(by_row):
        r = by_row[i]
        sec = r["section"].strip()
        if sec and sec != section and sec.rstrip(":").lower() != r["label"].rstrip(":").lower():
            rows.append({"label": sec, "heading": True, "cells": []})
        section = sec or section
        rows.append({"label": r["label"], "heading": False,
                     "cells": [r["cells"].get(c["index"], {"printed": "", "verdict": ""}) for c in cols]})
    verdicts = [v.get("verdict") or "read" for v in t["values"]]
    checks = t.get("checks") or []
    return {
        "statement": t["statement"], "title": title_of(t), "basis": t.get("basis"), "scale": t.get("scale_raw"),
        "currency": t.get("currency"), "reading": t.get("adapter"), "reading_label": READING_LABEL.get(t.get("adapter"), t.get("adapter")),
        "columns": [{"header": " ".join((c.get("header") or c.get("label") or "").split()), "label": c.get("label") or "",
                     "kind": c.get("kind"), "end": c.get("end"), "months": c.get("months"), "asked": c["index"] in asked} for c in cols],
        "period": _target(target).label if target else "the latest reported period",
        "rows": rows,
        "figures": {k: verdicts.count(k) for k in ("verified", "agreed", "read", "doubt") if verdicts.count(k)},
        "checks": {"passed": sum(c["verdict"] == "pass" for c in checks), "failed": sum(c["verdict"] == "fail" for c in checks)},
        "notes": list(t.get("notes") or []),
        "table_id": t.get("table_id"), "source_sha256": t.get("source_sha256"),
        "source_url": source.get("url") or "", "source_label": source.get("label") or "", "source_date": source.get("date"),
        "form": source.get("form"), "filed": source.get("filed"),
    }


def describe(v: dict, company: str) -> str:
    """One line saying what the table is and where it is from, for above the table."""
    where = f"Form {v['form']} filed {v['filed']}" if v.get("form") and v.get("filed") else (v.get("source_label") or "the source")
    scale = f" ({v['scale']}{', ' + v['currency'] if v.get('currency') and v['currency'] not in (v.get('scale') or '') else ''})" if v.get("scale") else ""
    owner = company + ("'" if company.endswith("s") else "'s")
    return f"{owner} {v['title']}{scale}, from its {where}."
