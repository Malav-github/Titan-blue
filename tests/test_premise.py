"""Verify first, act later. Before the person is asked to approve the plan, ONE quick search (top 10 results, one model call over
titles and snippets) screens what the question takes for granted. The person answers the finding: accept it, overrule it, correct
it, or hand over a source (which is then read and checked like any other evidence)."""

from runner import Plan, PremiseList, PremiseScreen
from tests.scripted import Extraction, ExtractedClaim
from tests.test_runner import GOOD_QUOTE, env, make_runner, types  # noqa: F401

PREMISE = "Acme publishes a price list"


def plan(**kw):
    return dict(Plan=lambda p: Plan(subject="Acme", queries=["acme pricing"], checklist=["Starter plan price"], **kw),
                PremiseList=lambda p: PremiseList(premise=[PREMISE]))


def claim():
    return Extraction(claims=[ExtractedClaim(predicate="pricing.starter", value="$29", claim="Acme's Starter plan costs $29 per month.",
                                             quote=GOOD_QUOTE, needed_fact=1)])


def nothing():
    return Extraction(claims=[])


def reported(*numbers):
    return lambda p: PremiseScreen(reported=list(numbers), reason="screened")


NOT_REPORTED = dict(PremiseScreen=reported())


# --- headless runs (no approval step) ---

def test_a_premise_reported_in_the_results_lets_the_research_start(env):
    result = make_runner(env, **plan()).start("What does Acme charge on its price list?")
    assert result["status"] == "finished"
    kinds = types(env, result["run_id"])
    assert "premise_screened" in kinds and "premise_check" not in kinds         # one quick screen, not a second research run
    report = result["report"]
    assert report["outcome"] == "answered"
    assert report["premise"] == [{"text": PREMISE, "verified": True, "basis": "search results report it (not read)"}]
    assert not any(r["premise"] for r in report["established"])                # the screen is not evidence: no claim carries it


def test_the_screen_is_one_search_over_the_top_ten_results(env):
    seen = []
    runner = make_runner(env, **plan())
    original = runner.search_fn
    runner.search_fn = lambda q, max_results=8, **kw: (seen.append((q, max_results)), original(q, max_results))[1]
    runner.start("What does Acme charge on its price list?")
    assert seen[0] == (PREMISE, 10) and [q for q, _ in seen].count(PREMISE) == 1


def test_a_premise_nothing_reports_stops_before_any_real_research(env):
    result = make_runner(env, **plan(), **NOT_REPORTED).start("What did Acme say in its 2031 letter?")
    assert result["status"] == "finished"
    report = result["report"]
    assert report["outcome"] == "premise_unverified"
    assert report["summary"] == "" and report["established"] == []
    assert report["gaps"] == ["Starter plan price"] and report["premise"][0]["verified"] is False
    assert "premise_unverified" in types(env, result["run_id"])
    assert env["world"].queries == [PREMISE]          # only the screen searched, never the real plan


def test_a_run_with_no_premise_goes_straight_to_the_research(env):
    result = make_runner(env).start("What does Acme charge?")
    assert "premise_screened" not in types(env, result["run_id"])
    assert result["report"]["outcome"] == "answered" and result["report"]["premise"] == []


def test_a_screen_that_could_not_run_never_refuses_the_question(env):
    runner = make_runner(env, **plan())
    original = runner.search_fn

    def flaky(q, max_results=8, **kw):
        if q == PREMISE:
            raise RuntimeError("search is down")
        return original(q, max_results)

    runner.search_fn = flaky
    result = runner.start("What does Acme charge on its price list?")
    assert result["status"] == "finished" and result["report"]["outcome"] == "answered"
    assert any("premise screen could not search" in w for w in result["report"]["warnings"])
    assert "premise_screen_failed" in types(env, result["run_id"])


def test_a_failed_model_call_leaves_the_premise_unjudged_not_refused(env):
    result = make_runner(env, **plan(), PremiseScreen=lambda p: RuntimeError("model down")).start("What does Acme charge?")
    assert result["report"]["outcome"] == "answered"
    assert any("premise was not judged" in w for w in result["report"]["warnings"])


def test_an_empty_judgement_from_the_model_is_not_a_refusal(env):
    """A small model sometimes returns nothing at all. That is no finding, so the question is researched, with a warning."""
    result = make_runner(env, **plan(), PremiseScreen=lambda p: PremiseScreen(reported=[], reason="")).start("What does Acme charge?")
    assert result["report"]["outcome"] == "answered"
    assert any("gave no judgement" in w for w in result["report"]["warnings"])


# --- the person answers the finding ---

def asked(runner, question="What did Acme say in 2031?"):
    first = runner.start(question, require_approval=True)
    assert first["status"] == "awaiting_approval"
    return first


def test_the_finding_arrives_with_the_plan_before_anything_real_is_searched(env):
    runner = make_runner(env, **plan(), **NOT_REPORTED)
    ask = asked(runner)["approval_request"]
    assert ask["premise"]["status"] == "unverified" and ask["premise"]["missing"] == [PREMISE]
    assert ask["premise"]["screen"]["query"] == PREMISE and len(ask["premise"]["screen"]["results"]) == 2
    assert ask["queries"] == ["acme pricing"] and ask["checklist"] == ["Starter plan price"]
    assert env["world"].queries == [PREMISE]          # the plan's own searches have not run


def test_a_reported_premise_shows_which_results_report_it(env):
    ask = asked(make_runner(env, **plan(), PremiseScreen=reported(2)))["approval_request"]
    screen = ask["premise"]["screen"]
    assert ask["premise"]["status"] == "reported" and screen["reported"] == [2]
    assert [r["reports_it"] for r in screen["results"]] == [False, True]


def test_the_screen_ignores_result_numbers_that_do_not_exist(env):
    ask = asked(make_runner(env, **plan(), PremiseScreen=reported(99)))["approval_request"]
    assert ask["premise"]["status"] == "unverified"


def test_approving_an_unverified_premise_accepts_the_refusal(env):
    runner = make_runner(env, **plan(), **NOT_REPORTED)
    first = asked(runner)
    done = runner.resume(first["run_id"], {"approved": True})
    assert done["status"] == "finished" and done["report"]["outcome"] == "premise_unverified"
    assert env["world"].queries == [PREMISE]


def test_the_person_can_overrule_the_refusal_and_research_anyway(env):
    runner = make_runner(env, **plan(), **NOT_REPORTED, Extraction=lambda p: nothing())
    first = asked(runner)
    done = runner.resume(first["run_id"], {"approved": True, "override": True})
    assert done["status"] == "finished"
    assert done["report"]["outcome"] == "not_found"                          # researched, and nothing was found
    assert any("went ahead at your request" in w for w in done["report"]["warnings"])
    assert done["report"]["premise"][0]["verified"] is False
    assert "acme pricing" in env["world"].queries


def test_a_comment_makes_the_agent_rebuild_its_plan_and_screen_again(env):
    texts = []

    def planner(p):
        texts.append(p)
        corrected = "that should have been 2026" in p
        return Plan(subject="Acme", queries=["acme 2026 pricing"] if corrected else ["acme 2031 letter"], checklist=["Starter plan price"])

    def premise_of(p):
        texts.append(p)
        return PremiseList(premise=["Acme published a 2026 price list" if "that should have been 2026" in p else "Acme wrote a 2031 letter"])

    # nothing reports the 2031 premise; the corrected 2026 one is reported
    runner = make_runner(env, Plan=planner, PremiseList=premise_of,
                         PremiseScreen=lambda p: PremiseScreen(reported=[1] if "2026" in p else [], reason="screened"))
    first = asked(runner)
    assert first["approval_request"]["premise"]["status"] == "unverified"

    second = runner.resume(first["run_id"], {"approved": True, "comment": "that should have been 2026"})
    assert second["status"] == "awaiting_approval"                           # a new plan and a new finding, awaiting the person again
    ask = second["approval_request"]
    assert ask["queries"] == ["acme 2026 pricing"] and ask["premise"]["status"] == "reported"
    assert ask["corrections"] == ["that should have been 2026"]
    assert "plan_corrected" in types(env, first["run_id"])
    assert any("THE PERSON CORRECTED YOUR EARLIER READING" in t for t in texts)

    done = runner.resume(first["run_id"], {"approved": True})
    assert done["status"] == "finished" and done["report"]["outcome"] == "answered"
    assert done["report"]["premise"][0]["text"] == "Acme published a 2026 price list" and done["report"]["premise"][0]["verified"]


def test_a_source_the_person_gives_is_read_and_checked_like_any_other(env):
    given = {"source": False}      # the web results hold nothing; claims only appear once the person's source has been read
    runner = make_runner(env, **plan(), **NOT_REPORTED, Extraction=lambda p: claim() if given["source"] else nothing())
    first = asked(runner)
    given["source"] = True
    second = runner.resume(first["run_id"], {"approved": True, "url": "https://user.example/letter"})
    assert second["status"] == "awaiting_approval" and second["approval_request"]["premise"]["status"] == "verified"
    assert "source_supplied" in types(env, first["run_id"])
    assert "https://user.example/letter" in env["world"].fetched


def test_rejecting_stops_the_run_without_a_report(env):
    runner = make_runner(env, **plan(), **NOT_REPORTED)
    first = asked(runner)
    assert runner.resume(first["run_id"], {"approved": False})["status"] == "stopped"


# --- a missing answer (no premise) asks for a source ---

def test_a_missing_answer_asks_for_a_source_then_says_it_was_not_found(env):
    runner = make_runner(env, Extraction=lambda p: nothing())
    first = runner.start("What does Acme charge?", ask_sources=True)
    assert first["status"] == "needs_source" and first["approval_request"]["stage"] == "answer"
    done = runner.resume(first["run_id"], {"decline": True})
    assert done["status"] == "finished" and done["report"]["outcome"] == "not_found"
    assert done["report"]["summary"] == ""
