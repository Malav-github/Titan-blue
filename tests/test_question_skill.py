"""The question skill: the kind of answer a question wants, by code, so the planner (a small model) does not decide it."""

import pytest

from skills.question import analyse
from tests.test_runner import env, make_runner  # noqa: F401


@pytest.mark.parametrize("question,kind,shape,checklist", [
    ("What was S&P Global's revenue for the third quarter of 2026?", "metric", "amount", ["S&P Global's revenue for the third quarter of 2026"]),
    ("Who is the current CEO of Dropbox?", "person_role", "person", ["CEO of Dropbox (current)"]),
    ("Who was the CFO of S&P Global in 2024?", "person_role", "person", ["CFO of S&P Global in 2024"]),
    ("On what exact date was 'Attention Is All You Need' first submitted?", "date_of", "date", ["Date 'Attention Is All You Need' first submitted"]),
    ("In what year was Dropbox founded?", "date_of", "date", ["Year Dropbox founded"]),
    ("Provide LyondellBasell's portfolio of chemical recycling technologies", "inventory", "items",
     ["Each distinct chemical recycling technology in LyondellBasell's portfolio, with what it is and its status"]),
    ("What does Acme charge?", "open", "open", None),                                       # the planner's checklist stands
])
def test_the_kind_shape_and_checklist_come_from_the_question(question, kind, shape, checklist):
    years = [int(y) for y in __import__("re").findall(r"\b20\d\d\b", question)]
    f = analyse(question, None, {"years": years})
    assert (f["kind"], f["shape"], f["checklist"]) == (kind, shape, checklist)


def test_a_title_in_quotes_is_the_subject_and_is_searched_as_an_exact_phrase():
    f = analyse("On what exact date was 'Attention Is All You Need' first submitted?")
    assert f["subject"] == "Attention Is All You Need" and f["queries"][0] == '"Attention Is All You Need" first submitted'


def test_a_question_asking_two_things_keeps_the_planners_checklist_but_gets_the_kinds_searches():
    f = analyse("What were Apple's net sales for fiscal 2025 and what period does that cover?")
    assert f["kind"] == "metric" and f["checklist"] is None and f["queries"][0] == "Apple fiscal 2025 net sales"


def test_relevance_by_answer_shape_says_yes_only_when_the_claim_has_the_shape(env):
    runner = make_runner(env)
    claim = lambda text: {"claim_text": text, "source_sha256": "none"}                          # noqa: E731
    runner._source_dates = lambda sha: []                                                        # no page date: the claim's words decide
    ceo = {"frame": analyse("Who is the current CEO of Dropbox?")}
    assert runner._answer_shaped(ceo, claim("Today, we announced that Ashraf Alkarmi will become Dropbox co-CEO alongside Drew Houston."))
    assert runner._answer_shaped(ceo, claim("Ali Dasdan, the Chief Technology Officer, will step down.")) is None     # another role
    rev = {"frame": analyse("What was S&P Global's revenue for the third quarter of 2026?")}
    assert runner._answer_shaped(rev, claim("Revenue was $3,678 million in the third quarter of 2026."))
    assert runner._answer_shaped(rev, claim("Revenue for the third quarter of 2010 was $1,956,000,000.")) is None       # another year
    when = {"frame": analyse("On what exact date was 'Attention Is All You Need' first submitted?")}
    assert runner._answer_shaped(when, claim("Submitted on 12 Jun 2017 (v1), last revised 2 Aug 2023."))
    events = {"frame": {"shape": "events"}, "enumerate": {"topic": "AI"}, "time_scope": {"years": [2026]}}
    assert runner._answer_shaped(events, claim("S&P Global launched an AI assistant for analysts in 2026."))
    assert runner._answer_shaped(events, claim("S&P Global said it maintained its dividend.")) is None    # 'ai' in 'said' is no topic
    assert runner._answer_shaped(events, claim("S&P Global launched an AI tool in 2024.")) is None          # another year


def test_a_premise_naming_a_title_is_checked_by_code_against_the_results(env):
    runner = make_runner(env)
    runner.search_fn = lambda q, max_results=10: {"providers": [], "results": [
        {"url": "https://arxiv.org/abs/1706.03762", "title": "[1706.03762] Attention Is All You Need", "snippet": "The dominant ..."}]}
    out = runner._screen_premise({"run_id": "r1", "question": "q"}, ["A paper titled 'Attention Is All You Need' was submitted on an exact date"])
    assert out["premise_status"] == "reported" and out["premise_screen"]["checker"] == "code:names_it"


def test_the_framed_checklist_replaces_the_planners_and_the_run_says_so(env):
    from runner import Plan
    runner = make_runner(env, Plan=lambda p: Plan(subject="Acme", queries=["acme boss"], checklist=["Acme leadership", "Acme history"]))
    result = runner.start("Who is the current CEO of Acme?", budgets={"max_iterations": 1})
    framed = next(e["payload"] for e in env["runs"].events(result["run_id"]) if e["type"] == "question_framed")
    assert framed["kind"] == "person_role" and framed["checklist"] == ["CEO of Acme (current)"]
    assert framed["replaced"] == ["Acme leadership", "Acme history"]          # the planner added a fact nobody asked for
