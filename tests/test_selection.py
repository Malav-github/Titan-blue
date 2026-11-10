"""Coverage, not repetition: which pages are read (selection.py), and how much of each page the model is shown (sentences.py)."""

import selection
import sentences

RELEASE = "S&P Global Expands AI-Powered Capabilities Across Private Markets"


def test_copies_of_one_article_are_read_once_from_the_best_copy():
    cands = [
        {"url": "https://www.nasdaq.com/press-release/sp-global-expands-ai", "title": f"{RELEASE} | Nasdaq", "class": "other"},
        {"url": "https://www.prnewswire.com/news-releases/sp-global-expands-ai-302", "title": RELEASE, "class": "wire_release"},
        {"url": "https://press.spglobal.com/2026-10-22-S-P-Global-Expands-AI", "title": RELEASE, "class": "company_site"},
        {"url": "https://www.reuters.com/business/sp-global-ai-tools-2026", "title": "S&P Global bets on AI agents for analysts - Reuters", "class": "major_publication"},
    ]
    kept, merged, capped = selection.diversify(cands)
    assert [c["url"] for c in kept] == ["https://press.spglobal.com/2026-10-22-S-P-Global-Expands-AI",   # the company's own copy
                                        "https://www.reuters.com/business/sp-global-ai-tools-2026"]
    assert {c["url"] for c in merged} == {cands[0]["url"], cands[1]["url"]} and capped == []


def test_no_site_fills_the_reading_list_but_the_company_and_regulators_may():
    topics = ["Oil prices slide as supply rises", "Chipmakers rally on strong demand", "Retail sales beat forecasts again",
              "Bond yields climb before the meeting"]
    yahoo = [{"url": f"https://finance.yahoo.com/news/story-{i}", "title": t, "class": "other"} for i, t in enumerate(topics)]
    releases = ["Acme opens a plant in Pune", "Acme names a new finance chief", "Acme launches its Starter plan",
                "Acme agrees to buy Fabrikam"]
    own = [{"url": f"https://press.acme.example/2026-0{i}", "title": t, "class": "company_site"} for i, t in enumerate(releases)]
    kept, merged, capped = selection.diversify(yahoo + own)
    assert len([c for c in kept if "yahoo" in c["url"]]) == 2 and len(capped) == 2
    assert len([c for c in kept if "acme" in c["url"]]) == 4                            # the company's own releases are the point


def test_a_small_document_is_shown_to_the_model_whole():
    units = [f"Sentence {i} about anything at all." for i in range(30)]
    [pack] = sentences.packs(units, sentences.candidates(units, {"revenue"}), limit=3)
    assert pack == list(range(30))                                                      # nothing filtered when it all fits


def test_a_long_document_is_scanned_whole_by_code_and_the_relevant_sentences_go_to_the_model():
    filler = [f"Paragraph {i} describes the office furniture policy in detail." for i in range(3000)]
    units = filler[:1500] + ["Revenue for the three months ended September 30, 2026 was $3,678 million."] + filler[1500:]
    chosen = sentences.candidates(units, {"revenue", "quarter"})
    batches = sentences.packs(units, chosen, limit=2)
    sent = [i for p in batches for i in p]
    assert 1500 in sent and 1499 in sent and 1501 in sent                               # the fact, with its neighbours
    assert len(sent) < len(units) / 10                                                  # not the whole furniture policy
    shown = sentences.numbered_at(units, batches[0])
    assert "[1501] Revenue for the three months ended" in shown and "..." in shown        # numbered by place in the document


def test_word_stems_match_so_reported_meets_report():
    units = ["Introduction to the company.", "Overview.", "Contents.", "Unrelated line one here."] * 2000 + ["Acme reported revenues of $1.2 billion."]
    assert len(units) - 1 in sentences.candidates(units, {"report", "revenue"})


def test_headlines_that_differ_in_a_period_or_number_are_different_documents():
    q1 = {"url": "https://press.spglobal.com/2026-04-28-results", "title": "S&P Global Reports First Quarter 2026 Results", "class": "company_site"}
    q2 = {"url": "https://www.prnewswire.com/news-releases/q2-results", "title": "S&P Global Reports Second Quarter 2026 Results", "class": "wire_release"}
    kept, merged, _ = selection.diversify([q1, q2])
    assert len(kept) == 2 and merged == []
