"""Readable text for OpenAlex responses, and narrowing of over-long quotes."""

import json

from connectors.render import render_json
from evidence import MAX_QUOTE_CHARS, narrow_quote, normalize, verify_quote
from tools.web import extract_text

WORK = {"id": "https://openalex.org/W2963403868", "display_name": "Attention is All you Need", "publication_year": 2017,
        "publication_date": "2017-06-12", "type": "preprint", "cited_by_count": 100000, "doi": "https://doi.org/10.65215/ctdc8e75",
        "primary_location": {"source": {"display_name": "arXiv (Cornell University)"}},
        "authorships": [{"author_position": "first", "author": {"display_name": "Ashish Vaswani"}, "institutions": [{"display_name": "Google (United States)"}]},
                        {"author_position": "middle", "author": {"display_name": "Noam Shazeer"}, "institutions": []}]}
URL = "https://api.openalex.org/works?search=Attention+Is+All+You+Need"


def test_works_list_reads_with_authors_in_order():
    text = render_json(URL, json.dumps({"meta": {}, "results": [WORK, {**WORK, "display_name": "Other", "authorships": []}]}))
    assert "Work 1: Attention is All you Need" in text and "Publication year: 2017" in text
    assert "Authors in order (2): 1. Ashish Vaswani (Google (United States)); 2. Noam Shazeer" in text
    assert "Venue: arXiv (Cornell University)" in text and "Work 2: Other" in text


def test_single_work_and_authors_and_unknown_shapes():
    assert "OpenAlex work." in render_json("https://api.openalex.org/works/W1", json.dumps(WORK))
    people = {"results": [{"display_name": "Geoffrey Hinton", "works_count": 500, "cited_by_count": 9, "last_known_institutions": [{"display_name": "University of Toronto"}]}]}
    assert "Author 1: Geoffrey Hinton" in render_json("https://api.openalex.org/authors?search=hinton", json.dumps(people))
    assert render_json(URL, json.dumps({"results": []})) is None
    assert render_json(URL, "not json") is None
    assert render_json("https://example.com/x", json.dumps(WORK)) is None


def test_the_shared_extractor_uses_it_only_for_json_from_openalex():
    suffix, text = extract_text(json.dumps({"results": [WORK]}).encode(), "application/json", "utf-8", URL)
    assert suffix == ".json" and text.startswith("OpenAlex works")
    raw = json.dumps({"a": 1})
    assert extract_text(raw.encode(), "application/json", "utf-8", "https://other.example/api")[1] == raw


def test_a_quote_from_the_rendered_text_verifies():
    _, text = extract_text(json.dumps({"results": [WORK]}).encode(), "application/json", "utf-8", URL)
    assert verify_quote(text, "Authors in order (2): 1. Ashish Vaswani")["ok"]


BLOCK = "Ashish Vaswani∗ Google Brain avaswani@google.com " + " ".join(f"Author{i} Org{i} a{i}@example.com" for i in range(300))


def test_a_long_quote_is_cut_around_the_claims_own_terms_and_stays_verbatim():
    n = narrow_quote(BLOCK, "Ashish Vaswani is the first author of the paper.", "Ashish Vaswani")
    assert n and n.startswith("Ashish Vaswani") and 20 <= len(n) <= 300 and n in normalize(BLOCK)
    mid = narrow_quote(BLOCK, "Author150 works at Org150.", "Org150")
    assert mid and "Org150" in mid and mid in normalize(BLOCK) and not mid.startswith(" ")


def test_short_quotes_are_left_alone():
    assert narrow_quote("x" * MAX_QUOTE_CHARS, "claim words here", "") is None
    assert narrow_quote("The Starter plan costs $29 per month.", "Starter costs $29", "$29") is None
