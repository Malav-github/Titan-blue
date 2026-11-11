"""Search phase 2, from a real run ('Provide LyondellBasell's portfolio of chemical recycling technologies'): a portfolio is a
list of items, the names the sources mention are searched for next, filings are read only when the question is about what
filings hold, a planner's `site:` is kept only where it fits, and a product page's date is not an announcement date."""

from skills import writing
from skills.planning import names, site_rules
from skills.planning.enumerate import enumeration
from tests.test_edgar_skill import edgar_runner
from tests.test_runner import env, make_runner  # noqa: F401

LYB_PAGE = ("LyondellBasell's MoReTec technology is a catalytic advanced recycling process. CirculenRevive polymers are made "
            "with it. The QCP joint venture with SUEZ recycles plastic waste. LYB shares trade on the NYSE. MoReTec runs at the "
            "Wesseling site.")


def test_a_portfolio_question_is_a_list_of_items_and_an_event_question_stays_a_list_of_events():
    lyb = enumeration("Provide LyondellBasell's portfolio of chemical recycling technologies", "LyondellBasell", {})
    assert lyb["kind"] == "inventory" and lyb["form"] == "inventory"
    assert lyb["item"] == "Each distinct chemical recycling technology in LyondellBasell's portfolio, with what it is and its status"
    assert enumeration("Which chemical recycling technologies does LyondellBasell have?", "LyondellBasell", {})["kind"] == "inventory"
    assert enumeration("What products did Acme launch in 2026?", "Acme", {"years": [2026]})["kind"] == "events"
    assert enumeration("What does Acme charge?", "Acme", {}) is None


def test_names_are_picked_out_by_code_without_the_subject_its_ticker_or_trade_abbreviations():
    found = names.named_items([LYB_PAGE], ["LyondellBasell", "LYB"])
    assert found[0] == "MoReTec" and {"CirculenRevive", "QCP", "SUEZ"} <= set(found)
    assert not {"LyondellBasell", "LYB", "NYSE"} & set(found)
    assert names.mentions("The MoReTec plant", "moretec") and not names.mentions("MoReTecX", "MoReTec")


def test_a_planners_site_is_kept_only_where_the_question_points_to_it():
    q = "Provide LyondellBasell's portfolio of chemical recycling technologies"
    queries, changes = site_rules(["LyondellBasell chemical recycling site:lyondellbasell.com",
                                   "LyondellBasell chemical recycling portfolio site:sec.gov",
                                   "LyondellBasell chemical recycling site:plasticstoday.com",
                                   "LyondellBasell recycling patents site:patents.google.com",
                                   "LyondellBasell recycling site:prnewswire.com"], q, "LyondellBasell")
    assert queries == ["LyondellBasell chemical recycling site:lyondellbasell.com", "LyondellBasell chemical recycling portfolio",
                       "LyondellBasell chemical recycling", "LyondellBasell recycling patents", "LyondellBasell recycling site:prnewswire.com"]
    assert len(changes) == 3
    kept, none = site_rules(["Acme revenue site:sec.gov", "Acme patents site:patents.google.com"],
                            "What was Acme's revenue, and which patents does it hold?", "Acme")
    assert kept == ["Acme revenue site:sec.gov", "Acme patents site:patents.google.com"] and none == []


def test_a_product_pages_date_is_not_when_anything_on_it_happened():
    def row(url, kind="search_result"):
        return {"claim_id": 1, "claim": "LyondellBasell's proprietary catalytic chemical recycling MoReTec technology.", "quote": "x",
                "times": [], "source_url": url, "source_date": "2025-07-30", "source_date_kind": kind, "source_tier": "primary"}
    page = writing.build_facts([row("https://www.lyondellbasell.com/en/our-solutions/circular-low-carbon-solutions/chemical-recycling/")])[0]
    assert not page.dated and page.basis == "reference" and "not when anything on it happened" in page.origin
    news = writing.build_facts([row("https://www.lyondellbasell.com/en/news-events/products--technology-news/moretec-plant/")])[0]
    assert news.dated and news.basis == "page"
    release = writing.build_facts([row("https://press.acme.example/x")])[0]
    assert release.dated and release.basis == "page"


def test_filings_are_not_read_for_a_question_they_do_not_hold(env):
    runner, sec, _ = edgar_runner(env)
    result = runner.start("Which products does Acme make?", budgets={"max_iterations": 1})
    events = env["runs"].events(result["run_id"])
    assert any(e["type"] == "edgar_skipped" for e in events)
    assert sec.calls == []                                  # the CIK is known; no filing list is read


def test_an_inventory_answer_has_one_entry_per_named_item(env):
    runner = make_runner(env)
    rows = [{"claim_id": i, "quote": q, "claim": q, "source_url": "https://www.lyondellbasell.com/x", "source_tier": "primary",
             "source_label": "Company web page", "times": [], "corroboration": "primary source"} for i, q in enumerate([
                 "MoReTec is a catalytic advanced recycling technology.", "The first MoReTec plant is in Wesseling.",
                 "CirculenRevive polymers are made from plastic waste.", "LyondellBasell aims to produce 2 million tons of recycled polymers."], 1)]
    s = {"subject": "LyondellBasell", "company": "LyondellBasell", "entity_name": "LyondellBasell", "question": "q",
         "enumerate": {"kind": "inventory", "what": "chemical recycling technology"}}
    runner._cite = lambda r: f"“{r['quote']}” [claim:{r['claim_id']}]"
    text = runner._inventory(s, rows, [], [])
    assert text.startswith("The evidence read names 2 items in or around LyondellBasell's chemical recycling technologies "
                           "(technologies, product lines and partners alike), most cited first.")
    moretec, revive = text.index("MoReTec:"), text.index("CirculenRevive:")
    assert moretec < revive and "[claim:2]" in text[moretec:revive]                  # most cited first, both MoReTec claims under it
    assert text.index("Other statements on the portfolio:") > revive and "[claim:4]" in text.split("Other statements")[1]


def test_a_statement_naming_the_whole_family_is_said_once_and_meta_copies_are_merged(env):
    runner = make_runner(env)
    runner._cite = lambda r: f"“{r['quote']}” [claim:{r['claim_id']}]"
    family = "Circulen comes in three variants: CirculenRecover, CirculenRevive and CirculenRenew."
    rows = [{"claim_id": i, "quote": q, "claim": q} for i, q in enumerate([
        f"description: {family}", f"og:description: {family}", "CirculenRevive polymers are chemically recycled.",
        "CirculenRecover polymers are mechanically recycled."], 1)]
    s = {"subject": "LyondellBasell", "company": "LyondellBasell", "entity_name": "LyondellBasell", "question": "q",
         "enumerate": {"kind": "inventory", "what": "product"}}
    text = runner._inventory(s, rows, [], [])
    assert text.count("three variants") == 1 and "[claim:2]" not in text
    assert text.index("Across the portfolio:") < text.index("CirculenRevive:")


def test_a_claim_counts_for_a_portfolio_only_when_it_speaks_to_the_topic(env):
    runner = make_runner(env)
    s = {"enumerate": {"kind": "inventory", "topic": "chemical recycling", "noun": "technologies"}}
    items = ["MoReTec", "TBA"]
    assert runner._names_an_item(s, items, "MoReTec is a catalytic chemical recycling technology.")
    assert not runner._names_an_item(s, items, "The PO/TBA process is invented by ARCO, producing propylene oxide.")


def test_a_typo_in_the_question_takes_the_planners_spelling():
    from runner import _spelled_as
    assert _spelled_as("Provide LyondellBasell's portfolio of chemical recyclying technologies",
                       "LyondellBasell chemical recycling technologies site:lyondellbasell.com") == \
        "Provide LyondellBasell's portfolio of chemical recycling technologies"
    assert _spelled_as("What does Dropbox charge?", "Dropbox pricing plans") == "What does Dropbox charge?"
