"""The runner pausing for a data source it lacks, and using the connector once it exists."""

import json

from connectors.caller import ConnectorError
from connectors.manifest import Manifest
from runner import ApiCall, ApiPlan, Plan, Runner, Selection
from tests.test_runner import (GOOD_QUOTE, PAGE_TEXT, ScriptedLLM, World, default_handlers, env, make_runner,  # noqa: F401
                               types)
from tools.archive import archive_bytes, utc_now


def blocked_world(env):
    """Every page is refused (403), as with sites that block bots."""
    env["world"].fetch = lambda url, max_chars=2000: {**World().fetch(url), "status": 403}


def api_result(text):
    sha, path = archive_bytes(json.dumps({"results": [{"summary": text}]}).encode(), ".json")
    url = "https://api.patentx.example/v1/patents?q=acme"
    return {"ok": True, "url": url, "final_url": url, "status": 200, "content_type": "application/json", "encoding": "utf-8",
            "method": "api", "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": text, "source_tier": "regulatory"}


def planner_needing_data(**kw):
    return dict(Plan=lambda p: Plan(subject="Acme", queries=["acme pricing"], checklist=["Starter plan price"],
                                    needs_data_source="structured pricing records"), **kw)


def manifest():
    return Manifest.model_validate({
        "connector_id": "patentx", "name": "PatentX", "description": "pricing records", "topics": ["pricing"],
        "base_url": "https://api.patentx.example/v1",
        "endpoints": [{"name": "search", "path": "/patents", "description": "Search", "params": [{"name": "q", "in": "query"}]}],
        "smoke_test": {"endpoint": "search", "params": {}}})


def activate(registry, m):
    v = registry.propose(m)
    for event in ("approved", "tested_pass"):
        registry.event(m.connector_id, v, event, {}, "test")
    registry.activate(m.connector_id, v)


def test_capability_requests_are_off_by_default(env):
    blocked_world(env)
    result = make_runner(env, **planner_needing_data()).start("q", budgets={"max_iterations": 1})
    assert result["status"] == "finished" and result["capability_request"] is None


def test_run_pauses_for_a_missing_data_source_and_records_the_request(env):
    blocked_world(env)
    result = make_runner(env, **planner_needing_data()).start("What does Acme charge?", allow_capabilities=True)
    assert result["status"] == "needs_capability"
    request = result["capability_request"]
    assert request["need"] == "structured pricing records" and "unusable" in request["reason"]
    stored = env["registry"].get_request(request["request_id"])
    assert stored["run_id"] == result["run_id"] and stored["status"] == "requested"
    assert "capability_needed" in types(env, result["run_id"]) and "run_finished" not in types(env, result["run_id"])


def test_mostly_failed_fetches_also_trigger_the_request(env):
    blocked_world(env)

    def many(query, max_results=8):
        return {"search_id": "s", "providers": [], "warnings": [], "results": [
            {"url": f"https://site{i}.example/a", "title": "t", "snippet": "s", "score": 0.9 - i / 10} for i in range(4)]}

    runner = Runner(store=env["store"], runs=env["runs"], checkpoint_path=env["ckpt"], registry=env["registry"],
                    llm_factory=lambda task: ScriptedLLM(default_handlers(Selection=lambda p: Selection(numbers=[1, 2, 3, 4]))),
                    search_fn=many, fetch_fn=env["world"].fetch)
    assert runner.start("q", allow_capabilities=True)["status"] == "needs_capability"


def test_after_the_capability_arrives_the_run_uses_it_and_finishes(env):
    blocked_world(env)
    calls = []

    def fake_call(registry, connector, endpoint, params, run_id=None):
        calls.append((connector, endpoint, params, run_id))
        return api_result(PAGE_TEXT)

    runner = Runner(store=env["store"], runs=env["runs"], checkpoint_path=env["ckpt"], registry=env["registry"],
                    call_api_fn=fake_call, search_fn=env["world"].search, fetch_fn=env["world"].fetch,
                    llm_factory=lambda task: ScriptedLLM(default_handlers(
                        ApiPlan=lambda p: ApiPlan(calls=[ApiCall(connector="patentx", endpoint="search", params={"q": "acme"})]),
                        **planner_needing_data())))
    paused = runner.start("What does Acme charge?", allow_capabilities=True, budgets={"max_iterations": 1})
    assert paused["status"] == "needs_capability" and not calls              # no connector yet, so nothing was called

    activate(env["registry"], manifest())                                     # what the capability manager does

    done = runner.resume(paused["run_id"], {"capability": "patentx"})
    assert done["status"] == "finished" and calls and calls[0][3] == paused["run_id"]
    [row] = done["report"]["established"]
    assert row["quote"] == GOOD_QUOTE and row["source_url"].startswith("https://api.patentx.example")
    called = [e for e in env["runs"].events(paused["run_id"]) if e["type"] == "api_called"]
    assert called and called[0]["payload"]["source_tier"] == "regulatory"
    assert done["report"]["budget_used"]["iterations"] == 2                    # the new source earned one more round


def test_declining_the_capability_still_ends_with_an_honest_report(env):
    blocked_world(env)
    runner = make_runner(env, **planner_needing_data())
    paused = runner.start("q", allow_capabilities=True, budgets={"max_iterations": 1})
    done = runner.resume(paused["run_id"], {"skip": True})
    assert done["status"] == "finished" and done["report"]["gaps"] == ["Starter plan price"]


def test_a_refused_connector_call_is_logged_not_fatal(env):
    def refuse(*a, **k):
        raise ConnectorError("no active connector 'ghost'")

    activate(env["registry"], manifest())   # api_lookup only runs when some connector is active
    runner = Runner(store=env["store"], runs=env["runs"], checkpoint_path=env["ckpt"], registry=env["registry"],
                    call_api_fn=refuse, search_fn=env["world"].search, fetch_fn=env["world"].fetch,
                    llm_factory=lambda task: ScriptedLLM(default_handlers(
                        ApiPlan=lambda p: ApiPlan(calls=[ApiCall(connector="patentx", endpoint="search")]))))
    result = runner.start("q", budgets={"max_iterations": 1})
    assert result["status"] == "finished" and "api_failed" in types(env, result["run_id"])


# --- identifiers the model was never given are never used ---

def test_invented_identifiers_are_found_and_real_ones_are_not():
    from runner import invented_values
    q = "What is the fiscal year end of the company with SEC CIK 0000320193? Paper published in 2017."
    assert invented_values({"cik": "0000320193"}, q) == []                  # given in the question
    assert invented_values({"cik": "320193"}, q) == []                      # leading zeros do not matter
    assert invented_values({"cik": "0000096660"}, q) == ["0000096660"]      # a made-up CIK (seen in a real run)
    assert invented_values({"id": "W123456789"}, q) == ["W123456789"]       # a made-up OpenAlex id (seen in a real run)
    assert invented_values({"q": "Dropbox", "entity_type": "organization"}, q) == []     # words are not identifiers
    assert invented_values({"year": 2017}, q) == [] and invented_values({"year": 2021}, q) == ["2021"]
    assert invented_values(None, q) == [] and invented_values({"cik": "0000096660"}, "see also 96660") == []


def test_a_call_with_a_made_up_id_is_skipped_and_not_counted(env):
    calls = []

    def fake_call(registry, connector, endpoint, params, run_id=None):
        calls.append(params)
        return api_result(PAGE_TEXT)

    activate(env["registry"], manifest())
    runner = Runner(store=env["store"], runs=env["runs"], checkpoint_path=env["ckpt"], registry=env["registry"],
                    call_api_fn=fake_call, search_fn=env["world"].search, fetch_fn=env["world"].fetch,
                    llm_factory=lambda task: ScriptedLLM(default_handlers(
                        ApiPlan=lambda p: ApiPlan(calls=[ApiCall(connector="patentx", endpoint="search", params={"q": "A-99871"}),
                                                         ApiCall(connector="patentx", endpoint="search", params={"q": "acme"})]))))
    result = runner.start("What does Acme charge?", budgets={"max_iterations": 1})
    assert calls == [{"q": "acme"}]                                          # the guessed id never reached the connector
    skipped = [e for e in env["runs"].events(result["run_id"]) if e["type"] == "api_skipped"]
    assert len(skipped) == 1 and "A-99871" in skipped[0]["payload"]["reason"]
    assert result["status"] == "finished"


def test_an_all_zero_identifier_is_never_accepted():
    from runner import invented_values
    assert invented_values({"cik": "0000000000"}, "What is the CIK 0000320193 company?") == ["0000000000"]   # seen in a real run
    assert invented_values({"cik": "0"}, "anything") == ["0"]
    assert invented_values({"cik": "0000320193"}, "CIK 0000320193") == []


def test_the_plan_prompt_forbids_answers_and_process_steps_in_the_checklist(env):
    seen = []

    def plan(prompt):
        seen.append(prompt)
        return Plan(subject="Acme", queries=["acme pricing"], checklist=["Starter plan price"])

    make_runner(env, Plan=plan).start("What does Acme charge?", budgets={"max_iterations": 1})
    text = seen[0]
    assert "Never write the answer or any value" in text and "Never add steps such as verifying" in text
    assert "at most 6" in text and "3 to 6" not in text        # no longer forced to pad a one-fact question
