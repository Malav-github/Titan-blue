"""The search benchmark scores a run on evidence: an expected item counts only when an established claim's quote has it."""

import benchmark


def test_the_cases_load_and_every_pattern_compiles():
    ids = [c["id"] for c in benchmark.cases()]
    assert ids == ["lyb_portfolio", "spgi_ai_2026", "spgi_q3_revenue", "dropbox_ceo", "attention_submitted"]


def test_an_item_counts_only_when_an_established_quote_has_it():
    case = {"id": "x", "expected": [{"item": "MoReTec", "patterns": ["MoReTec"]}, {"item": "Ferrara", "patterns": ["Ferrara"]}]}
    result = {"status": "finished", "run_id": "r", "report": {
        "summary": "MoReTec runs in Ferrara.",                     # the writer's words do not count
        "established": [{"quote": "Our MoReTec technology converts plastic waste.", "source_url": "https://www.lyondellbasell.com/a",
                         "source_tier": "primary"},
                        {"quote": "Recycling grew.", "source_url": "https://news.example/b", "source_tier": "secondary"}],
        "gaps": []}}
    events = [{"type": "claim_recorded"}] * 4 + [{"type": "search_done"}, {"type": "deadline_reached"}]
    s = benchmark.score(case, result, events, 61.4)
    assert s["found"] == ["MoReTec"] and s["missing"] == ["Ferrara"] and s["recall"] == 0.5
    assert (s["established"], s["recorded"], s["precision"], s["sites"], s["tier1"]) == (2, 4, 0.5, 2, 1)
    assert s["deadline"] and s["seconds"] == 61


def test_the_table_shows_the_previous_value_beside_a_changed_one():
    now = [{"id": "x", "recall": 1.0, "established": 30, "sites": 8, "tier1": 20, "precision": 0.3, "seconds": 200, "missing": []}]
    before = {"scores": [{"id": "x", "recall": 0.17, "established": 1, "sites": 1, "tier1": 1, "precision": 0.3, "seconds": 240}]}
    text = benchmark.table(now, before)
    assert "1.0 (0.17)" in text and "30 (1)" in text and "0.3 (" not in text
