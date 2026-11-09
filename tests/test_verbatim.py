"""Claims are the source's own words, with where they are: extraction by sentence choice (sentences.py), the page of a PDF
(EvidenceStore.page_of), and the exact address of the report, never a site's homepage (sources.not_citable)."""

import io

import pytest

import sentences
from evidence import EvidenceStore
from sources import is_homepage, not_citable
from tests.test_statements import _pdf
from tools.archive import archive_bytes, utc_now


# --- sentences ------------------------------------------------------------------------------------------------------------

def test_a_page_splits_into_sentences_without_touching_a_word():
    text = ("Acme Pricing\nThe Starter plan costs $29 per month. The Team plan costs $99 per month.\n"
            "Acme Inc. said the U.S. market grew. Revenue was Rs. 4,767 crore in Q2.\n"
            "He said “the deal is done.” Then he left.")
    assert sentences.split(text) == [
        "Acme Pricing", "The Starter plan costs $29 per month.", "The Team plan costs $99 per month.",
        "Acme Inc. said the U.S. market grew.", "Revenue was Rs. 4,767 crore in Q2.", "He said “the deal is done.”", "Then he left."]


def test_a_long_flattened_line_is_cut_where_it_can_be_chosen_in_parts():
    row = " ".join(f"| Item {i} | 2020-08-{i:02d} | Plastic depolymerization using fluorinated alumina" for i in range(1, 20))
    meta = "DC.type: patent DC.title: Plastic depolymerization DC.contributor (assignee): Basell Poliolefine Italia SRL " * 6
    for line in (row, meta):
        parts = sentences.split(line)
        assert len(parts) > 1 and all(len(p) <= sentences.MAX_UNIT for p in parts)
        assert " ".join(parts) == " ".join(line.split())           # cut, never changed


@pytest.mark.parametrize("numbers, quote, why", [
    ([2], "The Starter plan costs $29 per month.", ""),
    ([2, 3], "The Starter plan costs $29 per month. The Team plan costs $99 per month.", ""),
    ([9], None, "no sentence numbered 9"),
    ([1, 3], None, "not next to each other"),
    ([1, 2, 3, 4], None, "more than 3"),
    ([], None, "no sentence chosen"),
])
def test_a_choice_of_sentences_is_copied_or_refused(numbers, quote, why):
    units = ["Acme Pricing", "The Starter plan costs $29 per month.", "The Team plan costs $99 per month.", "Contact us."]
    got, reason = sentences.pick(units, numbers)
    assert got == quote and why in reason


def test_windows_never_cut_a_sentence():
    units = [f"Sentence number {i} says something." for i in range(40)]
    wins = sentences.windows(units, 200)
    assert all(w for w in wins) and set(u for w in wins for u in w) == set(units)
    assert all(len(" ".join(w)) <= 200 or len(w) == 1 for w in wins)


# --- the exact address, never a homepage -------------------------------------------------------------------------------

@pytest.mark.parametrize("url, front", [
    ("https://acme.com", True), ("https://acme.com/", True), ("https://www.acme.com/en-us/", True),
    ("https://acme.com/index.html?utm_source=x", True), ("https://acme.com/en", True),
    ("https://acme.com/news/2026/q3-results", False), ("https://acme.com/?p=123", False),
    ("https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm", False), ("upload://report.pdf", False),
])
def test_a_homepage_is_recognised(url, front):
    assert is_homepage(url) is front


def test_a_report_address_that_redirected_to_the_homepage_is_not_citable():
    assert "redirected to the site's homepage" in not_citable("https://acme.com/news/q3", "https://acme.com/")
    assert "is a site's homepage" in not_citable("https://acme.com/", "https://acme.com/")
    assert not_citable("https://acme.com/news/q3", "https://acme.com/news/q3-2026") is None


def test_no_claim_is_taken_from_a_homepage_and_the_run_says_why(env):
    from tests.test_runner import make_runner, types
    world = env["world"]
    plain = world.fetch
    world.fetch = lambda url, max_chars=2000: {**plain(url), "final_url": "https://acme.example/"}      # every page redirected home
    result = make_runner(env).start("What does Acme charge?", budgets={"max_iterations": 1})
    assert env["store"].counts()["claims"] == 0
    reasons = [e["payload"]["reason"] for e in env["runs"].events(result["run_id"]) if e["type"] == "source_not_citable"]
    assert reasons and "redirected to the site's homepage" in reasons[0]
    assert "source_not_citable" in types(env, result["run_id"])


def test_homepages_in_the_search_results_are_not_fetched(env):
    from tests.test_runner import make_runner
    world = env["world"]
    plain = world.search

    def search(query, max_results=8):
        out = plain(query, max_results)
        out["results"].insert(0, {"url": "https://acme.example/", "title": "Acme", "snippet": "home", "score": 1.0})
        return out

    world.search = search
    result = make_runner(env).start("What does Acme charge?", budgets={"max_iterations": 1})
    assert "https://acme.example/" not in world.fetched
    skipped = [e["payload"] for e in env["runs"].events(result["run_id"]) if e["type"] == "candidates_skipped"]
    assert skipped and skipped[0]["urls"] == ["https://acme.example/"]


# --- the page of a PDF ------------------------------------------------------------------------------------------------

def _pages(texts: list[list[str]], labels: tuple | None = None) -> bytes:
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    for lines in texts:
        writer.add_page(PdfReader(io.BytesIO(_pdf([(72, 700 - 14 * i, t, "l") for i, t in enumerate(lines)]))).pages[0])
    if labels:
        writer.set_page_label(*labels)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _source(store, body: bytes) -> str:
    sha, path = archive_bytes(body, ".pdf")
    return store.ingest({"url": "https://acme.example/reports/q3-2026.pdf", "final_url": "https://acme.example/reports/q3-2026.pdf",
                         "content_type": "application/pdf", "encoding": "utf-8", "method": "http", "fetched_at": utc_now(),
                         "sha256": sha, "archive_path": path})


def test_a_claim_from_a_pdf_records_the_page_it_is_on(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    body = _pages([["Acme Corp quarterly report", "Contents and overview of the quarter."],
                   ["Revenue for the third quarter was $3,678 million.", "Operating income was $1,500 million."],
                   ["The Board declared a dividend of $0.96 per share.", "Outlook for the year ahead."]])
    sha = _source(store, body)
    entity = store.add_entity("Acme Corp")
    r = store.add_claim(entity, "dividend", "$0.96", "The Board declared a dividend of $0.96 per share.", sha,
                        "The Board declared a dividend of $0.96 per share.")
    assert r["ok"] and r["page"]["page"] == 3 and r["page"]["last_page"] == 3 and r["page"]["pages"] == 3
    r = store.add_claim(entity, "revenue", "", "x", sha, "Revenue for the third quarter was $3,678 million.")
    assert r["page"]["page"] == 2 and r["page"]["printed"] is None             # no page labels: the PDF's own count
    across = store.add_claim(entity, "x", "", "y", sha, "Operating income was $1,500 million. The Board declared a dividend")
    assert (across["page"]["page"], across["page"]["last_page"]) == (2, 3)
    assert store.claim_detail(across["claim_id"])["page"]["page"] == 2


def test_the_printed_page_number_is_given_when_the_pdf_defines_it(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    body = _pages([["Cover page of the annual report for investors."], ["Contents of the annual report for investors."],
                   ["Revenue for the year was $14,208 million, the report said."]], labels=(2, 2, "/D", None, 17))
    sha = _source(store, body)
    r = store.add_claim(store.add_entity("Acme Corp"), "revenue", "", "x", sha, "Revenue for the year was $14,208 million")
    assert r["page"]["page"] == 3 and r["page"]["printed"] == "17"


def test_a_web_page_has_no_page_number(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    sha, path = archive_bytes(b"<html><body><article><p>The Starter plan costs $29 per month, billed annually.</p></article></body></html>", ".html")
    sha = store.ingest({"url": "https://acme.example/pricing", "final_url": "https://acme.example/pricing", "content_type": "text/html",
                        "encoding": "utf-8", "method": "http", "fetched_at": utc_now(), "sha256": sha, "archive_path": path})
    r = store.add_claim(store.add_entity("Acme"), "p", "", "x", sha, "The Starter plan costs $29 per month, billed annually.")
    assert r["ok"] and r["page"] is None


def test_the_answer_gives_the_page_and_the_exact_address(env):
    from tests.test_runner import make_runner
    from tests.scripted import ExtractedClaim, Extraction
    filler = [f"Further notes on the quarter, line {i}, for readers of this report." for i in range(6)]
    body = _pages([["Acme Corp quarterly report for the third quarter.", *filler],
                   ["The Starter plan costs $29 per month, billed annually.", *filler]], labels=(0, 1, "/D", None, 4))

    def fetch(url, max_chars=2000):
        env["world"].fetched.append(url)
        sha, path = archive_bytes(body, ".pdf")
        return {"url": url, "final_url": url, "status": 200, "content_type": "application/pdf", "encoding": "utf-8",
                "method": "http", "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}

    env["world"].fetch = fetch
    runner = make_runner(env, Extraction=lambda p: Extraction(claims=[ExtractedClaim(
        predicate="pricing.starter", quote="The Starter plan costs $29 per month, billed annually.", value="$29")]))
    runner.fetch_fn = fetch
    report = runner.start("What does Acme charge?", budgets={"max_iterations": 1})["report"]
    row = report["established"][0]
    assert row["claim"] == row["quote"] == "The Starter plan costs $29 per month, billed annually."
    assert row["page"]["page"] == 2 and row["page"]["printed"] == "5"
    assert "page 2 (printed page 5) of 2, https://acme.example/pricing" in report["summary"]


from tests.test_runner import env  # noqa: E402,F401
