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
