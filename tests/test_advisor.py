"""The request advisor (skills/advisor): a conversation on an 'Add a data source' request that answers only from what the
request holds, offers what can be done next, and never states a fact without its quote."""

import re

import httpx

from capability import LookUp
from skills import advisor, sourcing
from tests.test_capability import APIS, World, handlers, manager, parts  # noqa: F401


def fact(field, value, quote, url="https://developers.epo.org/ops"):
    return {"field": field, "value": value, "quote": quote, "source_url": url, "official": True}


EPO = {"name": "EPO Open Patent Services", "connector_id": "epo_open_patent_services", "kind": "rest", "kind_label": "API",
       "docs_url": "https://developers.epo.org/", "cost_label": "free (verified)",
       "verified_facts": [fact("free_tier", "up to 4 GB a week", "Up to 4 GB of data per week"),
                          fact("pricing", "EUR 2 800 a year", "Annual subscription: EUR 2 800")],
       "fit": {"criteria": [{"id": "cost", "label": "Within budget", "verdict": "meets", "why": "free tier", "hard": True,
                             "quote": "Up to 4 GB of data per week", "source_url": "https://developers.epo.org/ops"},
                            {"id": "storage", "label": "Results may be kept", "verdict": "unknown", "why": "no quote", "hard": True}],
               "excluded": False, "exclusion_reasons": []},
       "cannot_integrate": "the provider uses OAuth client credentials, which cannot be connected yet"}
FINDIP = {"name": "FindIP", "connector_id": "findip", "kind": "rest", "kind_label": "API", "docs_url": "https://findip.example/docs",
          "cost_label": "paid", "verified_facts": [fact("pricing", "$49 a month", "Plans start at $49 per month", "https://findip.example/pricing")],
          "fit": {"criteria": [{"id": "cost", "label": "Within budget", "verdict": "fails", "why": "paid", "hard": True}],
                  "excluded": True, "exclusion_reasons": ["over budget"]}}
MCP = {"name": "Patent Search MCP", "connector_id": "patent_search_mcp", "kind": "mcp", "kind_label": "MCP server",
       "docs_url": "https://mcp.example/server", "verified_facts": [], "cannot_integrate": "MCP servers are not installed yet"}


def at_gate(*cands, choice="findip"):
    return {"request_id": "r1", "need": "patent family records", "status": "awaiting_user", "brief": sourcing.brief_from({"need": "patent family records"}),
            "events": [{"event": "recommended", "detail": {"choice": choice}, "at": ""}],
            "gate": {"candidates": list(cands), "recommendation": {"choice": choice}, "your_steps": ["Read the terms"], "note": ""}}


def failed(*cands, reason="no integrable candidate"):
    return {"request_id": "r2", "need": "patent family records", "status": "failed", "brief": None, "gate": None,
            "events": [{"event": "failed", "detail": {"reason": reason, "findings": list(cands)}, "at": ""}]}


def test_what_a_message_is_about_is_read_by_code():
    r = at_gate(EPO, FINDIP)
    assert advisor.intent("Is there a free alternative?", r) == "alternatives"          # not a question about cost
    assert advisor.intent("why did the test fail?", r) == "troubleshoot"
    assert advisor.intent("Does EPO allow individuals to sign up?", r) == "question"
    assert advisor.intent("what now", r) == "status"
    assert advisor.intent("hello", r) == "help"


def test_a_question_is_answered_only_with_quotes_for_the_named_candidate():
    out = advisor.reply(at_gate(EPO, FINDIP), "How much does EPO cost?")
    assert out["intent"] == "question" and {f["candidate"] for f in out["facts"]} == {"EPO Open Patent Services"}
    assert all(f["quote"] for f in out["facts"]) and "Annual subscription: EUR 2 800" in {f["quote"] for f in out["facts"]}
    assert out["actions"] == []                                                          # answered: nothing to look up


def test_a_question_the_request_cannot_answer_says_so_and_offers_a_look_up_on_the_providers_own_pages():
    out = advisor.reply(at_gate(EPO, FINDIP), "Can an individual register for EPO without a company?")
    assert out["facts"] == [] and "no quoted fact" in out["text"]
    look = out["actions"][0]
    assert look["id"] == "look_up" and look["params"]["candidate"] == "EPO Open Patent Services"
    assert look["params"]["docs_url"] == "https://developers.epo.org/"
    assert any("unknown, not false" in n for n in out["notes"])


def test_a_question_about_all_of_them_answers_each_from_its_own_quotes():
    out = advisor.reply(at_gate(EPO, FINDIP), "Is any of them free?")
    assert {f["candidate"] for f in out["facts"]} == {"EPO Open Patent Services", "FindIP"}


def test_alternatives_list_what_else_was_found_and_offer_a_new_search_without_them():
    out = advisor.reply(at_gate(EPO, FINDIP, MCP), "anything free instead?")
    assert out["intent"] == "alternatives"
    assert any(p.startswith("EPO Open Patent Services (API") for p in out["points"])
    assert any("MCP server" in p for p in out["points"]) and not any(p.startswith("FindIP") for p in out["points"])   # the recommendation is not an alternative to itself
    act = out["actions"][0]
    assert act["id"] == "alternatives" and set(act["params"]["exclude"]) == {"EPO Open Patent Services", "FindIP", "Patent Search MCP"}
    assert act["params"]["brief"]["budget"] == "free" and act["params"]["brief"]["budget_label"] == "Free only"


def test_a_new_brief_changes_only_what_the_message_asks_for():
    base = sourcing.brief_from({"need": "patent data", "budget": "up_to_50", "fields": "applicant"})
    cheaper = advisor.constraints("something cheaper", base)
    assert cheaper["budget"] == "up_to_10" and cheaper["fields"] == ["applicant"]
    assert advisor.constraints("any MCP servers?", base)["kind"] == "mcp"
    assert advisor.constraints("APIs only please", base)["kind"] == "api"
    assert advisor.constraints("other options", base)["budget"] == "up_to_50"


def test_a_failure_is_matched_to_its_known_cause_and_what_can_be_done():
    out = advisor.reply(failed(EPO, MCP), "why did it fail?")
    assert out["intent"] == "troubleshoot"
    assert any("OAuth" in p for p in out["points"])                                    # EPO: OAuth, a missing capability
    assert {a["id"] for a in out["actions"]} >= {"alternatives", "retry"}
    assert not any("MCP" in p for p in out["points"] if p.startswith("Connecting"))   # an MCP server is not a connection failure


def test_a_401_offers_a_retest_and_a_pydantic_missing_field_is_not_mistaken_for_a_missing_key():
    r = {"request_id": "r3", "need": "x y", "status": "tests_failed", "brief": None, "gate": None, "events": [
        {"event": "integrated", "detail": {}, "at": ""},
        {"event": "test_item", "detail": {"item": "Read-only smoke call succeeds", "status": "fail", "detail": "RuntimeError: HTTP 401: bad key"}, "at": ""}]}
    found = advisor.diagnose(r)
    assert found[0]["diagnosis"] == "unauthorized" and "retest" in found[0]["actions"]
    assert advisor.reply(r, "it's not working")["actions"][0]["id"] == "retest"
    draft = failed({**EPO, "cannot_integrate": "1 validation error for Manifest\nbase_url\n  Field required [type=missing]"})
    connecting = next(p for p in advisor.diagnose(draft) if p["where"].startswith("Connecting"))
    assert connecting["diagnosis"] == "draft_failed" and connecting["model_limit"]
    key = {**r, "events": [{"event": "integrated", "detail": {}, "at": ""},
                           {"event": "test_item", "detail": {"item": "API key present", "status": "fail", "detail": "RuntimeError: PATENTX_API_KEY missing"}, "at": ""}]}
    assert advisor.diagnose(key)[0]["diagnosis"] == "key_missing"


def test_only_the_latest_attempts_test_failures_count():
    r = {"request_id": "r4", "need": "x y", "status": "ready", "brief": None, "gate": None, "events": [
        {"event": "integrated", "detail": {}, "at": ""},
        {"event": "test_item", "detail": {"item": "smoke", "status": "fail", "detail": "HTTP 401"}, "at": ""},
        {"event": "retest", "detail": {}, "at": ""},
        {"event": "test_item", "detail": {"item": "smoke", "status": "pass", "detail": "HTTP 200"}, "at": ""}]}
    assert advisor.problems(r) == [] and "nothing to fix" in advisor.reply(r, "is something wrong?")["text"]


def test_an_action_is_only_offered_where_it_can_be_done():
    assert advisor.reply(at_gate(FINDIP), "why did it fail?")["actions"] == []       # nothing failed: the gate is waiting
    assert all(a["id"] != "retest" for a in advisor.reply(failed(EPO), "why did it fail?")["actions"])


def test_the_settings_compile_and_name_known_actions_only():
    s = advisor.settings()
    assert all(re.compile(g["match"]) for g in s["diagnoses"])
    assert {i["id"] for i in s["intents"]} == {"alternatives", "troubleshoot", "status"}
    assert set(advisor.ACTION_LABELS) == set(advisor.ACTION_ALLOWED)


# --- in the capability manager ---

def test_sources_already_looked_at_are_left_out_of_a_new_search(parts):
    m = manager(parts, World(APIS), handlers(APIS))
    brief = {**sourcing.brief_from({"need": "patent data"}), "exclude": ["PatentX"]}
    out = m.start("patent data", brief=brief)
    events = parts["registry"].get_request(out["request_id"])["events"]
    skipped = [e["detail"] for e in events if e["event"] == "candidate_skipped"]
    assert any(d["candidate"] == "PatentX" and "other than this one" in d["reason"] for d in skipped)
    found = next(e for e in events if e["event"] == "candidates_found")["detail"]["candidates"]
    assert "PatentX" not in found


def test_tests_can_be_run_again_after_a_fix_and_the_waiting_run_then_carries_on(parts, monkeypatch):
    monkeypatch.setenv("PATENTX_API_KEY", "secret-key-123456")
    resumed = []
    m = manager(parts, World(APIS), handlers(APIS), transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")),
                ready=lambda *a: resumed.append(a))
    rid = m.start("patent data", parent_run_id="run1")["request_id"]
    m.resume(rid, {"approve": "patentx", "terms_ok": True})
    assert parts["registry"].get_request(rid)["status"] == "tests_failed"
    m.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [{"number": "WO1"}]}))   # the key was fixed
    out = m.retest(rid)
    assert out["outcome"] == "integrated" and parts["registry"].get_request(rid)["status"] in ("ready", "done")
    assert parts["registry"].active("patentx") is not None and resumed and resumed[0][1] == "patentx"


def test_a_look_up_keeps_an_answer_only_if_its_quote_is_on_the_providers_page(parts):
    good = handlers(APIS, LookUp=lambda p: LookUp(found=True, passage=1, quote="You must create an account to get an API key.",
                                                  answer="An account is needed."))
    m = manager(parts, World(APIS), good)
    rid = parts["registry"].create_request(None, "patent data", "x")
    r = m.look_up(rid, "PatentX", "https://docs.patentx.example/", "Do I need an account?")
    assert r["found"] and r["source_url"] == "https://docs.patentx.example/" and "create an account" in r["quote"]
    made_up = handlers(APIS, LookUp=lambda p: LookUp(found=True, passage=1, quote="Individuals may register without a company.",
                                                     answer="Yes."))
    r = manager(parts, World(APIS), made_up).look_up(rid, "PatentX", "https://docs.patentx.example/", "Can individuals register?")
    assert not r["found"] and r["rejected"] == "the model's quote is not on the page"
    assert [e["event"] for e in parts["registry"].get_request(rid)["events"]].count("looked_up") == 2
