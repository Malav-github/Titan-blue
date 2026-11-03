import json
import sqlite3

import httpx
import pytest
from pydantic import ValidationError

from connectors import caller
from connectors.caller import ConnectorError, call_api
from connectors.manifest import Manifest
from connectors.openapi import base_url_from, endpoints_from, is_openapi
from connectors.registry import ConnectorRegistry


def manifest_dict(**over):
    base = {
        "connector_id": "patentx", "name": "PatentX", "description": "Patent records", "topics": ["patents"],
        "base_url": "https://api.patentx.example/v1", "tier": "regulatory",
        "auth": {"type": "header", "name": "X-Api-Key"},
        "endpoints": [{"name": "search", "path": "/patents", "description": "Search patents",
                       "params": [{"name": "q", "in": "query", "required": True},
                                  {"name": "limit", "in": "query", "type": "integer"}]},
                      {"name": "get_patent", "path": "/patents/{number}", "description": "One patent",
                       "params": [{"name": "number", "in": "path"}]}],
        "smoke_test": {"endpoint": "search", "params": {"q": "plastic"}},
    }
    base.update(over)
    return base


def make_manifest(**over) -> Manifest:
    return Manifest.model_validate(manifest_dict(**over))


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setenv("PATENTX_API_KEY", "secret-key-123456")
    return ConnectorRegistry(tmp_path / "connectors.db")


def activate(registry, manifest=None):
    manifest = manifest or make_manifest()
    v = registry.propose(manifest)
    registry.event(manifest.connector_id, v, "approved", {}, "user")
    registry.event(manifest.connector_id, v, "tested_pass", {}, "test")
    registry.activate(manifest.connector_id, v)
    return v


def api(handler):
    return httpx.MockTransport(handler)


# --- manifest rules -------------------------------------------------------------------------

@pytest.mark.parametrize("over, why", [
    ({"base_url": "http://api.patentx.example"}, "https"),
    ({"base_url": "https://127.0.0.1/v1"}, "IP"),
    ({"base_url": "https://user:pw@api.patentx.example"}, "credentials"),
    ({"base_url": "https://api.patentx.example:8443"}, "port"),
    ({"connector_id": "Bad-Id"}, "id"),
    ({"smoke_test": {"endpoint": "nope", "params": {}}}, "smoke endpoint"),
    ({"smoke_test": {"endpoint": "search", "params": {}}}, "smoke missing required param"),
])
def test_manifest_rejects_unsafe_or_inconsistent_input(over, why):
    with pytest.raises(ValidationError):
        make_manifest(**over)


def test_endpoint_paths_must_be_safe_and_placeholders_must_match():
    for bad in ("/a/../b", "//evil", "http://evil/x", "/a?x=1", "/a/{id}"):
        eps = [{"name": "bad", "path": bad, "description": "x", "params": []}]
        with pytest.raises(ValidationError):
            make_manifest(endpoints=eps, smoke_test={"endpoint": "bad", "params": {}})


def test_key_env_is_derived_so_a_manifest_cannot_name_another_secret():
    assert make_manifest().key_env == "PATENTX_API_KEY"
    assert make_manifest(auth={"type": "none"}).key_env is None
    assert "key_env" not in Manifest.model_fields


# --- registry lifecycle ---------------------------------------------------------------------

def test_activation_requires_human_approval_and_passing_tests(registry):
    m = make_manifest()
    v = registry.propose(m)
    with pytest.raises(PermissionError, match="approved"):
        registry.activate("patentx", v)
    registry.event("patentx", v, "approved", {}, "user")
    with pytest.raises(PermissionError, match="tested_pass"):
        registry.activate("patentx", v)
    registry.event("patentx", v, "tested_pass")
    registry.activate("patentx", v)
    assert registry.active("patentx")[0] == v


def test_versions_are_immutable_and_disable_is_an_event(registry):
    v1 = activate(registry)
    assert registry.propose(make_manifest(description="newer")) == v1 + 1
    assert registry.active("patentx")[0] == v1          # a proposal alone changes nothing
    registry.disable("patentx", "key revoked")
    assert registry.active("patentx") is None and len(registry.list_active()) == 0
    registry.log_call("patentx", v1, "search", None, 200, True)  # triggers only fire on existing rows
    db = sqlite3.connect(registry.path)
    for sql in ("UPDATE manifests SET manifest = 'x'", "DELETE FROM connector_events", "DELETE FROM api_calls"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute(sql)
    db.close()


# --- call_api -------------------------------------------------------------------------------

def test_call_sends_key_in_header_validates_params_and_archives(registry):
    activate(registry)
    seen = {}

    def handler(request):
        seen.update(headers=dict(request.headers), url=str(request.url))
        return httpx.Response(200, json={"results": [{"number": "WO1"}]})

    out = call_api(registry, "patentx", "search", {"q": "plastic waste", "limit": 5}, run_id="r1", transport=api(handler))
    assert seen["headers"]["x-api-key"] == "secret-key-123456"
    assert "limit=5" in seen["url"] and "secret" not in out["url"]
    assert out["ok"] and out["method"] == "api" and out["source_tier"] == "regulatory"
    assert json.loads(open(out["archive_path"]).read())["results"][0]["number"] == "WO1"


def test_query_auth_key_is_sent_but_never_stored_in_the_url(registry):
    activate(registry, make_manifest(auth={"type": "query", "name": "api_key"}))
    seen = {}
    out = call_api(registry, "patentx", "search", {"q": "x"},
                   transport=api(lambda r: (seen.update(url=str(r.url)), httpx.Response(200, json={}))[1]))
    assert "api_key=secret-key-123456" in seen["url"]
    assert "secret" not in out["url"] and "api_key" not in out["url"]


def test_path_parameters_are_escaped(registry):
    activate(registry)
    seen = {}
    call_api(registry, "patentx", "get_patent", {"number": "../../etc/passwd"},
             transport=api(lambda r: (seen.update(url=str(r.url)), httpx.Response(200, json={}))[1]))
    assert seen["url"].startswith("https://api.patentx.example/v1/patents/") and "etc/passwd" not in seen["url"]
    assert "%2F" in seen["url"]


@pytest.mark.parametrize("endpoint, params, message", [
    ("search", {}, "missing required"),
    ("search", {"q": "x", "evil": "1"}, "unknown parameter"),
    ("search", {"q": "x", "limit": "many"}, "must be integer"),
    ("delete_everything", {}, "no endpoint"),
])
def test_bad_calls_are_refused_before_any_request(registry, endpoint, params, message):
    activate(registry)
    with pytest.raises(ConnectorError, match=message):
        call_api(registry, "patentx", endpoint, params, transport=api(lambda r: pytest.fail("no request expected")))


def test_missing_key_and_inactive_connector_are_refused(registry, monkeypatch):
    with pytest.raises(ConnectorError, match="no active connector"):
        call_api(registry, "patentx", "search", {"q": "x"})
    activate(registry)
    monkeypatch.delenv("PATENTX_API_KEY")
    monkeypatch.setattr(caller, "dotenv_values", lambda p: {})
    with pytest.raises(ConnectorError, match="PATENTX_API_KEY is not set"):
        call_api(registry, "patentx", "search", {"q": "x"}, transport=api(lambda r: httpx.Response(200)))


def test_placeholder_key_counts_as_missing(registry, monkeypatch):
    activate(registry)
    monkeypatch.setenv("PATENTX_API_KEY", "your_key_here")
    with pytest.raises(ConnectorError, match="not set"):
        call_api(registry, "patentx", "search", {"q": "x"}, transport=api(lambda r: httpx.Response(200)))


def test_redirects_are_not_followed(registry):
    activate(registry)
    with pytest.raises(ConnectorError, match="redirect"):
        call_api(registry, "patentx", "search", {"q": "x"},
                 transport=api(lambda r: httpx.Response(302, headers={"location": "https://evil.example/steal"})))


def test_a_key_echoed_by_the_api_is_redacted_before_archiving(registry):
    activate(registry)
    out = call_api(registry, "patentx", "search", {"q": "x"},
                   transport=api(lambda r: httpx.Response(200, json={"echo": "secret-key-123456"})))
    assert out["key_redacted_from_response"] and "secret-key" not in open(out["archive_path"]).read()


def test_http_errors_are_returned_not_raised_and_are_logged(registry):
    activate(registry)
    out = call_api(registry, "patentx", "search", {"q": "x"}, transport=api(lambda r: httpx.Response(429, text="slow down")))
    assert out["ok"] is False and out["status"] == 429
    assert registry.count_calls("patentx") == 1


def test_rate_limit_and_budgets_use_the_persistent_log(registry):
    activate(registry, make_manifest(rate_limit={"requests_per_minute": 2}, budget={"max_calls_per_run": 5}))
    ok = api(lambda r: httpx.Response(200, json={}))
    call_api(registry, "patentx", "search", {"q": "a"}, run_id="r", transport=ok)
    call_api(registry, "patentx", "search", {"q": "b"}, run_id="r", transport=ok)
    with pytest.raises(ConnectorError, match="rate limit"):
        call_api(registry, "patentx", "search", {"q": "c"}, run_id="r", transport=ok)
    reopened = ConnectorRegistry(registry.path)              # a fresh process sees the same history
    with pytest.raises(ConnectorError, match="rate limit"):
        call_api(reopened, "patentx", "search", {"q": "d"}, run_id="r", transport=ok)


def test_per_run_and_spend_caps(registry):
    activate(registry, make_manifest(rate_limit={"requests_per_minute": 100},
                                     budget={"max_calls_per_run": 1, "cost_per_call_usd": 0.5, "max_cost_per_day_usd": 0.75}))
    ok = api(lambda r: httpx.Response(200, json={}))
    call_api(registry, "patentx", "search", {"q": "a"}, run_id="r1", transport=ok)
    with pytest.raises(ConnectorError, match="per-run budget"):
        call_api(registry, "patentx", "search", {"q": "b"}, run_id="r1", transport=ok)
    with pytest.raises(ConnectorError, match="spend cap"):
        call_api(registry, "patentx", "search", {"q": "c"}, run_id="r2", transport=ok)


# --- OpenAPI --------------------------------------------------------------------------------

SPEC = {
    "openapi": "3.0.1", "servers": [{"url": "https://api.example.org/v2"}],
    "paths": {
        "/works": {"get": {"operationId": "listWorks", "summary": "List works",
                           "parameters": [{"name": "search", "in": "query", "schema": {"type": "string"}},
                                          {"name": "per-page", "in": "query", "schema": {"type": "integer"}}]}},
        "/works/{id}": {"get": {"operationId": "getWork", "parameters": [{"name": "id", "in": "path", "required": True}]}},
        "/works/new": {"post": {"operationId": "createWork"}},
        "/upload": {"get": {"operationId": "weird", "requestBody": {}}},
    },
}


def test_openapi_conversion_keeps_only_readonly_endpoints():
    assert is_openapi(SPEC) and base_url_from(SPEC) == "https://api.example.org/v2"
    eps = endpoints_from(SPEC)
    assert [e.name for e in eps] == ["listworks", "getwork"]
    assert {p.name: p.type for p in eps[0].params} == {"search": "string", "per-page": "integer"}
    assert eps[1].params[0].in_ == "path" and eps[1].params[0].required
    assert not is_openapi({"swagger": "2.0"}) and base_url_from({"servers": [{"url": "http://x"}]}) is None


def test_key_listed_as_a_required_parameter_is_dropped_so_a_smoke_test_exists():
    """OpenAlex's spec lists api_key as a required query parameter on every endpoint; the caller adds the key itself."""
    from capability import CapabilityManager
    from connectors.openapi import drop_auth_param
    spec = {"openapi": "3.1.0", "servers": [{"url": "https://api.example.org"}],
            "paths": {"/works": {"get": {"operationId": "listWorks", "parameters": [
                          {"name": "api_key", "in": "query", "required": True}, {"name": "search", "in": "query"}]}},
                      "/works/{id}": {"get": {"operationId": "getWork", "parameters": [
                          {"name": "id", "in": "path", "required": True}, {"name": "api_key", "in": "query", "required": True}]}}}}
    assert CapabilityManager._smoke_from(endpoints_from(spec)) is None          # the bug: no endpoint without required params
    eps = drop_auth_param(endpoints_from(spec), "query", "api_key")
    assert [p.name for p in eps[0].params] == ["search"]
    assert [p.name for p in eps[1].params] == ["id"]                              # other required params stay
    assert CapabilityManager._smoke_from(eps) == {"endpoint": "listworks", "params": {}}
    other = drop_auth_param(endpoints_from(spec), "bearer", None)               # only a query key is removed
    assert "api_key" in [p.name for p in other[0].params]
