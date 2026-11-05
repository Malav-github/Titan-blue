"""Verify first, act later: what a question takes for granted is established like any other claim, before the person is asked to
approve the plan, and the person can answer the agent's finding (accept it, overrule it, correct it, or give a source)."""

from runner import Extraction, ExtractedClaim, Plan, PremiseList
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


# --- headless runs (no approval step) ---

def test_a_verified_premise_lets_the_real_research_start(env):
    result = make_runner(env, **plan()).start("What does Acme charge on its price list?")
    assert result["status"] == "finished"
    kinds = types(env, result["run_id"])
    assert "premise_check" in kinds and "premise_checked" in kinds
    report = result["report"]
    assert report["outcome"] == "answered"
    assert report["premise"] == [{"text": PREMISE, "verified": True}]
    assert report["summary"] and any(r["premise"] for r in report["established"])   # premise evidence is shown, labelled


def test_an_unverified_premise_stops_before_any_real_research(env):
    result = make_runner(env, **plan(), Extraction=lambda p: nothing()).start("What did Acme say in its 2031 letter?")
    assert result["status"] == "finished"
    report = result["report"]
    assert report["outcome"] == "premise_unverified"
    assert report["summary"] == "" and report["established"] == []
    assert report["gaps"] == ["Starter plan price"] and report["premise"] == [{"text": PREMISE, "verified": False}]
    assert "premise_unverified" in types(env, result["run_id"])
    assert env["world"].queries == [PREMISE]          # only the premise was searched, never the real plan


def test_a_run_with_no_premise_goes_straight_to_the_research(env):
    result = make_runner(env).start("What does Acme charge?")
    assert "premise_check" not in types(env, result["run_id"])
    assert result["report"]["outcome"] == "answered" and result["report"]["premise"] == []


# --- the person answers the finding ---

def asked(runner, question="What did Acme say in 2031?"):
    first = runner.start(question, require_approval=True)
    assert first["status"] == "awaiting_approval"
    return first


def test_the_plan_is_ready_for_a_decision_before_anything_is_searched(env):
    """Three steps (quick look, plan, decision): the premise is listed and waits to be checked once the person approves."""
    runner = make_runner(env, **plan(), Extraction=lambda p: nothing())
    first = asked(runner)
    ask = first["approval_request"]
    assert ask["premise"]["status"] == "pending" and ask["premise"]["items"] == [PREMISE]
    assert ask["queries"] == ["acme pricing"] and ask["checklist"] == ["Starter plan price"]
    assert env["world"].queries == []                                  # nothing has been searched
    assert not {"premise_check", "search_done", "source_fetched"} & set(types(env, first["run_id"]))


def test_approving_checks_the_premise_first_and_asks_again_only_if_it_fails(env):
    runner = make_runner(env, **plan(), Extraction=lambda p: nothing())
    first = asked(runner)
    second = runner.resume(first["run_id"], {"approved": True})
    assert second["status"] == "awaiting_approval"
    ask = second["approval_request"]
    assert ask["premise"]["status"] == "unverified" and ask["premise"]["missing"] == [PREMISE]
    assert env["world"].queries == [PREMISE]                           # only the premise was searched, never the real plan


def test_a_premise_that_holds_goes_straight_on_without_asking_again(env):
    runner = make_runner(env, **plan())
    first = asked(runner)
    done = runner.resume(first["run_id"], {"approved": True})
    assert done["status"] == "finished" and done["report"]["outcome"] == "answered"


def test_approving_an_unverified_premise_accepts_the_refusal(env):
    runner = make_runner(env, **plan(), Extraction=lambda p: nothing())
    second = runner.resume(asked(runner)["run_id"], {"approved": True})
    done = runner.resume(second["run_id"], {"approved": True})
    assert done["status"] == "finished" and done["report"]["outcome"] == "premise_unverified"
    assert env["world"].queries == [PREMISE]


def test_the_person_can_overrule_the_refusal_and_research_anyway(env):
    runner = make_runner(env, **plan(), Extraction=lambda p: nothing())
    second = runner.resume(asked(runner)["run_id"], {"approved": True})
    done = runner.resume(second["run_id"], {"approved": True, "override": True})
    assert done["status"] == "finished"
    assert done["report"]["outcome"] == "not_found"                          # researched, and nothing was found
    assert any("went ahead at your request" in w for w in done["report"]["warnings"])
    assert done["report"]["premise"] == [{"text": PREMISE, "verified": False}]
    assert "acme pricing" in env["world"].queries


def test_a_comment_makes_the_agent_rebuild_its_plan_and_check_again(env):
    texts = []

    def planner(p):
        texts.append(p)
        corrected = "that should have been 2026" in p
        return Plan(subject="Acme", queries=["acme 2026 pricing"] if corrected else ["acme 2031 letter"], checklist=["Starter plan price"])

    def premise_of(p):
        texts.append(p)
        return PremiseList(premise=["Acme published a 2026 price list" if "that should have been 2026" in p else "Acme wrote a 2031 letter"])

    # the 2031 premise cannot be found, the corrected 2026 one can
    runner = make_runner(env, Plan=planner, PremiseList=premise_of, Extraction=lambda p: claim() if "2026" in p else nothing())
    first = asked(runner)
    assert first["approval_request"]["queries"] == ["acme 2031 letter"]

    second = runner.resume(first["run_id"], {"approved": False, "comment": "that should have been 2026"})
    assert second["status"] == "awaiting_approval"                           # a new plan, awaiting the person again
    ask = second["approval_request"]
    assert ask["queries"] == ["acme 2026 pricing"] and ask["premise"]["status"] == "pending"
    assert ask["corrections"] == ["that should have been 2026"]
    assert "plan_corrected" in types(env, first["run_id"])
    assert any("THE PERSON CORRECTED YOUR EARLIER READING" in t for t in texts)

    done = runner.resume(first["run_id"], {"approved": True})                # the corrected premise holds: no second question
    assert done["status"] == "finished" and done["report"]["outcome"] == "answered"
    assert done["report"]["premise"] == [{"text": "Acme published a 2026 price list", "verified": True}]


def test_a_comment_after_a_failed_check_also_rebuilds_the_plan(env):
    def planner(p):
        corrected = "that should have been 2026" in p
        return Plan(subject="Acme", queries=["acme 2026"] if corrected else ["acme 2031"], checklist=["Starter plan price"])

    runner = make_runner(env, Plan=planner, **{"PremiseList": lambda p: PremiseList(premise=["Acme wrote a letter"])},
                         Extraction=lambda p: claim() if "2026" in p else nothing())
    second = runner.resume(asked(runner)["run_id"], {"approved": True})
    assert second["approval_request"]["premise"]["status"] == "unverified"
    third = runner.resume(second["run_id"], {"approved": True, "comment": "that should have been 2026"})
    assert third["status"] == "awaiting_approval" and third["approval_request"]["queries"] == ["acme 2026"]


def test_a_source_the_person_gives_is_read_and_checked_like_any_other(env):
    given = {"source": False}      # the web results hold nothing; claims only appear once the person's source has been read
    runner = make_runner(env, **plan(), Extraction=lambda p: claim() if given["source"] else nothing())
    second = runner.resume(asked(runner)["run_id"], {"approved": True})
    assert second["approval_request"]["premise"]["status"] == "unverified"
    given["source"] = True
    done = runner.resume(second["run_id"], {"approved": True, "url": "https://user.example/letter"})
    assert done["status"] == "finished" and done["report"]["outcome"] == "answered"       # the source held, so the research went on
    assert "source_supplied" in types(env, second["run_id"])
    assert "https://user.example/letter" in env["world"].fetched


def test_rejecting_stops_the_run_without_a_report(env):
    runner = make_runner(env, **plan(), Extraction=lambda p: nothing())
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
