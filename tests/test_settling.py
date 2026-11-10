"""When one source is enough to answer (settling.py, definitions/guidance/answer_settling.json): the company's own filing or
release, the other party's in a deal, a regulatory filing, or a Tier 1 publication settles a fact on its own; any other
source needs a second independent site."""

import pytest

import settling
from source_class import classify
from tests.test_speed import add_claims, runner_with_log, state
from tests.test_runner import env  # noqa: F401


def strength(url, text="", question="What did Acme announce?", names=("Acme",), title=""):
    profile = classify(url, [], text=text, names=list(names), title=title)
    return settling.strength(profile, url, question, list(names))


@pytest.mark.parametrize("url, kind", [
    ("https://www.sec.gov/Archives/edgar/data/1234/000000123426000009/acme-8k.htm", "regulatory_filing"),
    ("https://www.sec.gov/Archives/edgar/data/9999/000099992626000001/buyer-8k.htm", "regulatory_filing"),   # filed by someone else
    ("https://www.sebi.gov.in/filings/2026/acme-order.html", "regulatory_filing"),
    ("https://www.sec.gov.ph/notices/acme-2026.pdf", "regulatory_filing"),
    ("https://www.reuters.com/business/acme-buys-fabrikam-2026-10-01/", "tier1_news"),
    ("https://www.bloomberg.com/news/articles/2026-10-01/acme", "tier1_news"),
    ("https://www.wsj.com/business/acme-deal-123", "tier1_news"),
    ("https://www.ft.com/content/abc", "tier1_news"),
])
def test_regulatory_filings_and_tier1_publications_settle_alone(url, kind):
    got = strength(url)["id"]
    # the subject's own filing is also the company's own document: either way it settles alone
    assert got == kind or (kind == "regulatory_filing" and got == "company_document" and "/1234/" in url)


@pytest.mark.parametrize("url", [
    "https://www.cnbc.com/2026/10/01/acme.html", "https://fortune.com/2026/10/01/acme/", "https://www.forbes.com/sites/x/acme/",
    "https://seekingalpha.com/news/acme", "https://acme-fans.blogspot.com/2026/10/acme.html", "https://en.wikipedia.org/wiki/Acme",
])
def test_tier2_publications_and_other_sites_need_a_second_source(url):
    assert strength(url) is None


def test_the_companys_own_release_settles_alone():
    s = strength("https://www.prnewswire.com/news-releases/acme-reports-results-302000000.html",
                 text="NEW YORK, Oct. 1, 2026 /PRNewswire/ -- Acme Corp (NYSE: ACME) today reported results.", names=("Acme Corp", "Acme"))
    assert s["id"] == "company_document"


def test_the_other_partys_release_settles_alone_for_a_deal_question():
    text = "SEATTLE, Oct. 1, 2026 /PRNewswire/ -- Fabrikam Inc. today announced that it has agreed to be acquired by Acme."
    deal = strength("https://www.prnewswire.com/news-releases/fabrikam-to-be-acquired-302000001.html", text=text,
                    question="What did Acme pay to acquire Fabrikam?")
    assert deal and deal["id"] == "deal_party_document" and "Fabrikam" in deal["why"]
    not_a_deal = strength("https://www.prnewswire.com/news-releases/fabrikam-to-be-acquired-302000001.html", text=text,
                          question="What does Acme charge for its Starter plan?")
    assert not_a_deal is None                                    # the same release is not strong for a question about something else


def test_a_tier1_report_settles_a_fact_with_one_check(env):
    log = []
    runner = runner_with_log(env, log)
    items = add_claims(env, ["https://www.cnbc.com/2026/p", "https://www.reuters.com/business/p"])
    runner.CHECK_WAVE = 1
    runner.n_check(state(items, settle_sites=2))
    assert log == ["support", "relevance"]                        # Reuters was checked first and settled the fact
    assert sum(e["type"] == "check_skipped" for e in env["runs"].events("r1")) == 1


def test_tier2_reports_need_two_independent_sites(env):
    log = []
    runner = runner_with_log(env, log)
    items = add_claims(env, ["https://www.cnbc.com/2026/p", "https://fortune.com/2026/p", "https://www.forbes.com/2026/p"])
    runner.CHECK_WAVE = 1
    runner.n_check(state(items, settle_sites=2))
    assert log.count("relevance") == 2                            # two Tier 2 sites settle it; the third is not checked
    assert sum(e["type"] == "check_skipped" for e in env["runs"].events("r1")) == 1


def test_the_rule_is_data_and_complete():
    r = settling.rules()
    assert {x["id"] for x in r["strong"]} == {"company_document", "deal_party_document", "regulatory_filing", "tier1_news"}
    assert settling.others_need_sites() == 2 and "sec.gov" in r["_by_id"]["regulatory_filing"]["hosts"]
