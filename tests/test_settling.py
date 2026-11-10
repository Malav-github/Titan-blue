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
    ("https://www.reuters.com/business/acme-buys-fabrikam-2026-10-01/", "top_publisher"),
    ("https://www.bloomberg.com/news/articles/2026-10-01/acme", "top_publisher"),
    ("https://www.wsj.com/business/acme-deal-123", "top_publisher"),
    ("https://www.ft.com/content/abc", "top_publisher"),
])
def test_tier1_documents_and_tier2_publishers_settle_alone(url, kind):
    got = strength(url)["id"]
    # the subject's own filing is also the company's own document: either way it settles alone
    assert got == kind or (kind == "regulatory_filing" and got == "company_document" and "/1234/" in url)


@pytest.mark.parametrize("url", [
    "https://www.benzinga.com/news/26/10/acme", "https://finance.yahoo.com/news/acme-1.html", "https://www.forbes.com/sites/x/acme/",
    "https://seekingalpha.com/news/acme", "https://acme-fans.blogspot.com/2026/10/acme.html", "https://en.wikipedia.org/wiki/Acme",
])
def test_tier3_sources_need_a_second_source(url):
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


def test_a_tier2_report_settles_a_fact_with_one_check(env):
    log = []
    runner = runner_with_log(env, log)
    items = add_claims(env, ["https://www.benzinga.com/2026/p", "https://www.reuters.com/business/p"])
    runner.CHECK_WAVE = 1
    runner.n_check(state(items, settle_sites=2))
    assert log == ["support", "relevance"]                        # Reuters was checked first and settled the fact
    assert sum(e["type"] == "check_skipped" for e in env["runs"].events("r1")) == 1


def test_tier3_reports_need_two_independent_sites(env):
    log = []
    runner = runner_with_log(env, log)
    items = add_claims(env, ["https://www.benzinga.com/2026/p", "https://finance.yahoo.com/2026/p", "https://www.investing.com/2026/p"])
    runner.CHECK_WAVE = 1
    runner.n_check(state(items, settle_sites=2))
    assert log.count("relevance") == 2                            # two Tier 2 sites settle it; the third is not checked
    assert sum(e["type"] == "check_skipped" for e in env["runs"].events("r1")) == 1


def test_the_rule_is_data_and_complete():
    r = settling.rules()
    assert {(x["id"], x["tier"]) for x in r["strong"]} == {("company_document", 1), ("deal_party_document", 1), ("regulatory_filing", 1),
                                                          ("official_blog", 1), ("top_publisher", 2)}
    assert r["settles_alone"] == [1, 2] and settling.tier_label(2).startswith("Tier 2")
    assert settling.others_need_sites() == 2 and "sec.gov" in r["_by_id"]["regulatory_filing"]["hosts"]


def test_tier1_is_read_before_tier2_and_tier2_says_it_is_a_report():
    assert strength("https://www.sec.gov/Archives/edgar/data/9999/000099992626000001/x.htm")["tier"] == 1
    reuters = strength("https://www.reuters.com/business/acme-2026-10-01/")
    assert reuters["tier"] == 2 and "not the official document" in reuters["why"]


# --- Tier 2 publishers, and blog posts -------------------------------------------------------------------------------

@pytest.mark.parametrize("url, publisher", [
    ("https://economictimes.indiatimes.com/tech/acme-results/articleshow/1.cms", "The Economic Times"),
    ("https://www.livemint.com/companies/acme-11.html", "Mint"),
    ("https://www.bworldonline.com/corporate/2026/10/01/acme/", "BusinessWorld"),
    ("https://business.inquirer.net/2026/acme", "Philippine Daily Inquirer"),
    ("https://www.nytimes.com/2026/10/01/business/acme.html", "The New York Times"),
    ("https://asia.nikkei.com/Business/acme", "Nikkei Asia"),
])
def test_top_tier_publishers_including_indian_and_philippine_are_tier2(url, publisher):
    s = strength(url)
    assert s["tier"] == 2 and publisher in s["why"]


@pytest.mark.parametrize("url", ["https://timesofindia.indiatimes.com/blogs/x/acme/", "https://www.forbes.com/sites/someone/2026/acme/",
                                 "https://www.moneycontrol.com/news/acme.html"])
def test_lookalikes_and_contributor_pages_are_not_tier2(url):
    assert strength(url) is None


@pytest.mark.parametrize("url, kind", [
    ("https://medium.com/@someone/acme-is-great-123", "platform"), ("https://acme.substack.com/p/news", "platform"),
    ("https://www.linkedin.com/pulse/acme-results-someone/", "platform"), ("https://www.forbes.com/sites/x/2026/acme/", "platform"),
    ("https://blogs.nvidia.com/blog/acme-partnership/", "own_domain"), ("https://blog.google/technology/ai/acme/", "own_domain"),
    ("https://www.acme.com/blog/q3-update", "own_domain"), ("https://www.acme.com/news/q3-update", None),
])
def test_blog_posts_are_recognised(url, kind):
    assert settling.blog_kind(url) == kind


def official(brand):
    return {"official": True, "brand": brand, "id": "Q1", "official_website": "https://x", "wikipedias": 40, "why": f"Wikidata Q1 '{brand}' ..."}


def test_an_official_blog_of_the_subject_or_a_named_company_is_tier1_and_any_other_blog_is_not():
    url = "https://blogs.nvidia.com/blog/acme-partnership/"
    profile = classify(url, [], text="", names=["Nvidia"])
    mine = settling.strength(profile, url, "What did Nvidia announce?", ["Nvidia"], blog=official("Nvidia"))
    assert mine["tier"] == 1 and mine["id"] == "company_document" and "official blog of Nvidia" in mine["why"]
    named = settling.strength(classify(url, [], names=["Acme"]), url, "What is Acme's partnership with Nvidia?", ["Acme"], blog=official("Nvidia"))
    assert named["tier"] == 1 and named["id"] == "official_blog"
    other = settling.strength(classify(url, [], names=["Acme"]), url, "What did Acme announce?", ["Acme"], blog=official("Nvidia"))
    assert other is None                                          # an official blog, but of a company the question does not name
    fake = settling.strength(classify(url, [], names=["Nvidia"]), url, "What did Nvidia announce?", ["Nvidia"],
                             blog={"official": False, "brand": None, "why": "no Wikidata organisation lists it"})
    assert fake is None


def test_the_major_brand_check_needs_the_official_website_and_many_wikipedias(monkeypatch):
    from tools import primary
    def wikidata(params):
        if params["action"] == "wbsearchentities":
            return {"search": [{"id": "Q182477", "label": "Nvidia"}, {"id": "Q9", "label": "Nvidia fan club"}]}
        return {"entities": {
            "Q182477": {"claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://www.nvidia.com/"}}}]},
                        "sitelinks": {f"{c}wiki": {} for c in ["en", "de", "fr", "es", "it", "ja", "ko", "zh", "ru", "pt", "pl", "nl"]},
                        "labels": {"en": {"value": "Nvidia"}}},
            "Q9": {"claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://nvidiafans.example/"}}}]}, "sitelinks": {}}}}
    monkeypatch.setattr(primary, "_wikidata", wikidata)
    yes = primary.brand_of_domain("blogs.nvidia.com", 10)
    assert yes["official"] and yes["brand"] == "Nvidia" and yes["wikipedias"] == 12
    no = primary.brand_of_domain("blog.nvidia-gaming-news.com", 10)
    assert not no["official"] and "no Wikidata organisation" in no["why"]
    small = primary.brand_of_domain("blogs.nvidia.com", 50)
    assert not small["official"] and "not a major brand" in small["why"]
    monkeypatch.setattr(primary, "_wikidata", lambda p: (_ for _ in ()).throw(ConnectionError("offline")))
    down = primary.brand_of_domain("blogs.nvidia.com", 10)
    assert not down["official"] and "could not be looked up" in down["why"]


def test_a_blog_post_is_checked_in_the_run_and_its_label_says_why(env, monkeypatch):
    from runner import Runner
    from tests.test_runner import make_runner
    from tests.scripted import ExtractedClaim, Extraction
    from tools.archive import archive_bytes, utc_now
    looked = []
    monkeypatch.setattr(Runner, "brand_lookup", staticmethod(lambda host, n: looked.append(host) or
                        {"official": False, "brand": None, "why": f"no Wikidata organisation lists {host} as its official website"}))
    page = "Acme pricing notes. " * 10 + "The Starter plan costs $29 per month, billed annually. " + "More notes. " * 10

    def fetch(url, max_chars=2000):
        sha, path = archive_bytes(f"<html><body><article><p>{page}</p></article></body></html>".encode(), ".html")
        return {"url": url, "final_url": "https://acmefan.example/blog/acme-prices", "status": 200, "content_type": "text/html",
                "encoding": "utf-8", "method": "http", "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}

    runner = make_runner(env, Extraction=lambda p: Extraction(claims=[ExtractedClaim(quote="The Starter plan costs $29 per month, billed annually.")]))
    runner.fetch_fn = fetch
    result = runner.start("What does Acme charge?", budgets={"max_iterations": 1})
    assert looked == ["acmefan.example"]                            # checked once for the domain
    rows = result["report"]["established"] + result["report"]["partial"]
    events = [e["payload"] for e in env["runs"].events(result["run_id"]) if e["type"] == "blog_checked"]
    assert events and events[0]["kind"] == "own_domain" and not events[0]["official"]
    assert not rows or "no Wikidata organisation lists" in rows[0]["corroboration"]
