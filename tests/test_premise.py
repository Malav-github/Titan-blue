"""Verify first, act later: what a question takes for granted is established like any other claim before the research starts."""

from runner import Extraction, ExtractedClaim, Plan
from tests.test_runner import GOOD_QUOTE, env, make_runner, types  # noqa: F401

PREMISE = "Acme publishes a price list"


def plan(**kw):
    return lambda p: Plan(subject="Acme", queries=["acme pricing"], checklist=["Starter plan price"], premise=[PREMISE], **kw)


def claim():
    return Extraction(claims=[ExtractedClaim(predicate="pricing.starter", value="$29", claim="Acme's Starter plan costs $29 per month.",
                                             quote=GOOD_QUOTE, needed_fact=1)])


def test_a_verified_premise_lets_the_real_research_start(env):
    result = make_runner(env, Plan=plan()).start("What does Acme charge on its price list?")
    assert result["status"] == "finished"
    kinds = types(env, result["run_id"])
    assert "premise_check" in kinds and "premise_verified" in kinds
    report = result["report"]
    assert report["outcome"] == "answered"
    assert report["premise"] == [{"text": PREMISE, "verified": True}]
    assert report["summary"] and any(r["premise"] for r in report["established"])   # premise evidence is shown, labelled


def test_an_unverified_premise_stops_before_any_real_research(env):
    runner = make_runner(env, Plan=plan(), Extraction=lambda p: Extraction(claims=[]))
    result = runner.start("What did Acme say in its 2031 letter?")
    assert result["status"] == "finished"
    report = result["report"]
    assert report["outcome"] == "premise_unverified"
    assert report["summary"] == "" and report["established"] == []
    assert report["gaps"] == [PREMISE]
    kinds = types(env, result["run_id"])
    assert "premise_verified" not in kinds and "gate_stopped" in kinds
    assert env["world"].queries == [PREMISE]          # only the premise was searched, never the real plan


def test_a_run_with_no_premise_goes_straight_to_the_research(env):
    result = make_runner(env).start("What does Acme charge?")
    assert "premise_check" not in types(env, result["run_id"])
    assert result["report"]["outcome"] == "answered" and result["report"]["premise"] == []


def test_the_person_can_supply_a_source_which_is_checked_like_any_other(env):
    # claims come out only when the page is the person's source: the premise cannot be established from the web results
    given = {"source": False}      # the web results hold nothing; claims only appear once the person's source has been supplied
    runner = make_runner(env, Plan=plan(), Extraction=lambda p: claim() if given["source"] else Extraction(claims=[]))
    first = runner.start("What did Acme say?", ask_sources=True)
    assert first["status"] == "needs_source"
    ask = first["approval_request"]
    assert ask["type"] == "source_needed" and ask["stage"] == "premise" and ask["missing"] == [PREMISE]

    given["source"] = True
    second = runner.resume(first["run_id"], {"url": "https://user.example/letter"})
    kinds = types(env, first["run_id"])
    assert "source_supplied" in kinds and "premise_verified" in kinds        # the premise held once the source was read
    assert second["status"] == "finished" and second["report"]["outcome"] == "answered"


def test_a_missing_answer_also_asks_for_a_source_then_says_it_was_not_found(env):
    runner = make_runner(env, Extraction=lambda p: Extraction(claims=[]))          # no premise: the answer itself cannot be found
    first = runner.start("What does Acme charge?", ask_sources=True)
    assert first["status"] == "needs_source" and first["approval_request"]["stage"] == "answer"
    done = runner.resume(first["run_id"], {"decline": True})
    assert done["status"] == "finished" and done["report"]["outcome"] == "not_found"
    assert done["report"]["summary"] == ""


def test_declining_the_source_request_stops_with_the_premise_unverified(env):
    runner = make_runner(env, Plan=plan(), Extraction=lambda p: Extraction(claims=[]))
    first = runner.start("What did Acme say in 2031?", ask_sources=True)
    assert first["status"] == "needs_source"
    done = runner.resume(first["run_id"], {"decline": True})
    assert done["status"] == "finished" and done["report"]["outcome"] == "premise_unverified"
