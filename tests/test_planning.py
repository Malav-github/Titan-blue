"""The planning skill: what a question points to, where to look first, which document, and what 'latest' means today."""

from datetime import datetime, timezone

import pytest

from runner import Plan
from skills import planning
from skills.temporal.clock import now
from skills.temporal.intent import classify
from tests.test_runner import env, make_runner  # noqa: F401


def at(*args):
    moment = datetime(*args, tzinfo=timezone.utc)
    return lambda: moment


NOW = now(at(2026, 11, 4, 3, 51), zone="Asia/Kolkata")


def brief(question):
    return planning.build(question, None, classify(question, NOW), NOW)


def test_the_catalog_and_filing_types_load_and_validate():
    assert {"company_disclosure", "patent", "research_paper", "press_release"} <= {t["id"] for t in planning.catalog()}
    assert {"10-K", "10-Q", "8-K", "DEF 14A"} <= {d["id"] for d in planning.documents()}
    assert planning.manifest()["name"] == "planning"


@pytest.mark.parametrize("question, first_site", [
    ("What are Apple's latest 10-Q net sales?", "sec.gov"),
    ("Which patents did Nvidia publish on ray tracing?", "patents.google.com"),
    ("When was the Attention Is All You Need paper first submitted?", "arxiv.org"),
    ("What did Stripe announce in its press release about its funding round?", "the company's newsroom"),
])
def test_each_kind_of_question_leads_with_its_own_best_place(question, first_site):
    assert brief(question).places[0]["site"] == first_site


def test_a_wire_service_that_keeps_the_release_unchanged_is_ranked_below_the_originator():
    places = brief("What did Stripe announce in its press release?").places
    roles = [p["role"] for p in places]
    assert roles[0] == "primary" and "keeps_original" in roles
    assert {"prnewswire.com", "businesswire.com"} <= {p["site"] for p in places} or len(places) == planning.setting("max_places")


def test_latest_means_the_period_closest_to_today_and_names_the_newest_filing_expected():
    b = brief("What are Apple's latest quarterly net sales?")
    assert b.recent and "closest to today (2026-11-04)" in b.target
    tenq = next(d for d in b.documents if d["id"] == "10-Q")
    assert "2026-09-30" not in tenq["expect"]            # 40 days have not passed since 30 September
    assert "2026-06-30" in tenq["expect"] and "period of report" in tenq["expect"]
    tenk = next(d for d in brief("What does the latest annual report say about risk factors of Apple?").documents if d["id"] == "10-K")
    assert "2025-12-31" in tenk["expect"]


def test_a_named_year_is_the_target_not_today():
    b = brief("What were Apple's net sales in fiscal 2023?")
    assert not b.recent and "2023" in b.target
    assert all("Latest wanted" not in d["expect"] for d in b.documents)


def test_filing_knowledge_reaches_the_brief():
    docs = {d["id"]: d for d in brief("Who was appointed as the new CFO of Tesla?").documents}
    assert "8-K" in docs and "5.02" in docs["8-K"]["holds"]
    assert any("effective date" in m for m in brief("Who was appointed as the new CFO of Tesla?").metadata)
    patent = brief("Which patents does Nvidia hold?")
    assert any(d["id"] == "patent" and "kind code" in d["label"].lower() for d in patent.documents)
    assert "priority date" in patent.metadata


def test_a_question_that_points_to_nothing_known_gets_no_brief():
    b = brief("Why is the sky blue?")
    assert b.empty and b.guidance() == "" and b.as_dict()["places"] == []


def test_guidance_is_compact_and_says_where_not_what():
    text = brief("What are Apple's latest quarterly net sales?").guidance()
    assert text.startswith("PLANNING BRIEF") and "sec.gov" in text and "site: operator" in text
    assert len(text) < 2500


def test_ensure_sources_adds_a_site_query_only_when_the_plan_misses_the_best_place():
    q = "What are Apple's latest quarterly net sales?"
    scope = classify(q, NOW)
    b = planning.build(q, None, scope, NOW)
    out = planning.ensure_sources(["apple net sales"], b, "Apple", scope, NOW, 3)
    assert out[0] == "apple net sales" and "site:sec.gov" in out[-1] and "10-Q" in out[-1] and "2026" in out[-1]
    already = planning.ensure_sources(["apple site:sec.gov 10-Q"], b, "Apple", scope, NOW, 3)
    assert already == ["apple site:sec.gov 10-Q"]
    full = planning.ensure_sources(["a", "b", "c"], b, "Apple", scope, NOW, 3)
    assert len(full) == 3 and "site:sec.gov" in full[-1] and full[:2] == ["a", "b"]


def test_the_planner_sees_the_brief_and_the_plan_reaches_the_best_place(env):
    prompts = []

    def planner(p):
        prompts.append(p)
        return Plan(subject="Acme", queries=["acme quarterly sales"], checklist=["Acme net sales"])

    runner = make_runner(env, Plan=planner)
    first = runner.start("What are Acme's latest quarterly net sales?", require_approval=True)
    assert "PLANNING BRIEF" in prompts[0] and "10-Q" in prompts[0]
    ready = next(e["payload"] for e in env["runs"].events(first["run_id"]) if e["type"] == "plan_ready")
    assert any("site:sec.gov" in q for q in ready["queries"])
    assert ready["brief"]["places"][0]["site"] == "sec.gov"


def test_more_ways_to_say_latest_are_respected():
    for q in ("What is the ongoing status of the Acme trial?", "What does Acme charge at present?", "Who runs Acme presently?"):
        assert classify(q, NOW).recent, q
