"""Source profiles are data, the router is deterministic, and every case in evals/source_cases.json routes as written."""

import json

import pytest

import sources
from config import PROJECT_ROOT

CASES = json.loads((PROJECT_ROOT / "evals" / "source_cases.json").read_text(encoding="utf-8"))["cases"]
ACTIVE = ["openalex", "sec_edgar"]


def test_every_profile_is_complete_and_named_after_its_file():
    assert {"web", "sec_edgar", "openalex"} <= set(sources.profiles())
    for p in sources.profiles().values():
        assert p["covers"] and p["weak_at"] and p["query_style"]


def test_an_undescribed_connector_is_always_offered():
    assert sources.route(["brand_new"], "anything at all")["offered"] == ["brand_new"]


def test_hidden_connectors_say_why():
    r = sources.route(ACTIVE, "Who is the current CEO of Dropbox?")
    assert r["offered"] == [] and {h["id"] for h in r["hidden"]} == set(ACTIVE) and "nothing in the question" in r["hidden"][0]["reason"]


def test_the_checklist_can_bring_a_connector_in():
    assert sources.route(ACTIVE, "How is Dropbox doing?", "Annual revenue in the latest 10-K")["offered"] == ["sec_edgar"]


def test_planner_gets_search_advice():
    assert "weak at" in sources.search_advice()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_case_routes_as_expected(case):
    assert sorted(sources.route(ACTIVE, case["question"])["offered"]) == sorted(case["route"])


def test_case_file_has_the_full_ladder_for_each_source():
    for source in ("sec_edgar", "openalex", "web"):
        mine = [c for c in CASES if c["source"] == source]
        assert sorted(c["level"] for c in mine) == [1, 2, 3, 4, 5, 6, 7]
        assert sum(c["expect"]["kind"] == "not_established" for c in mine) >= 1
    assert len({c["id"] for c in CASES}) == len(CASES)
    for c in CASES:
        assert c["expect"]["kind"] in ("contains_any", "contains_all", "not_established", "time_sensitive") and c["check"]


# --- the runner uses the router -------------------------------------------------------------------------------

def test_a_connector_that_does_not_fit_the_question_is_hidden_and_costs_no_model_call(env):
    from runner import ApiPlan
    from tests.test_runner import make_runner
    from tests.test_runner_capability import activate, manifest

    asked = []
    m = manifest().model_copy(update={"connector_id": "sec_edgar"})
    activate(env["registry"], m)
    runner = make_runner(env, ApiPlan=lambda p: asked.append(p) or ApiPlan(calls=[]))
    runner.search_fn = lambda query, max_results=8, **extra: env["world"].search(query, max_results)
    result = runner.start("Who founded Acme?", budgets={"max_iterations": 1})
    routed = next(e["payload"] for e in env["runs"].events(result["run_id"]) if e["type"] == "sources_routed")
    assert routed["offered"] == [] and routed["hidden"][0]["id"] == "sec_edgar" and asked == []

    asked.clear()
    runner.start("What was Acme's revenue for fiscal 2024?", budgets={"max_iterations": 1})
    assert len(asked) == 1                                           # a question about financials does reach the connector


from tests.test_runner import env  # noqa: E402,F401
