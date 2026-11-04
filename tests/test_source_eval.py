"""Grading of source-ladder runs: only a false answer is 'wrong'; a gap is an honest miss; the unclear go to a person."""

import source_eval as se


def case(kind, **expect):
    return {"id": "x", "route": [], "expect": {"kind": kind, **expect}}


def report(established=(), summary="", **extra):
    return {"summary": summary, "established": [dict(claim=c, quote=q, value="", **d) for c, q, d in established], **extra}


def test_expected_value_in_an_established_claim_is_correct_even_with_thousands_separators():
    r = report([("Apple's net sales were $391,035 million.", "Total net sales 391,035", {})])
    assert se.grade(case("contains_any", answers=["391035", "391.0"]), r)["outcome"] == "correct"


def test_all_groups_must_be_stated():
    r = report([("Net sales were 245.1 billion in fiscal 2024.", "q", {})])
    c = case("contains_all", groups=[["245.1"], ["211.9"]])
    assert se.grade(c, r)["outcome"] == "review"
    r2 = report([("245.1 billion in 2024 and 211.9 billion in 2023", "q", {})])
    assert se.grade(c, r2)["outcome"] == "correct"


def test_nothing_established_is_an_honest_miss_for_answerable_questions_and_correct_for_traps():
    assert se.grade(case("contains_any", answers=["x"]), report())["outcome"] == "honest_miss"
    assert se.grade(case("not_established"), report())["outcome"] == "correct"


def test_a_trap_that_gets_an_established_answer_is_wrong():
    assert se.grade(case("not_established"), report([("Acme made $5.", "q", {})]))["outcome"] == "wrong"


def test_a_mismatch_goes_to_a_person_and_partial_claims_do_not_count():
    r = report([("It was 2008.", "q", {})])
    assert se.grade(case("contains_any", answers=["2007"]), r)["outcome"] == "review"
    r["partial"] = [{"claim": "It was 2007.", "quote": "2007"}]
    assert se.grade(case("contains_any", answers=["2007"]), r)["outcome"] == "review"


def test_time_sensitive_needs_dated_sources():
    assert se.grade(case("time_sensitive"), report())["outcome"] == "honest_miss"
    assert se.grade(case("time_sensitive"), report([("c", "q", {"source_date": "2026-05-26"})]))["outcome"] == "correct"
    assert se.grade(case("time_sensitive"), report([("c", "q", {"source_date": None})]))["outcome"] == "wrong"


def test_route_check_and_totals():
    c = {"route": ["sec_edgar"]}
    ev = [{"type": "sources_routed", "payload": {"offered": ["sec_edgar"], "hidden": []}}]
    assert se.check_route(c, ev)["ok"] and not se.check_route({"route": []}, ev)["ok"]
    assert se.check_route({"route": []}, [])["ok"]                       # no connector was active, nothing offered
    results = [{"outcome": "correct", "route": {"ok": True}}, {"outcome": "wrong", "route": {"ok": False}}]
    assert se.summarize(results) == {"cases": 2, "correct": 1, "honest_miss": 0, "review": 0, "wrong": 1, "route_ok": 1}


def test_every_case_in_the_file_can_be_graded_against_an_empty_report():
    for c in se.load_cases():
        assert se.grade(c, report())["outcome"] in se.OUTCOMES
