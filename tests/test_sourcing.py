"""The sourcing skill (skills/sourcing) and the capability manager's search for data sources: a brief, WordBench's catalogue,
the APIs.guru directory and the official MCP Registry, and each candidate's fit to the brief, APIs and MCP servers apart."""

import json

import pytest

from skills import sourcing
from tests.test_capability import APIS, World, handlers, manager, parts  # noqa: F401

BRIEF = sourcing.brief_from({"need": "patent family records for European filings", "fields": "publication number\napplicant",
                             "budget": "up_to_50", "volume": 2000})


def test_a_brief_from_the_form():
    b = sourcing.brief_from({"need": "  patent  data ", "fields": "a\n- b; c", "budget": "free", "kind": "mcp", "volume": "300",
                             "commercial_use": True})
    assert b["need"] == "patent data" and b["fields"] == ["a", "b", "c"] and b["budget_monthly_usd"] == 0 and b["kind"] == "mcp"
    assert b["volume"] == 300 and b["commercial_use"] and not b["identity_ok"]
    assert sourcing.brief_from({"need": "x", "budget": "nonsense"})["budget"] == "any"


def test_the_catalogue_offers_the_providers_whose_topics_match():
    names = [c["name"] for c in sourcing.from_catalogue(BRIEF)]
    assert names[0] == "EPO Open Patent Services" and "OpenAlex" not in names


APIS_GURU = {
    "patentx.example": {"preferred": "1.0", "versions": {"1.0": {
        "info": {"title": "PatentX", "description": "Search patent families, applicants and publication numbers.",
                 "x-apisguru-categories": ["open_data"], "x-origin": [{"url": "https://docs.patentx.example/openapi.json"}]},
        "swaggerUrl": "https://docs.patentx.example/openapi.json", "externalDocs": {"url": "https://docs.patentx.example/"}}}},
    "weather.example": {"preferred": "2", "versions": {"2": {"info": {"title": "Weather", "description": "Forecasts."},
                                                             "swaggerUrl": "https://w.example/spec.json"}}},
}
MCP_REGISTRY = {"servers": [
    {"server": {"name": "io.example/patent-search", "title": "Patent Search MCP", "description": "Patent family search for applicants.",
                "version": "1.2.0", "websiteUrl": "https://mcp.example/server",
                "remotes": [{"type": "streamable-http", "url": "https://mcp.example/mcp",
                             "headers": [{"name": "Authorization", "isRequired": True, "isSecret": True, "description": "Bearer token"}]}]},
     "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active", "isLatest": True}}},
    {"server": {"name": "io.example/old-patents", "title": "Old patents", "description": "Patent applicants, family records."},
     "_meta": {"io.modelcontextprotocol.registry/official": {"status": "deprecated"}}},
]}


def test_directories_give_leads_with_their_kind_and_what_the_directory_says():
    [api] = sourcing.from_apis_guru(APIS_GURU, BRIEF)
    assert api["kind"] == "rest" and api["found_in"] == "APIs.guru" and api["spec_url"].endswith("openapi.json")
    assert api["docs_url"] == "https://docs.patentx.example/"
    [mcp] = sourcing.from_mcp_registry([MCP_REGISTRY], BRIEF)             # the deprecated one is left out
    assert mcp["kind"] == "mcp" and mcp["found_in"] == "MCP Registry" and mcp["docs_url"] == "https://mcp.example/server"
    assert mcp["registry"]["transport"] == "remote" and mcp["registry"]["needs_key"] and mcp["registry"]["key_note"] == "Bearer token"
    assert sourcing.kind_label("rest") == "API" and sourcing.kind_label("mcp") == "MCP server"


def test_words_match_singular_and_plural_but_not_by_prefix():
    assert sourcing.overlap(["patents", "families"], "Patent family search") == ["patents", "families"]
    assert sourcing.overlap(["applicant", "publication"], "application programming for public data") == []   # was 9% of all APIs


def test_a_lead_must_name_the_needs_subject_or_enough_of_its_words():
    def server(name, desc):
        return {"server": {"name": f"io.x/{name}", "title": name, "description": desc, "remotes": [{"type": "sse", "url": "https://x"}]}}
    brief = sourcing.brief_from({"need": "patent family records for European filings", "fields": "publication number\napplicant"})
    found = sourcing.from_mcp_registry([{"servers": [
        server("FindIP", "Semantic patent search and analysis."),                  # names the subject: in
        server("LovedOne Family", "Family members and photos."),                    # 'family' and 'members' only: out
        server("NY LLC filings", "Publication of New York LLC filings."),           # 'filings' and 'publication': out
    ]}], brief)
    assert [c["name"] for c in found] == ["FindIP"]
    assert sourcing.topic(brief) == ["patent", "family"] and sourcing.mcp_queries(brief) == ["patent", "family"]


@pytest.mark.parametrize("text, price", [("$49 per month", 49), ("from $300 a year", 25), ("$1,200/month or $99/mo", 99), ("free", None)])
def test_a_monthly_price_is_read_from_a_quoted_pricing_statement(text, price):
    assert sourcing.monthly_price(text) == price


@pytest.mark.parametrize("text, per_day", [("10 requests per second", 864_000), ("1,000 calls a day", 1000), ("5k requests per month", 5000 / 30)])
def test_a_daily_allowance_is_read_from_a_quoted_limit(text, per_day):
    assert sourcing.calls_per_day(text) == pytest.approx(per_day)


def fact(field, value, quote="q"):
    return {"field": field, "value": value, "quote": quote, "source_url": "https://docs.x/"}


def verdicts(f):
    return {c["id"]: c["verdict"] for c in f["criteria"]}


def test_fit_rules_out_what_breaks_a_hard_requirement_and_says_why():
    api = {"name": "X", "kind": "rest", "found_in": "APIs.guru"}
    draft = {"manifest": {"base_url": "https://api.x", "tier": "secondary"}, "is_free": False, "cost_label": "paid"}
    over = sourcing.fit(BRIEF, api, [fact("pricing", "$99 per month", "It costs $99 per month.")], draft, "publication number applicant")
    assert verdicts(over)["cost"] == "fails" and over["excluded"] and "over Up to $50 a month" in over["exclusion_reasons"][0]
    ok = sourcing.fit(BRIEF, api, [fact("pricing", "$29 per month", "It costs $29 per month."), fact("terms_storage", "yes"),
                                   fact("rate_limit", "10 requests per second")], draft, "publication number applicant")
    v = verdicts(ok)
    assert v["cost"] == "meets" and v["storage"] == "meets" and v["volume"] == "meets" and v["coverage"] == "meets" and not ok["excluded"]
    no_store = sourcing.fit(BRIEF, api, [fact("terms_storage", "no")], draft, "")
    assert no_store["excluded"] and verdicts(no_store)["storage"] == "fails"
    free_only = sourcing.brief_from({"need": "patent data", "budget": "free"})
    assert sourcing.fit(free_only, api, [fact("pricing", "$29 per month")], draft, "")["excluded"]
    id_needed = sourcing.fit(BRIEF, api, [fact("identity", "yes")], draft, "")
    assert verdicts(id_needed)["identity"] == "fails" and id_needed["excluded"]
    mcp = sourcing.fit(BRIEF, {"name": "M", "kind": "mcp", "found_in": "MCP Registry"}, [], {"error": "x", "kind": "mcp"}, "patent")
    assert verdicts(mcp)["installable"] == "fails" and not mcp["excluded"]            # not installable yet is not a ruling-out
    api_only = sourcing.brief_from({"need": "patent data", "kind": "api"})
    assert verdicts(sourcing.fit(api_only, {"name": "M", "kind": "mcp"}, [], None, ""))["kind"] == "fails"


def test_an_mcp_server_with_quoted_pricing_is_paid_and_ruled_out_of_a_free_brief():
    free = sourcing.brief_from({"need": "patent data", "budget": "free"})
    mcp = {"name": "Vibe", "kind": "mcp", "found_in": "MCP Registry"}
    f = sourcing.fit(free, mcp, [fact("pricing", "$250 for attorney review"), fact("payment_required", "Yes")], {"error": "x", "kind": "mcp"}, "patent")
    assert verdicts(f)["cost"] == "fails" and f["excluded"]
    g = sourcing.fit(free, mcp, [fact("free_tier", "free tier available")], {"error": "x", "kind": "mcp"}, "patent")
    assert verdicts(g)["cost"] == "meets"


def test_unknown_is_unknown_never_a_pass():
    v = verdicts(sourcing.fit(BRIEF, {"name": "X", "kind": "rest"}, [], {"manifest": {"base_url": "https://a"}, "is_free": None,
                                                                         "cost_label": "cost unknown"}, "publication number"))
    assert v["cost"] == v["storage"] == v["identity"] == v["volume"] == "unknown"


# --- in the capability manager ---

def directories(listing=APIS_GURU, mcp=MCP_REGISTRY):
    return lambda url: listing if "apis.guru" in url else mcp


def test_the_manager_finds_apis_and_mcp_servers_in_the_directories_and_scores_them_against_the_brief(parts):
    world = World(APIS)
    world.pages["https://mcp.example/server"] = (b"<html><body><article><p>" + b"Patent Search MCP gives patent family records. " * 10
                                                 + b"</p></article></body></html>", "text/html")
    m = manager(parts, world, handlers(APIS))
    m.directory_fn, m.discovery = directories(), ("directories",)
    out = m.start("patent family records for European filings", brief=BRIEF)
    assert out["status"] == "awaiting_user"
    gate = out["gate"]
    by = {c["name"]: c for c in gate["candidates"]}
    assert set(by) == {"PatentX", "Patent Search MCP"}
    assert by["PatentX"]["kind_label"] == "API" and by["Patent Search MCP"]["kind_label"] == "MCP server"
    assert by["PatentX"]["found_in"] == "APIs.guru" and by["PatentX"]["fit"]["criteria"]
    assert by["Patent Search MCP"]["registry"]["transport"] == "remote" and "not installed yet" in by["Patent Search MCP"]["cannot_integrate"]
    assert gate["recommendation"]["choice"] == "patentx" and gate["recommendation"]["best_mcp"] == "patent_search_mcp"
    assert gate["brief"]["budget_label"] == "Up to $50 a month" and {c["id"] for c in gate["criteria"]} >= {"cost", "storage", "installable"}
    found = next(e for e in parts["registry"].get_request(out["request_id"])["events"] if e["event"] == "candidates_found")
    assert found["detail"]["by_kind"] == {"API": ["PatentX"], "MCP server": ["Patent Search MCP"]}


def test_an_mcp_only_brief_ends_with_its_findings_kept_and_labelled(parts):
    world = World(APIS)
    m = manager(parts, world, handlers(APIS))
    m.directory_fn, m.discovery = directories(), ("directories",)
    out = m.start("patent family records", brief=sourcing.brief_from({"need": "patent family records", "kind": "mcp"}))
    assert out["status"] == "failed"
    failed = next(e for e in parts["registry"].get_request(out["request_id"])["events"] if e["event"] == "failed")
    assert [c["kind_label"] for c in failed["detail"]["findings"]] == ["MCP server"] and failed["detail"]["best_mcp"] == "patent_search_mcp"


def test_a_candidate_whose_host_does_not_resolve_is_skipped_not_the_whole_request(parts, monkeypatch):
    import capability
    import socket

    def check(url):
        if "mcp.example" in url:
            raise socket.gaierror(11001, "getaddrinfo failed")
    monkeypatch.setattr(capability, "check_public_url", check)
    m = manager(parts, World(APIS), handlers(APIS))
    m.directory_fn, m.discovery = directories(), ("directories",)
    out = m.start("patent family records", brief=BRIEF)
    events = parts["registry"].get_request(out["request_id"])["events"]
    skipped = next(e for e in events if e["event"] == "candidate_skipped")
    assert skipped["detail"]["candidate"] == "Patent Search MCP" and out["status"] == "awaiting_user"


def test_a_directory_that_cannot_be_read_is_logged_and_the_search_carries_on(parts):
    def broken(url):
        raise ConnectionError("down")
    m = manager(parts, World(APIS), handlers(APIS))
    m.directory_fn, m.discovery = broken, ("directories", "search")
    out = m.start("patent family records", brief=BRIEF)
    events = [e["event"] for e in parts["registry"].get_request(out["request_id"])["events"]]
    assert "directory_failed" in events and out["status"] == "awaiting_user"                # the web search still found PatentX


def test_the_ontology_catalogue_and_settings_load_and_agree():
    assert {k["id"] for k in sourcing.ontology()["kinds"]} == {"api", "mcp"}
    assert all(p["docs_url"].startswith("https://") for p in sourcing.catalogue())
    assert set(sourcing.settings()["budget_options"]) == {"free", "up_to_10", "up_to_50", "up_to_200", "up_to_1000", "any"}
    tpl = json.loads((sourcing.SETTINGS.parent.parent / "templates" / "add-data-source.json").read_text(encoding="utf-8"))
    assert tpl["inputs"]["properties"]["budget"]["enum"] == list(sourcing.settings()["budget_options"])
