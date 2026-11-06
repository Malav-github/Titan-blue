"""The writing skill: dated facts grouped into moments, the form a request asks for, the prompt, and the evaluation matrix that scores
an output by code. The first case is the real one that started it: a strategic review (October 2026) and an acquisition years
earlier written up as if they happened together."""

from datetime import date

import pytest

from runner import Summary
from skills import writing
from skills.writing import golden
from tests.test_runner import env, make_runner, types  # noqa: F401

TODAY = date(2026, 11, 6)
LATEST = {"intent": "latest"}


def row(cid, text, url="https://press.spglobal.com/x", when="2026-10-27T03:03:26", kind="search_result", times=None, tier="primary", fact=0, corroboration="primary source"):
    return {"claim_id": cid, "claim": text, "value": "", "quote": text, "times": times or [], "source_url": url, "source_date": when,
            "source_date_kind": kind, "source_tier": tier, "corroboration": corroboration, "fact": fact}


ROWS = [
    row(222, "S&P Global announced a strategic review for Capital IQ and associated data businesses."),
    row(223, "S&P Global has expanded AI-powered capabilities across its private markets ecosystem.", when="2026-10-22T13:28:28"),
    row(224, "S&P Global has expanded its AI-powered capabilities across private markets.", when="2026-10-22T13:28:28"),
    row(230, "S&P Global agreed to acquire IHS Markit analytics company in a $44 billion transaction.", url="https://www.reuters.com/markets/deals/x",
        when="", kind="", tier="secondary", corroboration="single source, unconfirmed"),
]
FACTS = writing.build_facts(ROWS)

BAD = ("S&P Global announced a strategic review for Capital IQ and associated data businesses [claim:222]. It has expanded AI-powered capabilities "
       "across its private markets ecosystem [claim:223], and also expanded its AI-powered capabilities across private markets [claim:224]. "
       "Additionally, S&P Global agreed to acquire IHS Markit analytics company in a $44 billion transaction [claim:230]. The claims do not "
       "establish the specific outcomes or details of the strategic review.")
GOOD = ("On 27 October 2026, S&P Global announced a strategic review for Capital IQ and associated data businesses [claim:222]. "
        "On 22 October 2026, it expanded its AI-powered capabilities across private markets [claim:223][claim:224]. "
        "Earlier, on a date the sources do not give, Reuters reported that S&P Global agreed to acquire IHS Markit in a $44 billion transaction [claim:230]. "
        "The claims do not establish the outcome of the strategic review.")


def rate(text, rows=None, **kw):
    facts = writing.build_facts(rows or ROWS)
    kw = {"question": "S&P Global's latest strategic move.", "subject": "S&P Global", "scope": LATEST, "today": TODAY,
          "gaps": ["outcome of the review"], "form": writing.form_by_id("news"), **kw}
    return writing.score(text, facts, **kw)


# --- facts and moments ---

def test_a_reference_page_date_is_not_the_events_date():
    by = {f.claim_id: f for f in FACTS}
    assert by[230].basis == "none" and not by[230].dated and by[230].source_class == "major_publication"
    wiki = writing.build_facts([row(9, "S&P Global agreed to acquire IHS Markit.", url="https://en.wikipedia.org/wiki/S%26P_Global",
                                    when="2004-05-13T02:43:58+00:00", kind="published", tier="secondary")])[0]
    assert wiki.basis == "reference" and not wiki.dated                    # Wikipedia's 2004 'published' date says nothing about the deal
    assert by[222].basis == "page" and by[222].iso() == "2026-10-27"


def test_an_event_date_beats_the_page_date_and_a_stated_year_beats_both():
    event = writing.build_facts([row(1, "Acme closed the deal.", times=[{"kind": "closed", "value": "2022-02-28"}])])[0]
    assert event.basis == "event" and event.iso() == "2022-02-28"
    stated = writing.build_facts([row(2, "In March 2020 Acme agreed to buy Beta.")])[0]
    assert stated.basis == "stated" and stated.iso() == "2020-03" and stated.precision == "month"
    year = writing.build_facts([row(3, "Acme has paid a dividend since 1999.")])[0]
    assert year.iso() == "1999" and year.basis == "stated"


def test_the_word_markets_is_not_a_month():
    assert writing.build_facts([row(1, "Acme grew in private markets 2026 forecasts say.", when="")])[0].basis == "none"


def test_events_far_apart_are_separate_moments_and_undated_ones_come_last():
    moments = writing.groups(FACTS)
    assert [[f.claim_id for f in m] for m in moments] == [[222, 223, 224], [230]]      # 5 days apart share a moment; the undated deal does not
    far = writing.build_facts([row(1, "A.", when="2026-10-27"), row(2, "B.", when="2020-11-30")])
    assert len(writing.groups(far)) == 2


# --- forms ---

@pytest.mark.parametrize("question,form", [
    ("S&P Global's latest strategic move.", "news"), ("What is Dropbox's headquarters city?", "answer"),
    ("Write a white paper on carbon capture", "white_paper"), ("Write an essay arguing for open data", "essay"),
    ("A long-form feature on the history of Intel", "long_form"), ("Give me a report on Acme", "report"),
    ("A one sentence summary of the merger", "short_form"), ("Research note on Acme's margins", "research_note")])
def test_the_form_follows_the_request(question, form):
    assert writing.choose_form(question)["id"] == form


def test_a_form_the_runner_cannot_write_yet_is_written_as_the_nearest_one():
    assert writing.written_as(writing.form_by_id("white_paper"))["id"] == "answer"
    assert writing.written_as(writing.form_by_id("report"))["id"] == "news"


def test_every_rule_and_check_in_the_data_exists_in_code():
    from skills.writing.evaluate import CHECKS
    for d in writing.matrix()["dimensions"]:
        assert set(d["checks"]) <= set(CHECKS), d["id"]
    named = {r["check"][5:] for a in writing.rules() for r in a["rules"] if r["check"].startswith("auto:")}
    assert named <= set(CHECKS)


# --- the prompt ---

def test_the_prompt_carries_today_the_moments_and_the_undated_warning():
    p = writing.draft_prompt("S&P Global's latest strategic move.", FACTS, writing.form_by_id("news"), LATEST, TODAY, ["outcome"])
    assert "TODAY: 2026-11-06" in p and "MOMENT 1: 22 October 2026 to 27 October 2026" in p and "NO RELIABLE DATE" in p
    assert "did NOT happen together" in p and "LATEST" in p and "NOT ESTABLISHED" in p
    assert "NO RELIABLE DATE" in p and "[claim:230]" in p and "News article by Reuters" in p
    assert "MUST end with the tag" in p and "never as 2026-10-27" in p and "Do not say one caused" in p


# --- the matrix on the case that started it ---

def test_the_unaware_output_scores_low_and_names_the_fault():
    r = rate(BAD)
    assert r["score"] <= 50 and r["grade"] == "D"
    assert [b["id"] for b in r["blockers"]] == ["false_simultaneity", "no_dates_for_latest"]
    assert any("simultaneity word joins claim 230" in i for i in r["issues"])
    assert any("does not state its date" in i for i in r["issues"])
    assert any("say the same thing and it is written out twice" in i for i in r["issues"])          # 223 and 224 written out twice


def test_the_time_aware_output_scores_high_with_no_blockers():
    r = rate(GOOD)
    assert r["blockers"] == [] and r["score"] >= 90, r["issues"]
    assert r["dimensions"]["time"]["score"] == 1.0


def test_one_sentence_may_join_two_far_apart_events_only_if_it_dates_both():
    rows = [row(1, "Acme announced a review.", when="2026-10-27"), row(2, "Acme agreed to buy Beta in 2020.", when="")]
    undated = rate("Acme announced a review and agreed to buy Beta [claim:1][claim:2].", rows)
    dated = rate("Acme announced a review on 27 October 2026 and agreed to buy Beta in 2020 [claim:1][claim:2].", rows)
    simultaneity = lambda r: next(c for c in r["dimensions"]["time"]["checks"] if c["id"] == "simultaneity")["score"]  # noqa: E731
    assert simultaneity(undated) < 1 and simultaneity(dated) == 1


def test_a_latest_answer_must_lead_with_the_newest_event():
    rows = [row(1, "Acme announced a review.", when="2026-10-27"), row(2, "Acme completed the sale of Beta.", when="2025-03-04")]
    older_first = "On 4 March 2025, Acme completed the sale of Beta [claim:2]. On 27 October 2026, Acme announced a review [claim:1]."
    r = rate(older_first, rows, subject="Acme", gaps=[])
    check = lambda name: next(c for d in r["dimensions"].values() for c in d["checks"] if c["id"] == name)   # noqa: E731
    assert check("latest_first")["score"] == 0 and check("lead")["score"] == 0 and check("order")["score"] < 1
    newest_first = "On 27 October 2026, Acme announced a review [claim:1]. Earlier, on 4 March 2025, Acme completed the sale of Beta [claim:2]."
    r = rate(newest_first, rows, subject="Acme", gaps=[])
    assert check("latest_first")["score"] == 1 and check("order")["score"] == 1


def test_a_number_no_claim_contains_is_a_blocker():
    r = rate(GOOD.replace("$44 billion", "$55 billion"))
    assert [b["id"] for b in r["blockers"]] == ["wrong_number"] and r["score"] <= 40


def test_citing_a_claim_that_is_not_established_is_a_blocker():
    r = rate(GOOD + " Something else [claim:999].")
    assert "invalid_citation" in [b["id"] for b in r["blockers"]]


def test_relative_time_words_need_a_date():
    r = rate("S&P Global recently announced a strategic review [claim:222].", ROWS[:1])
    assert any("stands in for a date" in i for i in r["issues"])


def test_gaps_must_be_said_and_certainty_words_are_penalised():
    silent = rate("On 27 October 2026, S&P Global announced a strategic review for Capital IQ and associated data businesses [claim:222].", ROWS[:1])
    assert any("does not say so" in i for i in silent["issues"])
    sure = rate("On 27 October 2026, S&P Global definitely announced a strategic review for Capital IQ and associated data businesses [claim:222].", ROWS[:1])
    assert any("goes beyond the evidence" in i for i in sure["issues"])


def test_filler_and_over_long_sentences_are_penalised():
    r = rate("It is important to note that on 27 October 2026 S&P Global announced a strategic review for Capital IQ and associated data businesses "
             "and that this plays a crucial role in how the company will organise itself over the next several years [claim:222].", ROWS[:1])
    assert any("filler" in i for i in r["issues"]) and any("over 30 words" in i for i in r["issues"])


def test_nothing_written_is_not_scored():
    assert rate("")["score"] is None


def test_the_score_is_the_same_every_time():
    assert rate(BAD) == rate(BAD)


# --- the golden set ---

def report_of(summary):
    est = [r | {"established": True} for r in ROWS]
    return {"question": "S&P Global's latest strategic move.", "subject": "S&P Global", "summary": summary, "established": ROWS, "partial": [],
            "gaps": ["outcome of the review"], "time": {"today": "2026-11-06", "scope": "latest"}}


def test_a_captured_case_is_rescored_by_code_and_a_person_must_give_reasons(tmp_path):
    case = golden.case_from_report(report_of(BAD), "sp", note="seen in the app")
    assert golden.auto_score(case)["score"] <= 50
    scores = {d["id"]: 3 for d in writing.matrix()["dimensions"]}
    with pytest.raises(ValueError, match="reason"):
        golden.grade(case, scores, {})
    with pytest.raises(ValueError, match="whole number"):
        golden.grade(case, {**scores, "time": 9}, {k: "x" for k in scores})
    golden.grade(case, {**scores, "time": 1}, {k: f"because {k}" for k in scores}, grader="malav")
    assert case["human"]["scores"]["time"] == 1 and case["human"]["reasons"]["time"] == "because time"
    assert golden.overall_of(case["human"]["scores"]) == pytest.approx(0.2 * 25 + 3 / 5 * 75, abs=0.1)


def test_compare_reports_the_gap_between_code_and_person_per_dimension(tmp_path):
    bad, good = golden.case_from_report(report_of(BAD), "bad"), golden.case_from_report(report_of(GOOD), "good")
    ids = [d["id"] for d in writing.matrix()["dimensions"]]
    golden.grade(bad, {i: 1 if i == "time" else 3 for i in ids}, {i: "r" for i in ids})
    golden.grade(good, {i: 5 for i in ids}, {i: "r" for i in ids})
    out = golden.compare({"cases": [bad, good, golden.case_from_report(report_of(GOOD), "ungraded")]})
    assert out["graded"] == 2 and out["ungraded"] == 1
    assert out["dimensions"]["time"]["within_one"] >= 0.5 and out["dimensions"]["time"]["mean_abs_gap"] <= 1.5
    assert {o["id"] for o in out["overall"]} == {"bad", "good"}


def test_the_golden_file_round_trips(tmp_path):
    path = tmp_path / "g.json"
    data = golden.load(path)
    data["cases"].append(golden.case_from_report(report_of(GOOD), "g"))
    golden.save(data, path)
    assert golden.load(path)["cases"][0]["id"] == "g"


# --- in the runner ---

def test_the_summary_prompt_carries_the_dates_and_a_blocking_draft_is_written_again_once(env):
    seen = []

    def summary(prompt):
        seen.append(prompt)
        return Summary(text="Starter costs $29 [claim:1]. Also the Team plan costs $89 [claim:2].") if len(seen) == 1 else \
            Summary(text="Acme's Starter plan costs $29 per month [claim:1].")

    result = make_runner(env, Summary=summary).start("What does Acme charge?")
    assert "TODAY:" in seen[0] and "[claim:1]" in seen[0] and "RULES" in seen[0]
    q = result["report"]["quality"]
    assert q["score"] is not None and q["form"] in ("answer", "news")
    assert "summary_scored" in types(env, result["run_id"])


def test_an_uncited_editorial_sentence_is_a_blocker_and_a_judgement_the_claims_do_not_make_is_penalised():
    text = ("On 22 October 2026, S&P Global expanded its AI-powered capabilities across private markets [claim:223]. "
            "The move marks a significant enhancement in its analytics tools.")
    r = rate(text, ROWS[:3], gaps=[])
    assert "uncited_statement" in [b["id"] for b in r["blockers"]] and r["score"] <= 85
    assert any("carry no citation" in i for i in r["issues"]) and any("is a judgement that no claim makes" in i for i in r["issues"])


def test_a_link_word_no_claim_uses_is_penalised_in_time_awareness():
    text = ("On 27 October 2026, S&P Global announced a strategic review for Capital IQ and associated data businesses [claim:222]. "
            "The review follows the AI expansion on 22 October 2026 [claim:223].")
    r = rate(text, ROWS[:3], gaps=[])
    assert any("'follows' links events in a way no claim states" in i for i in r["issues"])


def test_sentences_that_state_something_without_a_claim_are_removed_and_reported():
    kept, removed = writing.drop_uncited(
        "On 27 October 2026, S&P Global announced a strategic review [claim:222]. Earlier, in 2020, it bought data firms. "
        "The claims do not establish the outcome.")
    assert kept == "On 27 October 2026, S&P Global announced a strategic review [claim:222]. The claims do not establish the outcome."
    assert removed == ["Earlier, in 2020, it bought data firms."]


def test_the_runner_removes_an_invented_sentence_and_says_so(env):
    from tests.test_runner import types
    result = make_runner(env, Summary=lambda p: Summary(text="Acme's Starter plan costs $29 per month [claim:1]. It has long led the market.")).start("What does Acme charge?")
    report = result["report"]
    assert "led the market" not in report["summary"] and report["quality"]["uncited_removed"] == ["It has long led the market."]
    assert "uncited_removed" in types(env, result["run_id"])
