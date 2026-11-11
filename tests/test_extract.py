import pytest
from pydantic import ValidationError
from tools import primary
from tools.web import extract_text, meta_block


def minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()


def test_pdf_text_is_extracted():
    suffix, text = extract_text(minimal_pdf("Hello Titan evidence"), "application/pdf", "utf-8", "https://x.example/a.pdf")
    assert suffix == ".pdf" and "Hello Titan evidence" in text


def test_corrupt_pdf_gives_empty_text_not_a_crash():
    assert extract_text(b"%PDF-1.4 garbage", "application/pdf", "utf-8", "u")[1] == ""


def test_html_metadata_is_appended_for_citation():
    html = ('<html><head><meta name="DC.contributor" scheme="assignee" content="Acme Corp">'
            '<meta name="viewport" content="x"></head><body><article><p>' + "Body text. " * 30 + "</p></article></body></html>")
    suffix, text = extract_text(html.encode(), "text/html", "utf-8", "https://x.example/")
    assert "[Page metadata]" in text and "DC.contributor (assignee): Acme Corp" in text and "viewport" not in text
    assert meta_block("<html></html>") == ""


def test_entities_sharing_one_domain_are_not_ambiguous(monkeypatch):
    site = lambda u: [{"mainsnak": {"datavalue": {"value": u}}}]  # noqa: E731

    def fake_wikidata(params):
        if params["action"] == "wbsearchentities":
            return {"search": [{"id": "Q1", "label": "Acme", "description": "company"},
                               {"id": "Q2", "label": "Acme", "description": "holding company"}]}
        return {"entities": {"Q1": {"claims": {"P856": site("https://acme.com/")}},
                             "Q2": {"claims": {"P856": site("https://www.acme.com/about")}}}}

    class Resp:
        body, status, encoding = b"<html>Acme</html>", 200, "utf-8"

    monkeypatch.setattr(primary, "_wikidata", fake_wikidata)
    monkeypatch.setattr(primary, "download", lambda url, timeout=20.0, max_bytes=0: Resp())
    statuses = [c["status"] for c in primary.resolve_domain("Acme")["candidates"]]
    assert statuses == ["confirmed", "confirmed"]


# --- one malformed claim must not cost the batch (seen on a real patent page: a missing needed_fact) ---

GOOD = {"predicate": "p", "sentences": [3], "value": "50,000", "needed_fact": 1}


def test_a_malformed_claim_is_dropped_and_the_rest_of_the_batch_survives():
    from runner import Extraction
    Extraction.take_dropped()
    bad_missing = {k: v for k, v in GOOD.items() if k != "needed_fact"}
    bad_type = {**GOOD, "needed_fact": "first"}
    out = Extraction.model_validate({"claims": [GOOD, bad_missing, {**GOOD, "sentences": [4, 5]}, bad_type, "not even a dict"]})
    assert [c.sentences for c in out.claims] == [[3], [4, 5]]
    assert Extraction.take_dropped() == 3
    assert Extraction.take_dropped() == 0                       # reading resets the count
    examples = Extraction.take_dropped_examples()
    assert 1 <= len(examples) <= 3 and "needed_fact" in examples[0]["problem"] and examples[0]["claim"]   # what was dropped is recorded
    assert Extraction.take_dropped_examples() == []


def test_nothing_is_repaired_or_invented_when_dropping():
    from runner import Extraction
    out = Extraction.model_validate({"claims": [{k: v for k, v in GOOD.items() if k != "sentences"}]})
    assert out.claims == []                                      # a claim without its sentences is gone, not filled in
    with pytest.raises(ValidationError):                         # only malformed items are tolerated; a malformed whole is still an error
        Extraction.model_validate({"claims": "garbage"})
    Extraction.take_dropped()


def test_the_prompt_and_schema_are_unchanged_for_the_model():
    from runner import Extraction
    assert set(Extraction.model_json_schema()["properties"]) == {"picks"}        # only numbers are asked of the model


def test_a_table_heavy_page_that_defeats_article_extraction_is_read_line_by_line():
    """EDGAR exhibits (an earnings release filed with an 8-K) are tables; article extraction returns nothing for them."""
    rows = "".join(f"<tr><td>Line item {i}</td><td>${i},000</td></tr>" for i in range(300))
    html = ("<html><body><div style='display:none'><ix:header>0000320193 hidden XBRL data</ix:header></div>"
            "<table><tr><td>Exhibit 99.1</td></tr><tr><td>Apple reports fourth quarter results</td></tr></table>"
            f"<table>{rows}</table><script>var x = 1;</script></body></html>")
    _, text = extract_text(html.encode(), "text/html", "utf-8", "https://www.sec.gov/Archives/edgar/data/320193/x/ex991.htm")
    assert "Apple reports fourth quarter results" in text and "Line item 299" in text
    assert "hidden XBRL" not in text and "var x" not in text
