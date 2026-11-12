"""The answer on its own: the written answer with numbered citations, the sources behind them, and a Word copy.

Built from a finished report by code. Nothing is reworded: the answer is the writer's text, with each
[claim:N] marker replaced by the number of its source, and each source keeps the exact quotes it backs.
The run itself (every step, every check) stays on the Runs page.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

# The writer's own in-text source note, "(Web page by x, dated y, https://...)", just before a marker: the number replaces it.
_NOTE = re.compile(r"\s*\((?:[^()]|\([^()]*\))*?https?://(?:[^()]|\([^()]*\))*\)(?=\s*\[claim:\d+\])")
# What code appends after the answer, always last: a brief's "Sources:" list, or a deep report's "## Sources",
# "## How the research was done" and "## Not established". The numbered list replaces the sources; the rest is for Runs.
_APPENDIX = re.compile(r"(?ms)^[ \t]*(?:Sources:|#{1,3}[ \t]+Sources)[ \t]*$.*\Z")
# What was not found is for the Runs page: a section with no answer, and the closing list of what was not established.
_EMPTY_SECTION = re.compile(r"(?m)^#{1,3}[ \t]+[^\n]*\n\s*Not established from what was read\.[ \t]*(?:\n|$)")
_NOT_FOUND_LINE = re.compile(r"(?m)^[ \t]*Not established from what was read:[^\n]*(?:\n|$)")

OUTCOME_TEXT = {
    "answered": "Answered",
    "partial": "Partly answered",
    "not_found": "Not found",
    "premise_unverified": "The question's premise could not be confirmed",
}


def _claims(report: dict) -> dict[int, tuple[dict, bool]]:
    """Every claim the answer may cite, with whether it needs caution (only partly supported)."""
    found: dict[int, tuple[dict, bool]] = {}
    for key, caution in (("established", False), ("context", False), ("reference_only", False), ("poorly_sourced", True), ("partial", True)):
        for c in report.get(key) or []:
            found.setdefault(int(c["claim_id"]), (c, caution))
    return found


def build(report: dict) -> dict:
    """The answer, numbered by source in order of first citation, and the sources with the quotes each one backs."""
    claims = _claims(report)
    text = (report.get("summary") or "").strip()
    text = _APPENDIX.sub("", text)
    text = _EMPTY_SECTION.sub("", text)
    text = _NOT_FOUND_LINE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = _NOTE.sub("", text)

    numbers: dict[str, int] = {}            # source url -> its number
    sources: list[dict] = []

    def number_of(claim_id: int) -> int | None:
        hit = claims.get(claim_id)
        if not hit:
            return None
        c, caution = hit
        url = c.get("source_url") or f"claim:{claim_id}"
        if url not in numbers:
            numbers[url] = len(sources) + 1
            sources.append({"n": numbers[url], "url": c.get("source_url") or "", "label": c.get("source_label") or _site(url),
                            "date": c.get("source_date"), "corroboration": c.get("corroboration") or "", "quotes": []})
        src = sources[numbers[url] - 1]
        if not any(q["claim_id"] == claim_id for q in src["quotes"]):
            src["quotes"].append({"claim_id": claim_id, "quote": c.get("quote") or c.get("claim") or "", "caution": caution})
        return numbers[url]

    tables = {int(t["n"]): t for t in report.get("tables") or [] if t.get("n")}
    shown: list[dict] = []

    def table_source(n: int) -> int | None:
        t = tables.get(n)
        if not t:
            return None
        url = t.get("source_url") or f"table:{n}"
        if url not in numbers:
            numbers[url] = len(sources) + 1
            sources.append({"n": numbers[url], "url": t.get("source_url") or "", "label": t.get("source_label") or _site(url),
                            "date": t.get("source_date"), "corroboration": "", "quotes": [], "tables": []})
        src = sources[numbers[url] - 1]
        src.setdefault("tables", []).append(t["title"])
        shown.append({**t, "source_n": numbers[url]})
        return numbers[url]

    def swap(m: re.Match) -> str:
        if m.group(1) == "table":
            n = table_source(int(m.group(2)))
            return f"[table:{len(shown)}]" if n else ""        # the renderer puts the table here
        n = number_of(int(m.group(2)))
        return f"[{n}]" if n else ""

    text = re.sub(r"\[(claim|table):(\d+)\]", swap, text)
    text = re.sub(r"\[(\d+)\](\s*\[\1\])+", r"[\1]", text)      # the same source cited twice in a row: once
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    notes: list[str] = []
    t = report.get("time") or {}
    if t.get("stale"):
        notes.append(f"The newest source is {t.get('age_days')} days old; there may be more recent news.")
    for c in report.get("conflicts") or []:
        notes.append(f"Sources disagree: {c.get('note')}.")
    if report.get("stopped_early"):
        notes.append("This run was stopped early; the answer covers only what was gathered before the stop.")
    if any(q["caution"] for s in sources for q in s["quotes"]):
        notes.append("Quotes marked “partly supported” back only part of what is said; treat them with caution.")
    return {
        "question": report.get("question") or "",
        "subject": report.get("subject") or "",
        "outcome": report.get("outcome") or ("answered" if text else "not_found"),
        "outcome_text": OUTCOME_TEXT.get(report.get("outcome") or "", ""),
        "as_of": t.get("today"),
        "answer": text,
        "sources": sources,
        "tables": shown,                     # [table:N] in `answer` is shown[N-1]
        "notes": notes,
    }


def _site(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url


# ---- Word -------------------------------------------------------------------------------------------

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _run(text: str, *, bold=False, italic=False, size=None, color=None, style=None) -> str:
    props = "".join([f'<w:rStyle w:val="{style}"/>' if style else "", "<w:b/>" if bold else "", "<w:i/>" if italic else "",
                     f'<w:color w:val="{color}"/>' if color else "", f'<w:sz w:val="{size}"/>' if size else ""])
    return f'<w:r>{f"<w:rPr>{props}</w:rPr>" if props else ""}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _para(runs: str, *, style=None, indent=None, space_after=None, keep_next=False) -> str:
    props = "".join([f'<w:pStyle w:val="{style}"/>' if style else "", "<w:keepNext/>" if keep_next else "",
                     f'<w:spacing w:after="{space_after}"/>' if space_after is not None else "",
                     f'<w:ind w:left="{indent}" w:hanging="{indent // 2 if style == "Bullet" else 0}"/>' if indent else ""])
    return f'<w:p>{f"<w:pPr>{props}</w:pPr>" if props else ""}{runs}</w:p>'


def _inline(line: str, *, bold=False) -> str:
    """A line of the answer: citation numbers in superscript-free brackets, kept as plain text so they copy cleanly."""
    return "".join(_run(part, bold=bold, color="2B579A" if re.fullmatch(r"\[\d+\]", part) else None)
                   for part in re.split(r"(\[\d+\])", line) if part)


def figures_text(t: dict) -> str:
    f, c = t.get("figures") or {}, t.get("checks") or {}
    said = {"verified": "matched to the filing's XBRL tags", "agreed": "agreed by the arithmetic", "read": "read as printed", "doubt": "in doubt"}
    parts = [f"{n} {said[k]}" for k, n in f.items() if n]
    checks = f"arithmetic checks: {c.get('passed', 0)} passed, {c.get('failed', 0)} failed" if (c.get("passed") or c.get("failed")) else ""
    return "; ".join(x for x in [f"Read by code from {t.get('reading_label') or 'the document'}", ", ".join(parts), checks] if x) + "."


def _cell(text: str, width: int, *, bold=False, right=False, shade=None, span=1, color=None, header=False) -> str:
    props = (f'<w:tcW w:w="{width}" w:type="dxa"/>' + (f'<w:gridSpan w:val="{span}"/>' if span > 1 else "")
             + (f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>' if shade else "") + '<w:vAlign w:val="bottom"/>')
    jc = '<w:jc w:val="right"/>' if right else ""
    ppr = f'<w:pPr><w:spacing w:before="20" w:after="20" w:line="240" w:lineRule="auto"/>{jc}</w:pPr>'
    run = _run(text, bold=bold or header, size=17, color=color) if text else ""
    return f"<w:tc><w:tcPr>{props}</w:tcPr><w:p>{ppr}{run}</w:p></w:tc>"


def _table_xml(t: dict) -> str:
    cols = t.get("columns") or []
    total = 9638                                  # the text width of an A4 page with 2 cm margins, in twentieths of a point
    label_w = 3600 if len(cols) <= 4 else 2800
    col_w = (total - label_w) // max(1, len(cols))
    grid = f'<w:tblGrid><w:gridCol w:w="{label_w}"/>' + "".join(f'<w:gridCol w:w="{col_w}"/>' for _ in cols) + "</w:tblGrid>"
    border = '<w:{0} w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>'
    tblpr = ('<w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:tblBorders>'
             + "".join(border.format(b) for b in ("top", "bottom", "insideH")) + '</w:tblBorders>'
             '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:left w:w="60" w:type="dxa"/><w:right w:w="60" w:type="dxa"/></w:tblCellMar></w:tblPr>')
    head = ('<w:tr><w:trPr><w:tblHeader/></w:trPr>' + _cell(t.get("scale") or "", label_w, header=True, shade="F2F2F2", color="595959")
            + "".join(_cell(c["header"] or c["label"], col_w, header=True, right=True, shade="DCE6F2" if c.get("asked") else "F2F2F2")
                      for c in cols) + "</w:tr>")
    body = []
    for r in t.get("rows") or []:
        if r.get("heading"):
            body.append("<w:tr>" + _cell(r["label"], total, bold=True, span=len(cols) + 1) + "</w:tr>")
            continue
        total_line = bool(re.match(r"(?:total|net income|net (?:cash|increase|decrease)|operating profit)", r["label"], re.I))
        cells = "".join(_cell(c["printed"] + (" ?" if c.get("verdict") == "doubt" else ""), col_w, right=True, bold=total_line,
                              color="9C5700" if c.get("verdict") == "doubt" else None,
                              shade="EEF3FA" if j < len(cols) and cols[j].get("asked") else None) for j, c in enumerate(r["cells"]))
        body.append("<w:tr><w:trPr><w:cantSplit/></w:trPr>" + _cell(r["label"], label_w, bold=total_line) + cells + "</w:tr>")
    return f"<w:tbl>{tblpr}{grid}{head}{''.join(body)}</w:tbl>"


def to_docx(view: dict, *, run_id: str = "") -> bytes:
    """A Word document of the answer and its sources. Written with the standard library: no extra dependency."""
    links: list[str] = []

    def link(url: str, text: str) -> str:
        links.append(url)
        rid = f"rIdL{len(links)}"
        return f'<w:hyperlink r:id="{rid}" w:history="1">{_run(text, style="Hyperlink")}</w:hyperlink>'

    body: list[str] = [_para(_run(view["question"] or "Answer"), style="Title")]
    meta = " · ".join(x for x in [f"As of {view['as_of']}" if view.get("as_of") else "", view.get("outcome_text") or "",
                                   f"Titan run {run_id[:8]}" if run_id else ""] if x)
    if meta:
        body.append(_para(_run(meta, color="666666", size=18), space_after=240))

    body.append(_para(_run("Answer"), style="Heading1"))
    answer = view.get("answer") or ""
    if not answer:
        body.append(_para(_run("No answer could be established from sources Titan could read and quote.", italic=True)))
    for line in answer.split("\n"):
        h = re.match(r"^(#{1,3})\s+(.*)$", line)
        tm = re.fullmatch(r"\s*\[table:(\d+)\]\s*", line)
        if tm and 0 < int(tm[1]) <= len(view.get("tables") or []):
            t = view["tables"][int(tm[1]) - 1]
            body.append(_table_xml(t))
            shaded = (f"Shaded: {t.get('period')}, the period asked for; the other columns are the filing's comparatives. "
                      if any(c.get("asked") for c in t.get("columns") or []) else "")
            body.append(_para(_run(f"{shaded}{figures_text(t)} Source [{t['source_n']}].", color="666666", size=16), space_after=240))
            for n in t.get("notes") or []:
                body.append(_para(_run(f"Note: {n}", color="666666", size=16)))
        elif h:
            body.append(_para(_inline(h[2]), style="Heading2" if len(h[1]) <= 2 else "Heading3", keep_next=True))
        elif re.match(r"^\s*[-*]\s+", line):
            body.append(_para(_run("•\t") + _inline(re.sub(r"^\s*[-*]\s+", "", line)), style="Bullet", indent=720))
        elif line.strip().endswith(":") and len(line) < 80:
            body.append(_para(_inline(line.strip(), bold=True), keep_next=True, space_after=60))
        elif line.strip():
            body.append(_para(_inline(line)))

    if view.get("notes"):
        body.append(_para(_run("Notes"), style="Heading1"))
        for n in view["notes"]:
            body.append(_para(_run("•\t") + _run(n), style="Bullet", indent=720))

    if view.get("sources"):
        body.append(_para(_run("Sources"), style="Heading1"))
        for s in view["sources"]:
            head = _run(f"[{s['n']}] ", bold=True) + _run(s["label"] + (f", dated {s['date']}" if s.get("date") else "") + ". ")
            body.append(_para(head + (link(s["url"], s["url"]) if s.get("url") else ""), keep_next=True, space_after=60))
            for title in s.get("tables") or []:
                body.append(_para(_run(f"Table: {title}", italic=True), indent=567, space_after=60))
            for q in s["quotes"]:
                caution = " (partly supported)" if q.get("caution") else ""
                body.append(_para(_run(f"“{q['quote']}”", italic=True) + _run(caution, color="9C5700"), indent=567, space_after=60))
            body.append(_para("", space_after=60))

    body.append(_para(_run("Every quote above appears word for word in the archived copy of its source. "
                           "How each one was found and checked is recorded in the run on Titan's Runs page.", color="666666", size=16)))

    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_W}><w:body>{"".join(body)}'
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
                'w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            + "".join(f'<Relationship Id="rIdL{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                      f'Target="{escape(u, {chr(34): "&quot;"})}" TargetMode="External"/>' for i, u in enumerate(links, 1))
            + "</Relationships>")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{escape(view["question"] or "Answer")}</dc:title><dc:creator>Titan</dc:creator>'
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created></cp:coreProperties>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _PACKAGE_RELS)
        z.writestr("docProps/core.xml", core)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/_rels/document.xml.rels", rels)
    return buf.getvalue()


def filename(view: dict) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "-", view.get("question") or "answer").strip("-")[:60] or "answer"
    return f"{stem}.docx"


_CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                  '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                  '<Default Extension="xml" ContentType="application/xml"/>'
                  '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                  '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                  '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>')
_PACKAGE_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                 '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                 '</Relationships>')


def _style(sid: str, name: str, rpr: str, ppr: str = "", kind: str = "paragraph", based: str = "Normal") -> str:
    based_on = f'<w:basedOn w:val="{based}"/>' if based else ""
    return (f'<w:style w:type="{kind}" w:styleId="{sid}"><w:name w:val="{name}"/>{based_on}'
            f'<w:qFormat/>{f"<w:pPr>{ppr}</w:pPr>" if ppr else ""}<w:rPr>{rpr}</w:rPr></w:style>')


_STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri" w:eastAsia="Calibri"/>'
           '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-GB"/></w:rPr></w:rPrDefault>'
           '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
           + _style("Normal", "Normal", "", based="")
           + _style("Title", "Title", '<w:sz w:val="40"/><w:color w:val="1F3864"/>', '<w:spacing w:after="80"/>')
           + _style("Heading1", "heading 1", '<w:b/><w:sz w:val="30"/><w:color w:val="1F3864"/>', '<w:keepNext/><w:spacing w:before="320" w:after="120"/><w:outlineLvl w:val="0"/>')
           + _style("Heading2", "heading 2", '<w:b/><w:sz w:val="26"/><w:color w:val="2F5496"/>', '<w:keepNext/><w:spacing w:before="240" w:after="80"/><w:outlineLvl w:val="1"/>')
           + _style("Heading3", "heading 3", '<w:b/><w:sz w:val="23"/>', '<w:keepNext/><w:spacing w:before="200" w:after="60"/><w:outlineLvl w:val="2"/>')
           + _style("Bullet", "List Paragraph", "", '<w:spacing w:after="80"/><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs>')
           + _style("Hyperlink", "Hyperlink", '<w:color w:val="0563C1"/><w:u w:val="single"/>', kind="character", based="")
           + "</w:styles>")
