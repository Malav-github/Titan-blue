"""Labels for sources (primary: the company's own site, filings and releases; secondary: publications reporting on them), no reference
works for a company's news, and material context (terms, parties, reasons) in the answer."""

from datetime import date

import pytest

import source_class
from runner import Plan, Relevance, Selection, Summary
from tests.scripted import Extraction, ExtractedClaim
from skills import writing
from tests.test_runner import GOOD_QUOTE, env, make_runner, types  # noqa: F401

NAMES = ["S&P Global", "S&P Global Inc."]


@pytest.mark.parametrize("url,text,cls,tier", [
    ("https://www.spglobal.com/investors/news", "", "company_site", "primary"),
    ("https://press.spglobal.com/2026-10-27-x", "", "company_site", "primary"),
    ("https://www.sec.gov/Archives/edgar/data/64040/000006404026000050/spgi-20260930.htm", "S&P Global Inc. 10-Q", "filing", "primary"),
    ("https://www.sec.gov/Archives/edgar/data/1/x.htm", "Some Other Corp 10-K", "filing", "secondary"),
    ("https://www.prnewswire.com/news-releases/sp-global-announces-agreement-1.html", "S&P Global Announces Agreement", "wire_release", "primary"),
    ("https://www.businesswire.com/news/home/2026/other-company-announces", "Other Company announces", "wire_release", "secondary"),
    ("https://www.reuters.com/markets/deals/sp-global-x", "", "major_publication", "secondary"),
    ("https://www.wsj.com/finance/x", "", "major_publication", "secondary"),
    ("https://en.wikipedia.org/wiki/S%26P_Global", "", "reference", "secondary"),
    ("https://www.marketscreener.com/quote/x", "", "other", "secondary"),
])
def test_what_kind_of_place_a_page_is_on(url, text, cls, tier):
    got = source_class.classify(url, ["spglobal.com"], text=text, names=NAMES)
    assert (got["id"], got["tier"]) == (cls, tier)


def test_a_wire_page_whose_issuer_is_not_confirmed_says_so():
    got = source_class.classify("https://www.businesswire.com/x", [], text="Another Co announces", names=NAMES)
    assert got["tier"] == "secondary" and "issuer not confirmed" in got["label"]


def test_reference_works_are_for_general_knowledge_not_for_developments():
    assert source_class.is_development("S&P Global's latest strategic move.")
    assert source_class.is_development("Did Acme acquire Beta?") and source_class.is_development("Who is Acme's CEO?")
    assert not source_class.is_development("In what year was Dropbox founded?")
    assert source_class.is_development("Tell me about Acme", {"intent": "latest"})
    assert not source_class.allowed("reference", True) and source_class.allowed("reference", False) and source_class.allowed("major_publication", True)


# --- in the runner ---

class Web:
    """A search result list with a Wikipedia page and a company release; every page fetched reads the same text."""
    def __init__(self, world):
        self.world, self.fetched = world, world.fetched

    def search(self, query, max_results=8, **kw):
        self.world.queries.append(query)
        return {"search_id": "s1", "providers": [{"provider": "fake", "ok": True}], "warnings": [], "results": [
            {"url": "https://en.wikipedia.org/wiki/Acme", "title": "Acme - Wikipedia", "snippet": "Acme is a company", "score": 0.95},
            {"url": "https://www.prnewswire.com/news-releases/acme-pricing-1.html", "title": "Acme announces pricing", "snippet": "Acme", "score": 0.5}]}


def run(env, question, **handlers):
    runner = make_runner(env, **handlers)
    runner.search_fn = Web(env["world"]).search
    return runner.start(question)


def test_a_reference_work_is_never_read_for_a_companys_news(env):
    result = run(env, "What is Acme's latest announcement?")
    assert not any("wikipedia" in u for u in env["world"].fetched) and env["world"].fetched
    skipped = next(e for e in env["runs"].events(result["run_id"]) if e["type"] == "reference_skipped")
    assert skipped["payload"]["urls"] == ["https://en.wikipedia.org/wiki/Acme"]


def test_a_reference_work_is_fine_for_general_knowledge(env):
    result = run(env, "In what year was Acme founded?", Selection=lambda p: Selection(numbers=[2]))      # candidate 2 is the Wikipedia page
    assert any("wikipedia" in u for u in env["world"].fetched) and "reference_skipped" not in types(env, result["run_id"])


def test_a_company_release_on_a_wire_is_primary_and_labelled(env):
    row = run(env, "What is Acme's latest announcement?", Plan=lambda p: Plan(subject="Acme", queries=["acme"], checklist=["Starter plan price"]))["report"]["established"][0]
    assert row["source_class"] == "wire_release" and row["source_tier"] == "primary"
    assert row["source_label"] == "Press release by Acme, via PR Newswire"
    assert row["corroboration"] == "Tier 1: official document, the company's own filing or release"     # settles the fact on its own
    assert row["source"]["issuer"] == "Acme" and row["source"]["venue"] == "wire_service" and row["source"]["publisher"] == "PR Newswire"


# --- context ---

def two_claims():
    return Extraction(claims=[
        ExtractedClaim(predicate="pricing.starter", value="$29", claim="Acme's Starter plan costs $29 per month.", quote=GOOD_QUOTE, needed_fact=1),
        ExtractedClaim(predicate="pricing.team", value="$99", claim="Acme's Team plan costs $99 per month with priority support.",
                       quote="The Team plan costs $99 per month with priority support.", needed_fact=1)])


def judge(p):
    """The Starter claim answers the question; the Team claim is context for it; anything else is off topic."""
    if "Starter plan costs $29" in p:
        return Relevance(reason="states the price", addresses=True)
    return Relevance(reason="gives the other plan's price, the circumstances of the pricing", addresses=False, context=True)


def test_a_claim_that_gives_material_context_is_kept_for_the_answer_not_dropped_as_off_topic(env):
    seen = []
    result = run(env, "What does Acme charge?", Extraction=lambda p: two_claims(), Relevance=judge,
                 Summary=lambda p: (seen.append(p), Summary(text="Acme's Starter plan costs $29 per month [claim:1]. The Team plan costs $99 per month with priority support [claim:2]."))[1])
    report = result["report"]
    assert [r["claim_id"] for r in report["established"]] == [1] and [r["claim_id"] for r in report["context"]] == [2]
    assert report["off_topic"] == 0
    assert "CONTEXT CLAIMS" in seen[0] and "[claim:2]" in seen[0]                 # the writer is given the context claim
    assert "[claim:2]" in report["summary"] and "- [claim:2]" in report["summary"]   # cited, and listed with its source


def test_a_claim_that_is_neither_the_fact_nor_context_is_off_topic(env):
    report = run(env, "What does Acme charge?", Extraction=lambda p: two_claims(),
                 Relevance=lambda p: Relevance(reason="x", addresses="Starter plan costs $29" in p, context=False))["report"]
    assert report["context"] == [] and report["off_topic"] == 1


# --- the matrix on sources and context ---

def row(cid, text, url, **kw):
    return {"claim_id": cid, "claim": text, "value": "", "quote": text, "times": [], "source_url": url, "source_date": "2026-10-27",
            "source_date_kind": "published", "source_tier": kw.pop("tier", "primary"), "corroboration": "primary source", "fact": 0, **kw}


def rate(text, rows, context=(), **kw):
    facts = writing.build_facts(rows) + writing.build_facts(list(context), role="context")
    return writing.score(text, facts, question="S&P Global's latest strategic move.", subject="S&P Global", scope={"intent": "latest"},
                         today=date(2026, 11, 6), gaps=[], form=writing.form_by_id("news"),
                         established_ids={f.claim_id for f in facts}, **kw)


def check(r, name):
    return next(c for d in r["dimensions"].values() for c in d["checks"] if c["id"] == name)


def test_a_reference_work_behind_a_news_answer_is_a_blocker():
    rows = [row(1, "S&P Global agreed to acquire IHS Markit.", "https://en.wikipedia.org/wiki/S%26P_Global", tier="secondary")]
    r = rate("On a date the source does not give, S&P Global agreed to acquire IHS Markit [claim:1].", rows)
    assert check(r, "source_fit")["score"] == 0 and r["score"] <= 60
    assert [b["id"] for b in r["blockers"]] == ["reference_for_news"] and "claim 1 rests on wikipedia.org" in r["blockers"][0]["detail"]


def test_a_point_from_a_publication_names_it_or_says_it_was_reported():
    rows = [row(1, "S&P Global agreed to buy Acme for $2 billion.", "https://www.reuters.com/x", tier="secondary")]
    assert check(rate("On 27 October 2026, S&P Global agreed to buy Acme for $2 billion [claim:1].", rows), "attributed")["score"] == 0
    assert check(rate("On 27 October 2026, Reuters reported that S&P Global agreed to buy Acme for $2 billion [claim:1].", rows), "attributed")["score"] == 1


def test_established_context_should_reach_the_reader():
    main = [row(1, "S&P Global agreed to acquire Acme.", "https://www.prnewswire.com/a")]
    ctx = [row(2, "The all-cash deal values Acme at $2 billion.", "https://www.prnewswire.com/a"),
           row(3, "S&P Global said the deal fits its data strategy.", "https://www.prnewswire.com/a")]
    without = rate("On 27 October 2026, S&P Global said it agreed to acquire Acme [claim:1].", main, ctx)
    with_ = rate("On 27 October 2026, S&P Global said it agreed to acquire Acme [claim:1]. The all-cash deal values Acme at $2 billion [claim:2]. "
                 "S&P Global said the deal fits its data strategy [claim:3].", main, ctx)
    assert check(without, "context_used")["score"] == 0 and check(with_, "context_used")["score"] == 1
    assert check(with_, "answers")["score"] == 1                 # context claims are not off-question
    assert check(with_, "numbers")["score"] == 1                 # the $2 billion is in the context claim it cites


def test_a_possessive_is_not_an_unknown_name():
    rows = [row(1, "S&P Global announced the acquisition of OpenZeppelin.", "https://www.prnewswire.com/a")]
    r = rate("On 17 September 2026, S&P Global's announcement named OpenZeppelin as its target [claim:1].", rows)
    assert check(r, "names")["score"] == 1
