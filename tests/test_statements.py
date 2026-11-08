"""Financial statements read by code (skills/statements): numbers as different markets print them, periods from headers and
captions, inline XBRL facts, HTML and PDF tables, arithmetic checks, and storage in the evidence store.

Every case here is one a real document showed (S&P Global's 10-Q and earnings release, a Philippine 17-Q, an Indian results
summary), rebuilt with fictional companies and numbers.
"""

from decimal import Decimal

import pytest

from evidence import EvidenceStore
from skills import statements
from skills.statements import checks
from skills.statements.html import tables as html_tables
from skills.statements.numbers import find_scale, parse_amount
from skills.statements.tables import interpret, read_header
from tools.archive import archive_bytes, utc_now


# --- numbers ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("printed, value, negative", [
    ("3,678", "3678", False),
    ("(93)", "-93", True),                      # a bracketed number is negative, never a footnote "(93)"
    ("(1,375,345,485)", "-1375345485", True),
    ("12,34,567", "1234567", False),            # Indian grouping
    ("1.234.567,89", "1234567.89", False),      # comma as decimal mark
    ("1 234 567", "1234567", False),
    ("−58", "-58", True),                       # a real minus sign
    ("—", "0", False),                          # a dash is printed for nil
    ("4.45", "4.45", False),
    ("1,234 (a)", "1234", False),               # a footnote mark after a number
    ("٣٬٦٧٨", None, None),                      # Arabic thousands separator is not one we read: unread, not guessed
    ("$3,678", "3678", False),
    ("₹ 6,416.62", "6416.62", False),
])
def test_numbers_as_markets_print_them(printed, value, negative):
    a = parse_amount(printed)
    if value is None:
        assert a is None
    else:
        assert a.value == Decimal(value) and a.negative == negative


def test_arabic_indic_digits_and_percentages():
    assert parse_amount("٣٦٧٨").value == 3678
    a = parse_amount("16.5%")
    assert a.percent and a.value == Decimal("16.5")


def test_a_lone_dot_group_is_unread_unless_the_market_says():
    assert parse_amount("1.234") is None                       # a thousand, or one point two three four?
    assert parse_amount("1.234", decimal_mark=",").value == 1234
    assert parse_amount("1.234", decimal_mark=".").value == Decimal("1.234")
    assert parse_amount("1,234").value == 1234


@pytest.mark.parametrize("caption, factor, currency, exempt", [
    ("(in millions, except per share data)", "1000000", None, True),
    ("(dollars in millions)", "1000000", "USD", False),
    ("(₹ in crore)", "10000000", "INR", False),
    ("(Rs. in lakhs)", "100000", "INR", False),
    ("(All amounts in Philippine Peso)", "1", "PHP", False),
    ("(Amounts in thousands of pesos)", "1000", "PHP", False),
])
def test_the_scale_a_caption_states(caption, factor, currency, exempt):
    s = find_scale(caption)
    assert s.factor == Decimal(factor) and s.currency == currency and s.per_share_exempt == exempt


def test_a_currency_without_a_scale_leaves_the_scale_unknown():
    assert find_scale("Philippine Peso").factor is None
    assert find_scale("Consolidated Statements of Income") is None


# --- periods from headers -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("header, kind, end, months", [
    ("Three Months Ended September 30, 2026", "duration", "2026-09-30", 3),
    ("June 30, 2025 (Unaudited)", "instant", "2025-06-30", None),
    ("Quarter ended 30.09.2025", "duration", "2025-09-30", 3),        # day first with dots
    ("Half year ended 30.09.2025", "duration", "2025-09-30", 6),
    ("Six-month periods ended June 30 2025 (Unaudited)", "duration", "2025-06-30", 6),
    ("Sept'25 (Unaudited)", "instant", "2025-09", None),             # month stated, day not: month precision
    ("YoY (%)", "change", None, None),
    ("% Change", "change", None, None),
])
def test_periods_read_from_headers(header, kind, end, months):
    c = read_header(header)
    assert (c.kind, c.end, c.months) == (kind, end, months)


def test_an_ambiguous_numeric_date_is_left_unread():
    assert read_header("04/05/2026").end is None                       # April 5 or 4 May: not guessed
    assert read_header("31/03/2026").end == "2026-03-31"               # 31 cannot be a month


def test_a_header_with_only_the_period_length_is_dated_from_the_caption():
    c = read_header("Three Months 2026", ended="September 30")
    assert (c.kind, c.end, c.months) == ("duration", "2026-09-30", 3)
    c = read_header("2025 (Unaudited)", ended="June 30", caption_months=6)
    assert (c.end, c.months) == ("2025-06-30", 6)


def test_a_fiscal_label_is_dated_only_by_a_statutory_financial_year():
    c = read_header("Q2 FY26")
    assert c.end is None and c.months == 3                             # no market: named, not dated
    c = read_header("Q2 FY26", fiscal_year_end="03-31")                # India: FY26 is April 2025 to March 2026
    assert (c.start, c.end) == ("2025-07-01", "2025-09-30") and "statutory" in c.derived


# --- inline XBRL ---------------------------------------------------------------------------------------------------------

def _ixbrl(rows: str, caption: str = "Acme Corp. Consolidated Statements of Income (Unaudited) (in millions)") -> bytes:
    return f"""<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"><body>
    <div style="display:none"><ix:header><ix:resources>
      <xbrli:context id="c-q3"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2026-07-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:context id="c-q3py"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-07-01</xbrli:startDate><xbrli:endDate>2025-09-30</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:context id="c-seg"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000000001</xbrli:identifier>
        <xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">acme:RatingsMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
        <xbrli:period><xbrli:startDate>2026-07-01</xbrli:startDate><xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
    </ix:resources></ix:header></div>
    <p>{caption}</p>
    <table>
      <tr><td></td><td colspan="3">Three Months Ended September 30,</td></tr>
      <tr><td></td><td colspan="2">2026</td><td colspan="2">2025</td></tr>
      {rows}
    </table></body></html>""".encode()


def _fact(name, ctx, text, sign="", scale="6"):
    s = f' sign="{sign}"' if sign else ""
    return (f'<ix:nonFraction unitRef="usd" contextRef="{ctx}" decimals="-6" name="{name}" format="ixt:num-dot-decimal"'
            f' scale="{scale}"{s} id="f-{name[-6:]}{ctx}">{text}</ix:nonFraction>')


INCOME_ROWS = "".join(f"<tr><td>{label}</td><td>$</td><td>{_fact(tag, 'c-q3', a)}</td><td>{_fact(tag, 'c-q3py', b)}</td></tr>"
                      for label, tag, a, b in [
                          ("Revenue", "us-gaap:Revenues", "1,200", "1,000"),
                          ("Income before taxes", "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", "300", "250"),
                          ("Provision for taxes", "us-gaap:IncomeTaxExpenseBenefit", "60", "50"),
                          ("Net income", "us-gaap:ProfitLoss", "240", "200")])


def test_inline_xbrl_gives_exact_concepts_periods_and_units():
    [t] = statements.extract(_ixbrl(INCOME_ROWS), "text/html")
    assert t.raw.adapter == "ixbrl" and t.statement == "income" and t.primary and t.basis == "consolidated"
    assert [(c.start, c.end, c.months, c.origin) for c in t.columns] == [
        ("2026-07-01", "2026-09-30", 3, "xbrl"), ("2025-07-01", "2025-09-30", 3, "xbrl")]
    rev = t.by_concept("revenue")[0]
    assert rev.concept_origin == "xbrl_tag"
    v = t.value_at(rev.index, t.columns[0].index)
    assert v.full == Decimal("1200000000") and v.unit == "USD" and v.fact["name"] == "us-gaap:Revenues"
    assert [c.verdict for c in t.checks] == ["pass", "pass"]           # profit before tax - tax = net income, both years


def test_the_page_decides_the_sign_shown_and_the_fact_keeps_its_own():
    rows = (f"<tr><td>Capital expenditures</td><td>({_fact('us-gaap:PaymentsToAcquireProductiveAssets', 'c-q3', '93')})</td>"
            f"<td>({_fact('us-gaap:PaymentsToAcquireProductiveAssets', 'c-q3py', '133')})</td></tr>"
            f"<tr><td>Deferred income taxes</td><td>({_fact('us-gaap:DeferredIncomeTaxExpenseBenefit', 'c-q3', '114', sign='-')})</td>"
            f"<td>{_fact('us-gaap:DeferredIncomeTaxExpenseBenefit', 'c-q3py', '12')}</td></tr>")
    [t] = statements.extract(_ixbrl(rows, "Acme Corp. Consolidated Statements of Cash Flows (in millions)"), "text/html")
    capex = t.value_at(t.rows[0].index, t.columns[0].index)
    assert capex.full == Decimal("-93000000") and capex.fact["value"] == "93000000"     # printed "(93)", tagged +93
    deferred = t.value_at(t.rows[1].index, t.columns[0].index)
    assert deferred.full == Decimal("-114000000") and deferred.fact["value"] == "-114000000"


def test_a_segment_fact_does_not_name_the_company_line():
    rows = f"<tr><td>Revenue</td><td>{_fact('us-gaap:Revenues', 'c-seg', '500')}</td><td>{_fact('us-gaap:Revenues', 'c-seg', '400')}</td></tr>"
    [t] = statements.extract(_ixbrl(rows, "Revenue by segment (in millions)"), "text/html")
    assert t.rows[0].concept == "" and not t.primary                   # the Ratings segment's revenue is not Acme's revenue


def test_in_a_tagged_table_the_label_does_not_override_the_tag():
    tag = "us-gaap:CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations"
    rows = (f"<tr><td>Discontinued operations:</td></tr>"
            f"<tr><td>Cash provided by operating activities</td><td>{_fact(tag, 'c-q3', '371')}</td><td>{_fact(tag, 'c-q3py', '586')}</td></tr>"
            f"<tr><td>Cash used for investing activities</td><td>({_fact('us-gaap:CashProvidedByUsedInInvestingActivitiesDiscontinuedOperations', 'c-q3', '15')})</td>"
            f"<td>({_fact('us-gaap:CashProvidedByUsedInInvestingActivitiesDiscontinuedOperations', 'c-q3py', '19')})</td></tr>")
    [t] = statements.extract(_ixbrl(rows, "Acme Corp. Consolidated Statements of Cash Flows (in millions)"), "text/html")
    assert [r.concept for r in t.rows] == ["", ""]                    # not the company's operating or investing cash flow


# --- HTML earnings release -----------------------------------------------------------------------------------------------

RELEASE = b"""<html><body>
<p>Exhibit 1</p><p>Acme Corp. Condensed Consolidated Statements of Income</p>
<p>Three and nine months ended September 30, 2026 and 2025</p><p>(dollars in millions, except per share data)</p>
<table>
<tr><td colspan="3">(unaudited)</td><td colspan="6">Three Months</td><td colspan="3">%</td><td colspan="6">Nine Months</td><td colspan="3">%</td></tr>
<tr><td colspan="3"></td><td colspan="3">2026</td><td colspan="3">2025</td><td colspan="3">Change</td><td colspan="3">2026</td><td colspan="3">2025</td><td colspan="3">Change</td></tr>
<tr><td colspan="3">Revenue</td><td>$</td><td>1,200</td><td></td><td>$</td><td>1,000</td><td></td><td>20</td><td>%</td><td></td><td>$</td><td>3,400</td><td></td><td>$</td><td>3,000</td><td></td><td>13</td><td>%</td><td></td></tr>
<tr><td colspan="3">Gain on dispositions</td><td></td><td>(20</td><td>)</td><td colspan="2">\xe2\x80\x94</td><td></td><td colspan="3">N/M</td><td></td><td>(20</td><td>)</td><td></td><td>(5</td><td>)</td><td colspan="3">N/M</td></tr>
<tr><td colspan="3">Income before taxes on income</td><td></td><td>300</td><td></td><td></td><td>250</td><td></td><td>20</td><td>%</td><td></td><td></td><td>800</td><td></td><td></td><td>700</td><td></td><td>14</td><td>%</td><td></td></tr>
<tr><td colspan="3">Provision for taxes on income</td><td></td><td>60</td><td></td><td></td><td>50</td><td></td><td>20</td><td>%</td><td></td><td></td><td>160</td><td></td><td></td><td>140</td><td></td><td>14</td><td>%</td><td></td></tr>
<tr><td colspan="3">Net income</td><td>$</td><td>240</td><td></td><td>$</td><td>200</td><td></td><td>20</td><td>%</td><td></td><td>$</td><td>640</td><td></td><td>$</td><td>560</td><td></td><td>14</td><td>%</td><td></td></tr>
</table></body></html>"""


def test_an_earnings_release_table_is_read_from_its_headers_and_caption():
    [t] = statements.extract(RELEASE, "text/html")
    assert t.raw.adapter == "html" and t.statement == "income" and t.primary
    assert t.scale.factor == Decimal(1_000_000) and t.scale.currency == "USD"
    periods = [(c.kind, c.end, c.months) for c in t.columns]
    assert periods == [("duration", "2026-09-30", 3), ("duration", "2025-09-30", 3), ("change", None, None),
                       ("duration", "2026-09-30", 9), ("duration", "2025-09-30", 9), ("change", None, None)]
    rev = t.by_concept("revenue")[0]
    assert [str(t.value_at(rev.index, c.index).full) for c in t.columns[:2]] == ["1200000000", "1000000000"]
    gain = next(r for r in t.rows if r.label == "Gain on dispositions")
    assert t.value_at(gain.index, t.columns[0].index).full == Decimal("-20000000")      # "(20" + ")" in the next cell
    assert t.value_at(gain.index, t.columns[1].index).full == 0                          # a dash spanning two cells is one nil
    assert {c.verdict for c in t.checks} == {"pass"} and len(t.checks) == 4


def test_a_misread_figure_fails_the_arithmetic():
    bad = RELEASE.replace(b"<td>240</td>", b"<td>204</td>")
    [t] = statements.extract(bad, "text/html")
    failed = [c for c in t.checks if c.verdict == "fail"]
    assert len(failed) == 1 and failed[0].rule == "profit_after_tax" and failed[0].expected == "204"


# --- PDF ----------------------------------------------------------------------------------------------------------------

def _pdf(items: list[tuple[float, float, str, str]], size: float = 9) -> bytes:
    """A one-page PDF with text placed where statements print it: (x, y, text, align) with align 'l' (x is the left edge)
    or 'r' (x is the right edge, as numbers are printed)."""
    from pdfminer.fontmetrics import FONT_METRICS
    widths = FONT_METRICS["Helvetica"][1]
    ops = []
    for x, y, text, align in items:
        w = sum(widths.get(ch, 556) for ch in text) * size / 1000
        left = x - w if align == "r" else x
        esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"BT /F1 {size} Tf 1 0 0 1 {left:.2f} {y:.2f} Tm ({esc}) Tj ET")
    stream = "\n".join(ops).encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)


def _balance_sheet(total_assets: str = "1,500,000") -> bytes:
    lines = [(200, 760, "Mabini Holdings Corporation and Subsidiaries", "l"),
             (200, 748, "Interim Consolidated Statements of Financial Position", "l"),
             (150, 736, "As at June 30, 2025 and December 31, 2024", "l"),
             (220, 724, "(All amounts in Philippine Peso)", "l"),
             (330, 700, "Notes", "r"), (450, 700, "June 30, 2025", "r"), (560, 700, "December 31, 2024", "r"),
             (450, 690, "(Unaudited)", "r"), (560, 690, "(Audited)", "r")]
    rows = [("Current assets", None, None, None),
            ("Cash and cash equivalents", "4", "400,000", "350,000"),
            ("Trade and other receivables - net", "5", "600,000", "550,000"),
            ("Total current assets", None, "1,000,000", "900,000"),
            ("Property, plant and equipment - net", "10", "500,000", "480,000"),
            ("Total non-current assets", None, "500,000", "480,000"),
            ("Total assets", None, total_assets, "1,380,000"),
            ("Total liabilities", None, "800,000", "700,000"),
            ("Equity", None, None, None),
            ("Share capital", "20.1", "300,000", "300,000"),
            ("Retained earnings", None, "400,000", "380,000"),
            (None, None, "700,000", "680,000"),                        # a total printed without a label
            ("Total liabilities and equity", None, "1,500,000", "1,380,000")]
    y = 672
    for label, note, a, b in rows:
        if label:
            lines.append((60, y, label, "l"))
        if note:
            lines.append((330, y, note, "r"))
        if a:
            lines += [(450, y, a, "r"), (560, y, b, "r")]
        y -= 12
    return _pdf(lines)


def test_a_pdf_balance_sheet_is_read_by_where_its_words_sit():
    [t] = [t for t in statements.extract(_balance_sheet(), "application/pdf") if t.primary]
    assert t.raw.adapter == "pdf" and t.statement == "balance" and t.basis == "consolidated"
    assert t.scale.factor == 1 and t.scale.currency == "PHP"
    assert [(c.kind, c.end) for c in t.columns] == [("instant", "2025-06-30"), ("instant", "2024-12-31")]   # the notes column is not a period
    cash = t.by_concept("cash")[0]
    assert t.value_at(cash.index, t.columns[0].index).full == Decimal(400000)
    receivables = next(r for r in t.rows if r.label.startswith("Trade"))
    assert t.value_at(receivables.index, t.columns[0].index).full == Decimal(600000)     # "- net" is not a nil value
    named = next(r for r in t.rows if r.derived_label)
    assert named.label == "Total Equity" and "sum of the 2 lines" in named.derived_label
    assert {c.rule for c in t.checks} >= {"assets_split", "balance_sheet_balances"} and {c.verdict for c in t.checks} == {"pass"}


def test_a_pdf_figure_that_does_not_add_up_fails():
    [t] = [t for t in statements.extract(_balance_sheet(total_assets="1,600,000"), "application/pdf") if t.primary]
    assert {c.rule for c in t.checks if c.verdict == "fail" and c.column == t.columns[0].index} == {"assets_split", "balance_sheet_balances"}


def test_a_scanned_pdf_gives_no_tables_rather_than_guesses():
    assert statements.extract(_pdf([]), "application/pdf") == []


# --- duplicates and wrapped labels -----------------------------------------------------------------------------------------

def test_the_same_label_under_an_attributable_section_is_not_the_company_line():
    html = b"""<p>Acme Consolidated Statements of Income (in millions)</p><table>
    <tr><td></td><td>Three months ended September 30, 2026</td><td>Three months ended September 30, 2025</td></tr>
    <tr><td>Income before taxes on income</td><td>300</td><td>250</td></tr>
    <tr><td>Provision for taxes on income</td><td>60</td><td>50</td></tr>
    <tr><td>Net income</td><td>240</td><td>200</td></tr>
    <tr><td>Amounts attributable to Acme shareholders:</td></tr>
    <tr><td>Net income</td><td>230</td><td>195</td></tr></table>"""
    [t] = statements.extract(html, "text/html")
    net = t.by_concept("net_income")
    assert len(net) == 1 and t.value_at(net[0].index, t.columns[0].index).full == Decimal(240_000_000)


def test_a_label_wrapped_over_two_lines_is_one_label():
    html = b"""<p>Acme Consolidated Statements of Cash Flows (in millions)</p><table>
    <tr><td></td><td>Year ended December 31, 2025</td><td>Year ended December 31, 2024</td></tr>
    <tr><td>Net increase (decrease) in cash and cash equivalents</td><td>(10)</td><td>20</td></tr>
    <tr><td>Effect of changes in foreign exchange rate on cash and cash</td></tr>
    <tr><td>equivalents</td><td>2</td><td>1</td></tr>
    <tr><td>Cash and cash equivalents at beginning of year</td><td>100</td><td>79</td></tr>
    <tr><td>Cash and cash equivalents at end of year</td><td>92</td><td>100</td></tr></table>"""
    [t] = statements.extract(html, "text/html")
    fx = t.by_concept("fx_effect_cash")
    assert len(fx) == 1 and fx[0].label.endswith("cash and cash equivalents")
    roll = [c for c in t.checks if c.rule == "cash_roll_forward"]
    assert [c.verdict for c in roll] == ["pass", "pass"] and roll[0].detail["parts"]["fx_effect_cash"] == "added"


# --- the evidence store ------------------------------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "files", raising=False)
    return EvidenceStore(tmp_path / "evidence.db")


def _ingest(store, body: bytes, url: str, content_type: str) -> str:
    sha, path = archive_bytes(body, ".html" if "html" in content_type else ".pdf")
    return store.ingest({"url": url, "final_url": url, "sha256": sha, "archive_path": path, "content_type": content_type,
                         "encoding": "utf-8", "method": "http", "fetched_at": utc_now()})


def test_statements_are_stored_with_every_value_and_check_and_never_twice(store):
    sha = _ingest(store, RELEASE, "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm", "text/html")
    summary = statements.read_source(store, sha)
    assert summary["ok"] and summary["market"] == "US" and summary["statements"][0]["checks_failed"] == 0
    again = statements.read_source(store, sha)
    assert again["table_ids"] == summary["table_ids"]                  # the same extractor never stores a table twice
    [t] = store.statements_for(sha, primary_only=True)
    assert t["statement"] == "income" and t["currency"] == "USD" and t["scale"] == "1000000"
    rev = [v for v in t["values"] if v["concept"] == "revenue"]
    assert [(v["value"], v["period_end"], v["period_months"]) for v in rev][:2] == [
        ("1200000000", "2026-09-30", 3), ("1000000000", "2025-09-30", 3)]
    assert store.find_values("revenue", "2026-09-30")[0]["value"] == "1200000000"
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE statement_values SET value = '1' WHERE concept = 'revenue'")


def test_a_document_without_statements_is_not_read(store):
    from types import SimpleNamespace
    assert not statements.titles_a_statement("Acme launches a new product. Revenue grew in the quarter.")
    assert statements.titles_a_statement("ACME CORP\nConsolidated Statements of Financial Position\nAs at June 30, 2025")
