import json

import httpx
import pytest

import capability
from capability import (Candidates, Cand, DraftEndpoint, DraftManifest, Fact, FactList, CapabilityManager, Queries,
                        Recommendation, slug)
from connectors.registry import ConnectorRegistry
from evidence import EvidenceStore
from runs import RunStore
from support import SupportVerdict
from tools.archive import archive_bytes, utc_now


class ScriptedLLM:
    def __init__(self, handlers):
        self.handlers = handlers

    def with_structured_output(self, schema):
        handler = self.handlers[schema.__name__]

        class Call:
            def invoke(self_, prompt):
                out = handler(prompt)
                if isinstance(out, Exception):
                    raise out
                return out
        return Call()


FREE_TEXT = ("The {n} API is free for up to 1,000 requests per day. You must create an account to get an API key. "
             "No payment details are required. ")
PAID_TEXT = "The {n} API costs $99 per month. A payment card is required to get an API key. "
FILLER = "This documentation describes the interface in detail for developers building integrations. " * 6


def html(text: str) -> bytes:
    return f"<html><body><article><p>{text}{FILLER}</p></article></body></html>".encode()


def spec(host: str) -> bytes:
    doc = {"openapi": "3.0.1", "servers": [{"url": f"https://api.{host}/v1"}],
           "components": {"securitySchemes": {"k": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}},
           "paths": {"/patents": {"get": {"operationId": "searchPatents", "summary": "Search",
                                          "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}]}}}}
    return json.dumps(doc).encode()


class World:
    def __init__(self, apis, with_spec=True):
        self.pages = {}
        self.results = []
        for name, host, free in apis:
            text = (FREE_TEXT if free else PAID_TEXT).format(n=name)
            self.pages[f"https://docs.{host}/"] = (html(text), "text/html")
            if with_spec:
                self.pages[f"https://docs.{host}/openapi.json"] = (spec(host), "application/json")
            self.results.append({"url": f"https://docs.{host}/", "title": f"{name} API docs", "snippet": "docs", "score": 0.9})
        self.results.append({"url": "https://mcp.example/server", "title": "Some MCP server", "snippet": "mcp", "score": 0.5})

    def search(self, query, max_results=8):
        return {"results": self.results, "providers": [], "warnings": [], "search_id": "s"}

    def fetch(self, url, max_chars=2000):
        base = {"url": url, "final_url": url, "encoding": "utf-8", "method": "http", "fetched_at": utc_now(),
                "text": "", "truncated": False}
        if url not in self.pages:
            return {**base, "status": 404, "content_type": "text/html", "sha256": "", "archive_path": ""}
        body, ctype = self.pages[url]
        sha, path = archive_bytes(body, ".json" if "json" in ctype else ".html")
        return {**base, "status": 200, "content_type": ctype, "sha256": sha, "archive_path": path}


def handlers(world_apis, recommend=None, **override):
    def facts(prompt):
        name = next(n for n, _, _ in world_apis if f"API: {n}" in prompt)
        free = next(f for n, _, f in world_apis if n == name)
        text = (FREE_TEXT if free else PAID_TEXT).format(n=name)
        out = []
        if free:
            out += [Fact(field="free_tier", value="1,000 requests per day", quote=f"The {name} API is free for up to 1,000 requests per day.", passage=1),
                    Fact(field="payment_required", value="no", quote="No payment details are required.", passage=1)]
        else:
            out += [Fact(field="pricing", value="$99 per month", quote=f"The {name} API costs $99 per month.", passage=1),
                    Fact(field="payment_required", value="yes", quote="A payment card is required to get an API key.", passage=1)]
        out.append(Fact(field="signup", value="create an account", quote="You must create an account to get an API key.", passage=1)) if free else None
        out.append(Fact(field="rate_limit", value="10/second", quote="Rate limit is 10 requests per second per key.", passage=1))  # invented
        return FactList(facts=out)

    h = {
        "Queries": lambda p: Queries(queries=["patent data API documentation"]),
        "Candidates": lambda p: Candidates(candidates=[
            *[Cand(name=n, docs_url=f"https://docs.{host}/", reason=f"{n} covers patents") for n, host, _ in world_apis],
            Cand(name="Some MCP", docs_url="https://mcp.example/server", kind="mcp", reason="an MCP server"),
            Cand(name="Invented", docs_url="https://not-in-results.example/", reason="hallucinated url")]),
        "FactList": facts,
        "SupportVerdict": lambda p: SupportVerdict(verdict="supported", reasoning="the quote states it"),
        "Recommendation": lambda p: Recommendation(choice=recommend or slug(world_apis[0][0]), reasoning="best fit"),
    }
    h.update(override)
    return h


@pytest.fixture
def parts(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(capability, "check_public_url", lambda url: None)
    monkeypatch.setattr("runner.tracing.callbacks", lambda: [], raising=False)
    monkeypatch.delenv("PATENTX_API_KEY", raising=False)
    monkeypatch.delenv("PAIDCO_API_KEY", raising=False)
    from connectors import caller
    monkeypatch.setattr(caller, "dotenv_values", lambda p: {})
    return {"registry": ConnectorRegistry(tmp_path / "c.db"), "runs": RunStore(str(tmp_path / "r.db")),
            "store": EvidenceStore(tmp_path / "e.db"), "ckpt": str(tmp_path / "ck.db")}


def manager(parts, world, hs, transport=None, ready=None):
    return CapabilityManager(
        registry=parts["registry"], runs=parts["runs"], store=parts["store"], checkpoint_path=parts["ckpt"],
        llm_factory=lambda task: ScriptedLLM(hs), search_fn=world.search, fetch_fn=world.fetch,
        transport=transport or httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [{"number": "WO1"}]})),
        on_ready=ready)


APIS = [("PatentX", "patentx.example", True)]


def test_gate_shows_only_quote_verified_facts_and_never_invented_urls_or_mcp(parts):
    world = World(APIS)
    result = manager(parts, world, handlers(APIS)).start("patent data", parent_run_id="run1")
    assert result["status"] == "awaiting_user"
    gate = result["gate"]
    names = {c["name"]: c for c in gate["candidates"]}
    assert set(names) == {"PatentX", "Some MCP"}                      # the hallucinated URL candidate was dropped
    px = names["PatentX"]
    assert {e["field"] for e in px["verified_facts"]} == {"free_tier", "payment_required", "signup"}
    assert "rate_limit" in px["unverified"]                           # the invented quote never made it in
    assert all(e["quote"] and e["source_url"] for e in px["verified_facts"])
    assert px["is_free"] is True and px["integration_plan"]["key_env"] == "PATENTX_API_KEY"
    assert px["integration_plan"]["host"] == "api.patentx.example"
    assert "MCP servers are not integrated" in names["Some MCP"]["cannot_integrate"]
    assert any("PATENTX_API_KEY" in step for step in gate["your_steps"])


def test_gate_loops_until_key_and_terms_are_confirmed_then_integrates_and_resumes_parent(parts, monkeypatch):
    world, resumed = World(APIS), []
    m = manager(parts, world, handlers(APIS), ready=lambda run, cid, req: (resumed.append((run, cid)), {"status": "finished"})[1])
    first = m.start("patent data", parent_run_id="run1")
    rid = first["request_id"]

    no_key = m.resume(rid, {"approve": "patentx", "terms_ok": True})
    assert no_key["status"] == "awaiting_user" and "PATENTX_API_KEY is not set" in no_key["gate"]["note"]
    assert parts["registry"].active("patentx") is None and not resumed

    monkeypatch.setenv("PATENTX_API_KEY", "secret-key-123456")
    no_terms = m.resume(rid, {"approve": "patentx"})
    assert "terms_ok" in no_terms["gate"]["note"]

    done = m.resume(rid, {"approve": "patentx", "terms_ok": True})
    assert done["outcome"] == "integrated" and all(t["status"] == "pass" for t in done["tests"])
    assert [t["item"] for t in done["tests"]][-1] == "Connector is active and visible to the runner"
    assert parts["registry"].active("patentx")[0] == 1
    assert resumed == [("run1", "patentx")]
    assert parts["registry"].get_request(rid)["status"] == "done"
    events = [e["type"] for e in parts["runs"].events("run1")]
    assert "capability_awaiting_user" in events and "capability_test_item" in events and "capability_ready" in events
    approved = [e for e in parts["registry"].events("patentx") if e["event"] == "approved"]
    assert approved and approved[0]["by"] == "user"
    assert "secret-key" not in json.dumps(parts["registry"].events("patentx"))


def test_cancel_integrates_nothing(parts):
    m = manager(parts, World(APIS), handlers(APIS))
    rid = m.start("patent data")["request_id"]
    out = m.resume(rid, {"approve": None})
    assert parts["registry"].get_request(rid)["status"] == "cancelled" and parts["registry"].list_active() == []
    assert out["outcome"] == "cancelled by user"


def test_failing_smoke_test_blocks_activation_and_parent_stays_paused(parts, monkeypatch):
    monkeypatch.setenv("PATENTX_API_KEY", "secret-key-123456")
    resumed = []
    m = manager(parts, World(APIS), handlers(APIS), transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")),
                ready=lambda *a: resumed.append(a))
    rid = m.start("patent data", parent_run_id="run1")["request_id"]
    out = m.resume(rid, {"approve": "patentx", "terms_ok": True})
    failed = [t for t in out["tests"] if t["status"] == "fail"]
    assert failed and "401" in failed[0]["detail"] and out["outcome"].startswith("tests failed")
    assert parts["registry"].active("patentx") is None and resumed == []
    assert parts["registry"].get_request(rid)["status"] == "tests_failed"
    assert "tested_fail" in [e["event"] for e in parts["registry"].events("patentx")]


def test_free_option_wins_unless_the_model_justifies_the_paid_one(parts):
    apis = [("PaidCo", "paidco.example", False), ("PatentX", "patentx.example", True)]
    world = World(apis)
    out = manager(parts, world, handlers(apis, recommend="paidco")).start("patent data")
    rec = out["gate"]["recommendation"]
    assert rec["choice"] == "patentx" and "no reason" in rec["note"]            # overridden: paid chosen without justification

    out2 = manager({**parts, "ckpt": parts["ckpt"] + "2"}, world, handlers(apis, Recommendation=lambda p: Recommendation(
        choice="paidco", reasoning="far better coverage", paid_justification="covers 40 more patent offices"))).start("patent data")
    rec2 = out2["gate"]["recommendation"]
    assert rec2["choice"] == "paidco" and rec2["paid_justification"] == "covers 40 more patent offices"
    assert [c["is_free"] for c in out2["gate"]["candidates"] if c["connector_id"] in ("paidco", "patentx")] == [False, True]


def test_manifest_is_drafted_from_docs_when_there_is_no_openapi_spec_with_one_retry(parts):
    world = World(APIS, with_spec=False)
    calls = []

    def draft(prompt):
        calls.append(prompt)
        if len(calls) == 1:   # first attempt violates the manifest rules (plain http)
            return DraftManifest(base_url="http://api.patentx.example", auth_type="header", auth_name="X-Api-Key",
                                 endpoints=[DraftEndpoint(name="search", path="/patents", description="Search")], smoke_endpoint="search")
        return DraftManifest(base_url="https://api.patentx.example/v1", auth_type="header", auth_name="X-Api-Key",
                             endpoints=[DraftEndpoint(name="search", path="/patents", description="Search")], smoke_endpoint="search")

    out = manager(parts, world, handlers(APIS, DraftManifest=draft)).start("patent data")
    plan = next(c for c in out["gate"]["candidates"] if c["name"] == "PatentX")["integration_plan"]
    assert plan["host"] == "api.patentx.example" and len(calls) == 2 and "previous attempt was invalid" in calls[1]


def test_nothing_integrable_reports_why_instead_of_hanging(parts):
    only_mcp = lambda p: Candidates(candidates=[Cand(name="Some MCP", docs_url="https://mcp.example/server", kind="mcp", reason="mcp")])  # noqa: E731
    out = manager(parts, World(APIS), handlers(APIS, Candidates=only_mcp)).start("patent data")
    assert out["status"] == "failed" and "Some MCP" in out["problems"]
    assert out["gate"] is None


def test_a_request_waiting_at_the_gate_can_be_picked_up_by_a_new_manager(parts, monkeypatch):
    first = manager(parts, World(APIS), handlers(APIS))
    rid = first.start("patent data")["request_id"]
    second = manager(parts, World(APIS), handlers(APIS))          # a fresh process: nothing in memory
    waiting = second.pending(rid)
    assert waiting["status"] == "awaiting_user" and waiting["gate"]["recommendation"]["choice"] == "patentx"
    monkeypatch.setenv("PATENTX_API_KEY", "secret-key-123456")
    assert second.resume(rid, {"approve": "patentx", "terms_ok": True})["outcome"] == "integrated"
    assert second.pending(rid) is None                             # nothing waiting any more


def test_a_quote_that_exists_but_does_not_state_the_fact_is_not_reported_as_verified(parts):
    """The live USPTO failure: one unrelated sentence was attached to many 'facts'."""
    def facts(prompt):
        same = "The PatentX API is free for up to 1,000 requests per day."
        return FactList(facts=[Fact(field="free_tier", value="1,000 per day", quote=same, passage=1),
                               Fact(field="auth", value="No authentication is required", quote=same, passage=1),
                               Fact(field="signup", value="No signup is required", quote=same, passage=1)])

    def verdict(prompt):  # only the free-tier statement is actually backed by that sentence
        ok = "CLAIM: free tier" in prompt
        return SupportVerdict(verdict="supported" if ok else "not_supported", reasoning="r")

    out = manager(parts, World(APIS), handlers(APIS, FactList=facts, SupportVerdict=verdict)).start("patent data")
    px = next(c for c in out["gate"]["candidates"] if c["name"] == "PatentX")
    assert [e["field"] for e in px["verified_facts"]] == ["free_tier"]
    assert {"auth", "signup"} <= set(px["unverified"]) and px["facts_rejected"] == 2


def test_pages_that_need_javascript_are_rendered_and_empty_ones_are_not_used(parts):
    world = World(APIS)
    empty = b"<html><body>Enable JavaScript</body></html>"
    world.pages["https://docs.patentx.example/"] = (empty, "text/html")
    rendered = []

    def render(url, max_chars=2000):
        rendered.append(url)
        return {**World(APIS).fetch(url), "url": url}

    m = manager(parts, world, handlers(APIS))
    m.render_fn = render
    out = m.start("patent data")
    assert "https://docs.patentx.example/" in rendered
    px = next(c for c in out["gate"]["candidates"] if c["name"] == "PatentX")
    assert px["verified_facts"]                       # the rendered page gave real text

    world.pages["https://docs.patentx.example/"] = (empty, "text/html")
    dead = manager({**parts, "ckpt": parts["ckpt"] + "3"}, world, handlers(APIS))
    dead.render_fn = lambda url, max_chars=2000: {**World(APIS).fetch("https://docs.patentx.example/missing")}
    assert dead._fetch_page({"request_id": "x"}, "https://docs.patentx.example/") is None


def test_a_manifest_failure_names_the_actual_problem(parts):
    world = World(APIS, with_spec=False)
    bad = lambda p: DraftManifest(base_url="http://insecure.example", auth_type="none",  # noqa: E731
                                  endpoints=[DraftEndpoint(name="search", path="/p", description="d")], smoke_endpoint="search")
    out = manager(parts, world, handlers(APIS, DraftManifest=bad)).start("patent data")
    assert out["status"] == "failed" and "base_url" in out["problems"]["PatentX"]


def test_a_model_that_mislabels_an_mcp_server_as_rest_is_corrected(parts):
    mislabelled = lambda p: Candidates(candidates=[  # noqa: E731
        Cand(name="Patent MCP Server", docs_url="https://mcp.example/server", kind="rest", reason="serves patents")])
    out = manager(parts, World(APIS), handlers(APIS, Candidates=mislabelled)).start("patent data")
    assert out["status"] == "failed" and "not integrated" in out["problems"]["Patent MCP Server"]


from connectors.manifest import Requirements  # noqa: E402


def test_only_an_ongoing_free_tier_counts_as_free():
    cost = CapabilityManager._cost
    assert cost(Requirements(free_tier="free for up to 1,000 requests per day"))[0] is True
    assert cost(Requirements(free_tier="A free trial with 1,000 credits for 3 days is available."))[0] is None
    assert cost(Requirements(free_tier="free for 3 days"))[1] == "free trial only (then paid)"
    assert cost(Requirements(pricing_notes="costs 10 credits per call"))[0] is False
    assert cost(Requirements(needs_payment=True))[1] == "paid"
    assert cost(Requirements(needs_payment=False)) == (True, "no payment required (verified)")
    assert cost(Requirements()) == (None, "cost unknown")


def test_endpoint_names_are_normalized_by_code_not_rejected(parts):
    world = World(APIS, with_spec=False)
    draft = lambda p: DraftManifest(base_url="https://api.patentx.example/v1/", auth_type="header", auth_name="X-Api-Key",  # noqa: E731
                                    endpoints=[DraftEndpoint(name="Search Patents", path="/patents?q=x", description="Search")],
                                    smoke_endpoint="Search Patents")
    out = manager(parts, world, handlers(APIS, DraftManifest=draft)).start("patent data")
    plan = next(c for c in out["gate"]["candidates"] if c["name"] == "PatentX")["integration_plan"]
    assert list(plan["endpoints"]) == ["search_patents"] and plan["endpoints"]["search_patents"] == "/patents"
    assert plan["host"] == "api.patentx.example"


def test_a_trial_is_not_labelled_free_and_a_meaningless_justification_is_not_shown(parts):
    trial = lambda p: FactList(facts=[  # noqa: E731
        Fact(field="free_tier", value="A free trial with 1,000 credits for 3 days.", quote="The PatentX API is free for up to 1,000 requests per day.", passage=1),
        Fact(field="pricing", value="10 credits per call", quote="You must create an account to get an API key.", passage=1)])
    rec = lambda p: Recommendation(choice="patentx", reasoning="only option", paid_justification="There is no clear justification.")  # noqa: E731
    out = manager(parts, World(APIS), handlers(APIS, FactList=trial, Recommendation=rec)).start("patent data")
    px = next(c for c in out["gate"]["candidates"] if c["name"] == "PatentX")
    assert px["cost_label"] == "free trial only (then paid)" and px["is_free"] is None
    assert out["gate"]["recommendation"]["paid_justification"] is None   # the default choice has nothing to justify


def test_no_justification_is_attached_to_a_verified_free_choice(parts):
    rec = lambda p: Recommendation(choice="patentx", reasoning="free and fits", paid_justification="irrelevant text")  # noqa: E731
    out = manager(parts, World(APIS), handlers(APIS, Recommendation=rec)).start("patent data")
    assert out["gate"]["recommendation"]["paid_justification"] is None


def test_no_manifest_is_invented_when_there_is_no_readable_documentation(parts):
    world = World(APIS, with_spec=False)
    world.pages["https://docs.patentx.example/"] = (b"<html><body>Enable JavaScript</body></html>", "text/html")
    called = []
    m = manager(parts, world, handlers(APIS, DraftManifest=lambda p: called.append(p) or pytest.fail("must not be asked")))
    m.render_fn = lambda url, max_chars=2000: world.fetch(url)          # rendering does not help either
    out = m.start("patent data")
    assert out["status"] == "failed" and "no readable documentation" in out["problems"]["PatentX"] and not called


def test_gate_says_where_the_manifest_came_from_and_flags_an_unverified_auth_type(parts):
    out = manager(parts, World(APIS, with_spec=False), handlers(APIS, FactList=lambda p: FactList(facts=[]),
        DraftManifest=lambda p: DraftManifest(base_url="https://api.patentx.example/v1", auth_type="none",
                                              endpoints=[DraftEndpoint(name="Search", path="/p", description="d")],
                                              smoke_endpoint="https://api.patentx.example/v1/p"))).start("patent data")
    px = next(c for c in out["gate"]["candidates"] if c["name"] == "PatentX")
    assert px["manifest_source"] == "model" and any("drafted by the model" in w for w in px["warnings"])
    assert any("NOT backed by any quote" in w for w in px["warnings"])
    assert px["integration_plan"]["smoke_test"]["endpoint"] == "search"      # single endpoint: smoke name repaired


def test_the_default_choice_never_carries_a_justification_even_if_the_model_writes_one(parts):
    rec = lambda p: Recommendation(choice="patentx", reasoning="x", paid_justification="There is no clear justification.")  # noqa: E731
    out = manager(parts, World(APIS), handlers(APIS, Recommendation=rec)).start("patent data")
    assert out["gate"]["recommendation"]["paid_justification"] is None


def test_path_placeholders_the_model_forgot_to_declare_are_declared_by_code(parts):
    world = World(APIS, with_spec=False)
    draft = lambda p: DraftManifest(base_url="https://api.patentx.example/v1", auth_type="header", auth_name="X-Api-Key",  # noqa: E731
                                    endpoints=[DraftEndpoint(name="get_one", path="/patents/{number}", description="One")],
                                    smoke_endpoint="get_one", smoke_params={"number": "WO1"})
    out = manager(parts, world, handlers(APIS, DraftManifest=draft)).start("patent data")
    plan = next(c for c in out["gate"]["candidates"] if c["name"] == "PatentX")["integration_plan"]
    assert plan["endpoints"] == {"get_one": "/patents/{number}"} and plan["smoke_test"]["params"] == {"number": "WO1"}


def test_a_parameter_named_like_a_path_placeholder_is_made_a_path_parameter():
    from capability import with_path_params
    fixed = with_path_params("/records/{id}", [{"name": "id", "in": "query", "required": False, "type": "string"}, {"name": "page", "in": "query"}])
    assert [(p["name"], p["in"], p["required"]) for p in fixed if p["name"] == "id"] == [("id", "path", True)]
    assert [p["in"] for p in fixed if p["name"] == "page"] == ["query"]
    assert [p["name"] for p in with_path_params("/a/{x}/{y}", [])] == ["x", "y"]


def test_normalize_draft_repairs_what_models_actually_got_wrong():
    """Taken from live Qwen3 8B drafts: display names, a path as the smoke endpoint, path params declared as query."""
    from capability import DraftParam, normalize_draft
    draft = DraftManifest(
        base_url="https://data.records.example/api/", auth_type="bearer", auth_name="Authorization", smoke_endpoint="/records/search",
        smoke_params={"term": "example", "page": "1", "made_up": "x"},
        endpoints=[DraftEndpoint(name="Search Records", path="/records/search?term=x", description="d",
                                 params=[DraftParam(name="term"), DraftParam(name="page", type="integer")]),
                   DraftEndpoint(name="Get Record by ID", path="/records/{id}", description="d", params=[DraftParam(name="id")])])
    norm = normalize_draft(draft)
    assert norm["base_url"] == "https://data.records.example/api"
    assert [e["name"] for e in norm["endpoints"]] == ["search_records", "get_record_by_id"]
    assert norm["endpoints"][0]["path"] == "/records/search"
    assert norm["smoke_test"] == {"endpoint": "search_records", "params": {"term": "example", "page": "1"}}
    assert next(p for p in norm["endpoints"][1]["params"] if p["name"] == "id")["in"] == "path"
    from connectors.manifest import Manifest
    Manifest.model_validate({"connector_id": "records", "name": "R", "description": "d", "topics": ["t"], **norm})
