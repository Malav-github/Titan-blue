"""Scanned statements: two OCR engines in parallel, their readings matched figure by figure, and every figure graded
verified, agreed or doubt, so an accurate figure can be said to be accurate and a doubtful one is said loudly.

The cases come from a real scanned Indian results page (VTM Limited, Q2 FY26): Tesseract read "4,071.38" where the page
says 1,071.38 and "6674.72]" for 6,674.72; RapidOCR read "7.316.94" for 7,316.94. Companies and figures here are fictional.
"""

import io
from decimal import Decimal

import pytest

from skills import statements
from skills.statements import checks, ocr
from skills.statements.checks import valid_grouping
from tests.test_statements import _pdf


def W(text, x0, x1, top, conf=0.95, engine="rapidocr"):
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": top + 8, "conf": conf, "engine": engine}


# --- matching the two engines ------------------------------------------------------------------------------------------

def test_numbers_are_matched_by_place_and_must_agree_character_for_character():
    rapid = [W("Total", 60, 80, 100), W("Income", 82, 110, 100), W("9,104.00", 300, 330, 100), W("7,285.37", 360, 390, 100)]
    tess = [W("Tota1", 60, 80, 100, engine="tesseract"), W("9,104.00", 301, 330, 100, 0.96, "tesseract"),
            W("7,285.87", 360, 390, 100, 0.91, "tesseract")]
    words = ocr.combine(tess, rapid)
    labels = [w["text"] for w in words if not w["ocr"]]
    assert labels == ["Total", "Income"]                                  # labels from RapidOCR, Tesseract's "Tota1" dropped
    a, b = [w["ocr"] for w in words if w["ocr"]]
    assert a["agree"] and a["tesseract"] == a["rapidocr"] == "9,104.00"
    assert not b["agree"] and (b["tesseract"], b["rapidocr"]) == ("7,285.87", "7,285.37")


def test_a_garbled_reading_is_a_disagreement_and_a_lone_reading_is_one_engine():
    rapid = [W("6,674.72", 300, 330, 100)]
    tess = [W("6674.72]", 300, 331, 100, 0.0, "tesseract"), W("152.84", 300, 325, 140, 0.9, "tesseract")]
    words = ocr.combine(tess, rapid)
    garbled = next(w["ocr"] for w in words if w["top"] == 100)
    assert not garbled["agree"] and garbled["tesseract"] == "6674.72]"
    lone = next(w["ocr"] for w in words if w["top"] == 140)
    assert lone["rapidocr"] is None and lone["tesseract"] == "152.84"


def test_table_rules_read_as_characters_are_not_part_of_a_number():
    words = ocr.combine([W("(850.27)|", 300, 334, 100, 0.97, "tesseract")], [W("(850.27)", 300, 330, 100)])
    assert words[0]["ocr"]["agree"]


@pytest.mark.parametrize("text, ok", [("1,234,567", True), ("12,34,567", True), ("8,987.59", True), ("(1,348.95)", True),
                                      ("1,2345", False), ("12,345,67", False), ("7.316.94", False), ("7,316,94", False),
                                      ("1.234.567", True), ("123", True)])
def test_digit_grouping(text, ok):
    assert valid_grouping(text) is ok


# --- grading a scanned statement -------------------------------------------------------------------------------------------

def _scanned(monkeypatch, rows, title="Statement of Unaudited Financial Results for the quarter ended September 30, 2025"):
    """A one-page PDF with no text layer, whose OCR words are `rows`: (label, [(tesseract, rapidocr, tess_conf), ...])."""
    words = [W("Mabini Textiles Limited", 200, 300, 40), W(title, 120, 480, 52), W("(in INR Lakhs)", 430, 480, 64),
             W("Quarter ended", 330, 400, 80), W("Sep 30, 2025", 300, 345, 90), W("Sep 30, 2024", 390, 435, 90)]
    y = 110
    for label, cells in rows:
        words.append(W(label, 60, 60 + 5 * len(label), y))
        for (t, r, conf), right in zip(cells, (345, 435)):
            text = r or t
            box = W(text, right - 5.5 * len(text), right, y)
            box["ocr"] = {"tesseract": t, "rapidocr": r, "conf": {"tesseract": conf if t else None, "rapidocr": 0.99 if r else None},
                          "agree": bool(t and r and t == r), "text": text, "page_box": [box["x0"], y, right, y + 8]}
            words.append(box)
        y += 12
    for w in words:
        w.setdefault("ocr", None)
    monkeypatch.setattr(ocr, "available", lambda: None)
    monkeypatch.setattr(ocr, "read_page", lambda body, index: words)
    [t] = [t for t in statements.extract(_pdf([]), "application/pdf", market="IN") if t.primary]
    return t


def _verdicts(t):
    return {(next(r.label for r in t.rows if r.index == v.row), v.col): (v.verdict, v.full, v.reasons, v.notes) for v in t.values}


def test_figures_both_engines_read_the_same_inside_a_sum_that_adds_up_are_verified(monkeypatch):
    t = _scanned(monkeypatch, [
        ("(a) Revenue from operations", [("8,987.59", "8,987.59", 0.96), ("7,316.94", "7,316.94", 0.95)]),
        ("(b) Other Income", [("116.41", "116.41", 0.97), ("207.64", "207.64", 0.95)]),
        ("Total Income (1+2)", [("9,104.00", "9,104.00", 0.96), ("7,524.58", "7,524.58", 0.94)]),
        ("Employee benefits expense", [("661.11", "661.11", 0.95), ("526.06", "526.06", 0.96)])])
    assert t.raw.adapter == "pdf_ocr" and t.scale.currency == "INR" and t.scale.factor == 100000
    assert [(c.kind, c.end, c.months) for c in t.columns] == [("duration", "2025-09-30", 3), ("duration", "2024-09-30", 3)]
    v = _verdicts(t)
    rev = v[("(a) Revenue from operations", t.columns[0].index)]
    assert rev[0] == "verified" and rev[1] == Decimal("898759000")          # lakh: 8,987.59 x 100,000
    assert v[("Employee benefits expense", t.columns[0].index)][0] == "agreed"   # both engines, no sum covers it


def test_one_unconfirmed_figure_in_a_sum_that_adds_up_is_settled_by_the_arithmetic(monkeypatch):
    t = _scanned(monkeypatch, [
        ("(a) Revenue from operations", [("8,987.59", "8,987.59", 0.96), ("7,316.94", "7,316.94", 0.95)]),
        ("(b) Other Income", [("116.41", "116.41", 0.97), ("207.64", "207.64", 0.95)]),
        ("Total Income (1+2)", [("9104.00]", "9,104.00", 0.0), ("7,524.58", "7,524.58", 0.94)])])
    verdict, full, reasons, notes = _verdicts(t)[("Total Income (1+2)", t.columns[0].index)]
    assert verdict == "verified" and full == Decimal("910400000") and not reasons
    assert any("disagree" in n for n in notes) and any("settled by arithmetic" in n for n in notes)


def test_two_unconfirmed_figures_in_one_sum_stay_in_doubt(monkeypatch):
    t = _scanned(monkeypatch, [
        ("(a) Revenue from operations", [("8,987.59", "8,987.59", 0.96), ("7,316.94", "7,316.94", 0.95)]),
        ("(b) Other Income", [(None, "116.41", None), ("207.64", "207.64", 0.95)]),
        ("Total Income (1+2)", [("9104.00]", "9,104.00", 0.0), ("7,524.58", "7,524.58", 0.94)])])
    v = _verdicts(t)
    col = t.columns[0].index
    assert v[("(b) Other Income", col)][0] == "doubt" and "one OCR engine only" in v[("(b) Other Income", col)][2][0]
    total = v[("Total Income (1+2)", col)]
    assert total[0] == "doubt" and total[1] == Decimal("910400000")      # kept as RapidOCR read it, never dropped
    assert any("disagree" in r for r in total[2])
    assert t.value_at(next(r.index for r in t.rows if r.label.startswith("Total")), col).proposed["kind"] == "engine"


def test_a_misread_is_taken_as_the_reading_that_makes_its_sum_add_up_and_flagged(monkeypatch):
    t = _scanned(monkeypatch, [
        ("(a) Revenue from operations", [("7,316.94", "7.316.94", 0.95), ("7,316.94", "7,316.94", 0.95)]),
        ("(b) Other Income", [("207.64", "207.64", 0.95), ("207.64", "207.64", 0.95)]),
        ("Total Income (1+2)", [("7,524.58", "7,524.58", 0.94), ("7,524.58", "7,524.58", 0.94)])])
    v = _verdicts(t)
    col = t.columns[0].index
    verdict, full, reasons, _ = v[("(a) Revenue from operations", col)]
    assert verdict == "doubt" and full == Decimal("731694000")          # 7,316.94 lakh, not 731,694 lakh
    assert any("taken as 7,316.94 (printed '7.316.94'): tesseract's reading; with it total_income adds up" in r for r in reasons)
    assert all(c.verdict == "pass" for c in t.checks)
    assert v[("Total Income (1+2)", col)][0] == "verified" and v[("(b) Other Income", col)][0] == "verified"


def test_a_figure_both_engines_misread_is_repaired_by_its_column_format_and_flagged(monkeypatch):
    t = _scanned(monkeypatch, [
        ("(a) Revenue from operations", [("7.316.94", "7.316.94", 0.95), ("8,987.59", "8,987.59", 0.95)]),
        ("(b) Other Income", [("207.64", "207.64", 0.95), ("116.41", "116.41", 0.95)]),
        ("Total Income (1+2)", [("7,524.58", "7,524.58", 0.94), ("9,104.00", "9,104.00", 0.94)]),
        ("Employee benefits expense", [("526.06", "526.06", 0.96), ("661.11", "661.11", 0.95)])])
    col = t.columns[0].index
    rev = t.value_at(next(r.index for r in t.rows if r.label.startswith("(a)")), col)
    assert rev.verdict == "doubt" and rev.amount.value == Decimal("7316.94") and rev.proposed["kind"] == "repair"
    assert rev.proposed["confirmed_by"] == ["total_income"]
    assert any("the last separator is the decimal point" in r for r in rev.reasons)
    row = statements.doubt_row(t, rev)
    assert row["printed"] == "7.316.94" and row["taken_as"] == "7,316.94" and row["summary"].endswith("please double-check")


def test_a_misread_no_reading_can_settle_stays_doubt_for_every_figure_in_its_sum(monkeypatch):
    t = _scanned(monkeypatch, [
        ("(a) Revenue from operations", [("7,816.94", "7,816.94", 0.95), ("7,316.94", "7,316.94", 0.95)]),
        ("(b) Other Income", [("207.64", "207.64", 0.95), ("207.64", "207.64", 0.95)]),
        ("Total Income (1+2)", [("7,524.58", "7,524.58", 0.94), ("7,524.58", "7,524.58", 0.94)])])
    v = _verdicts(t)
    col = t.columns[0].index
    assert all(v[(label, col)][0] == "doubt" for label in ("(a) Revenue from operations", "(b) Other Income", "Total Income (1+2)"))
    assert any("does not add up" in r for r in v[("Total Income (1+2)", col)][2])
    assert v[("(a) Revenue from operations", col)][1] == Decimal("781694000")     # kept as both engines read it


@pytest.mark.parametrize("text,d,fixed", [("7.316.94", 2, "7,316.94"), ("(1.690.69)", 2, "(1,690.69)"), ("7,316,94", 2, "7,316.94"),
                                          ("1.234", 0, "1,234"), ("12.34.567", 0, "12,34,567"), ("12,34,567.8", 2, None),
                                          ("7316", 2, None), ("7.316.94", None, "7,316.94"), ("7.316", None, None),
                                          ("1.234.567", None, None)])
def test_separator_repair(text, d, fixed):
    assert checks.repair_separators(text, d) == fixed


def test_an_unsure_reading_with_nothing_to_confirm_it_is_doubt(monkeypatch):
    t = _scanned(monkeypatch, [("Employee benefits expense", [("661.11", "661.11", 0.42), ("526.06", "526.06", 0.96)]),
                               ("Finance cost", [("39.80", "39.80", 0.95), ("27.70", "27.70", 0.95)]),
                               ("Other expenses", [("1,989.15", "1,989.15", 0.95), ("1,308.51", "1,308.51", 0.95)])])
    v = _verdicts(t)
    assert v[("Employee benefits expense", t.columns[0].index)][0] == "doubt"
    assert "tesseract unsure (0.42)" in v[("Employee benefits expense", t.columns[0].index)][2]
    assert v[("Employee benefits expense", t.columns[1].index)][0] == "agreed"


def test_figures_in_doubt_are_kept_out_of_the_index_but_stored_with_both_readings(monkeypatch, tmp_path):
    from evidence import EvidenceStore
    from tests.test_statements import _ingest
    t_rows = [("(a) Revenue from operations", [("8,987.59", "8,987.59", 0.96), ("7.316.94", "7.316.94", 0.95)]),
              ("(b) Other Income", [("116.41", "116.41", 0.97), ("207.64", "207.64", 0.95)]),
              ("Total Income (1+2)", [("9,104.00", "9,104.00", 0.96), ("7,524.58", "7,524.58", 0.94)])]
    _scanned(monkeypatch, t_rows)                                        # sets up the fake OCR
    store = EvidenceStore(tmp_path / "evidence.db")
    sha = _ingest(store, _pdf([]), "https://www.mabini.example/results-q2.pdf", "application/pdf")
    summary = statements.read_source(store, sha)
    assert summary["ocr_pages"] == [1] and summary["doubt_count"] == 1             # the repair; its sum then adds up
    assert summary["doubts"][0]["printed"] == "7.316.94" and summary["doubts"][0]["taken_as"] == "7,316.94"
    assert [v["value"] for v in store.find_values("revenue")] == ["898759000"]          # the doubtful 2024 figure is left out
    doubtful = store.find_values("revenue", include_doubt=True)
    stored = next(v for v in doubtful if v["verdict"] == "doubt")
    assert stored["reading"] == "ocr" and '"rapidocr": "7.316.94"' in stored["ocr"]
    assert stored["printed"] == "7.316.94" and stored["value"] == "731694000"          # kept, as the reading it is taken as
    assert '"kind": "repair"' in stored["proposed"]


def test_a_scanned_page_without_ocr_is_reported_never_silently_empty(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: "Tesseract is not installed")
    report = {}
    assert statements.extract(_pdf([]), "application/pdf", report=report) == []
    assert report["unread_pages"] == [{"page": 1, "reason": "scanned page, OCR unavailable: Tesseract is not installed"}]


# --- both real engines on a real image -------------------------------------------------------------------------------------

def _font():
    for path in (r"C:\Windows\Fonts\arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial.ttf"):
        try:
            from PIL import ImageFont
            return ImageFont.truetype(path, 30)
        except OSError:
            continue
    return None


@pytest.mark.skipif(ocr.available() is not None or _font() is None, reason="needs Tesseract, RapidOCR and a TrueType font")
def test_both_engines_read_a_scanned_statement_and_the_sums_verify_it():
    from PIL import Image, ImageDraw
    font, img = _font(), Image.new("L", (2480, 1400), 255)
    d = ImageDraw.Draw(img)
    d.text((500, 80), "Statement of Unaudited Financial Results for the quarter ended September 30, 2025", font=font, fill=0)
    d.text((1900, 150), "(in INR Lakhs)", font=font, fill=0)
    d.text((1300, 220), "Sep 30, 2025", font=font, fill=0)
    d.text((1800, 220), "Sep 30, 2024", font=font, fill=0)
    rows = [("Revenue from operations", "8,987.59", "7,316.94"), ("Other Income", "116.41", "207.64"),
            ("Total Income (1+2)", "9,104.00", "7,524.58")]
    for k, (label, a, b) in enumerate(rows):
        y = 320 + 70 * k
        d.text((150, y), label, font=font, fill=0)
        for text, right in ((a, 1480), (b, 1980)):
            d.text((right - d.textlength(text, font=font), y), text, font=font, fill=0)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=300)                                 # an image-only PDF: no text layer at all
    report = {}
    [t] = [t for t in statements.extract(buf.getvalue(), "application/pdf", market="IN", report=report) if t.primary]
    assert report["ocr_pages"] == [1] and t.raw.adapter == "pdf_ocr"
    assert [c.end for c in t.columns] == ["2025-09-30", "2024-09-30"]
    assert {c.verdict for c in t.checks} == {"pass"} and len(t.checks) == 2
    assert {v.verdict for v in t.values} == {"verified"}
    rev = t.by_concept("revenue")[0]
    assert t.value_at(rev.index, t.columns[0].index).full == Decimal("898759000")
