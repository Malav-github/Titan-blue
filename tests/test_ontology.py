"""The ontologies: what a source is (issuer, document type, venue, publisher, and so its tier and label) and the kinds of company
event Titan records. Both are read by code; loading validates them."""

import copy

import pytest

import ontology
from ontology.source_profile import describe

APPLE = ["Apple Inc.", "Apple"]
RELEASE = ("Apple reports fourth quarter results\nCUPERTINO, California, October 30, 2026 Apple today announced financial results for its "
           "fiscal 2026 fourth quarter ended September 27, 2026.")
COVER = ("UNITED STATES SECURITIES AND EXCHANGE COMMISSION Washington, D.C. 20549 FORM 10-K (Mark One) ANNUAL REPORT PURSUANT TO SECTION 13 "
         "For the fiscal year ended September 27, 2026 Commission File Number: 001-36743 Apple Inc. (Exact name of Registrant as specified in its charter)")


def look(url, text="", **kw):
    return describe(url, text, names=kw.pop("names", APPLE), official_domains=kw.pop("domains", ["apple.com"]), **kw)


def test_both_ontologies_load_and_validate():
    assert len(ontology.company_events()["predicates"]) >= 20 and "other.fact" in ontology.predicate_ids()
    assert {v["id"] for v in ontology.sources()["venues"]} >= {"investor_relations", "company_newsroom", "edgar", "wire_service"}


@pytest.mark.parametrize("url,text,doc,venue,tier,label", [
    ("https://investor.apple.com/news/2026/apple-reports-fourth-quarter-results", RELEASE,
     "earnings_release", "investor_relations", "primary", "Earnings release by Apple Inc., on its investor relations site"),
    ("https://www.apple.com/newsroom/2026/10/apple-reports-fourth-quarter-results/", RELEASE,
     "earnings_release", "company_newsroom", "primary", "Earnings release by Apple Inc., in its newsroom"),
    ("https://www.apple.com/newsroom/2026/09/apple-introduces-x/", "Apple introduces X", "press_release", "company_newsroom", "primary",
     "Press release by Apple Inc., in its newsroom"),
    ("https://www.sec.gov/Archives/edgar/data/320193/000032019326000105/aapl-20260927.htm", COVER,
     "form_10k", "edgar", "primary", "Form 10-K annual report by Apple Inc., on EDGAR"),
    ("https://www.prnewswire.com/news-releases/apple-announces-x-302.html", "Apple announces X\nCUPERTINO ...\nSOURCE Apple Inc.\n",
     "press_release", "wire_service", "primary", "Press release by Apple Inc., via PR Newswire"),
    ("https://www.businesswire.com/news/home/x", "Other Co announces results", "press_release", "wire_service", "secondary",
     "Press release, via Business Wire (issuer not confirmed)"),
    ("https://www.reuters.com/technology/apple-x", "Apple beat estimates", "news_article", "major_publication", "secondary", "News article by Reuters"),
    ("https://en.wikipedia.org/wiki/Apple_Inc.", "Apple Inc. is", "reference_entry", "reference_work", "secondary", "Reference entry by Wikipedia"),
    ("https://www.marketscreener.com/quote/x", "Apple news", "web_page", "other", "secondary", "Web page by marketscreener.com"),
])
def test_a_source_is_described_by_issuer_type_venue_and_publisher(url, text, doc, venue, tier, label):
    d = look(url, text)
    assert (d["document_type"], d["venue"], d["tier"], d["label"]) == (doc, venue, tier, label)


def test_the_same_release_moved_from_investor_relations_to_edgar_keeps_issuer_and_type_and_changes_venue():
    ir = look("https://investor.apple.com/news/2026/apple-reports-fourth-quarter-results", RELEASE)
    edgar = look("https://www.sec.gov/Archives/edgar/data/320193/000032019326000105/a8-kex991q42026.htm", RELEASE, ciks=["0000320193"])
    assert (ir["issuer"], ir["document_type"]) == (edgar["issuer"], edgar["document_type"]) == ("Apple Inc.", "earnings_release")
    assert (ir["venue"], edgar["venue"]) == ("investor_relations", "edgar") and edgar["tier"] == "primary"
    assert edgar["filed_as"] == "Exhibit 99.1 to Form 8-K, Item 2.02" and edgar["publisher"] == "U.S. Securities and Exchange Commission"
    assert edgar["label"] == "Earnings release by Apple Inc., on EDGAR (filed as Exhibit 99.1 to Form 8-K, Item 2.02)"


def test_an_edgar_filing_is_the_issuers_by_its_cover_or_its_cik_and_not_merely_because_a_name_is_mentioned():
    assert look("https://www.sec.gov/Archives/edgar/data/320193/x/aapl-8k.htm", "FORM 8-K CURRENT REPORT", ciks=["320193"])["issuer_basis"].startswith("filer CIK")
    other = look("https://www.sec.gov/Archives/edgar/data/1/x.htm", COVER.replace("Apple Inc.", "Other Corp"))
    assert other["issuer"] == "Other Corp" and other["tier"] == "secondary" and other["label"].endswith("(issuer not the subject)")
    # a filing that only cites another form in passing is not taken for it ("see our Annual Report on Form 10-K")
    assert look("https://www.sec.gov/Archives/edgar/data/320193/x/doc.htm", "Risk factors are in our Annual Report on Form 10-K.", ciks=["320193"])["document_type"] == "sec_filing"


def test_a_publication_is_the_issuer_of_its_own_story_even_when_it_quotes_the_company():
    d = look("https://www.wsj.com/business/apple-x", "Apple Inc. said in a press release on Thursday")
    assert d["issuer"] == "The Wall Street Journal" and d["tier"] == "secondary" and not d["issuer_document"]


def test_an_insider_form_on_edgar_is_not_the_companys_own_document():
    d = look("https://www.sec.gov/Archives/edgar/data/320193/x/form4.xml", "FORM 4 STATEMENT OF CHANGES IN BENEFICIAL OWNERSHIP", ciks=["320193"])
    assert d["document_type"] == "ownership_filing" and d["tier"] == "secondary"


# --- validation ---

@pytest.fixture
def restore():
    yield
    ontology.company_events.cache_clear()


def _first(d):
    return d["predicates"][0]


@pytest.mark.parametrize("change,message", [
    (lambda d: _first(d)["example"].update(quote="Something else entirely."), "not word for word"),
    (lambda d: _first(d)["fields"][0].update(type="colour"), "unknown type"),
    (lambda d: _first(d)["rules"].append({"kind": "order", "after": "no.such"}), "unknown predicate"),
    (lambda d: d["predicates"].pop(), "other.fact is missing"),
])
def test_a_broken_event_ontology_fails_loudly(monkeypatch, restore, change, message):
    original = ontology._read
    good = original("company_events.json")
    bad = copy.deepcopy(good)
    change(bad)
    monkeypatch.setattr(ontology, "_read", lambda name: bad if name == "company_events.json" else original(name))
    ontology.company_events.cache_clear()
    with pytest.raises(ValueError, match=message):
        ontology.company_events()
