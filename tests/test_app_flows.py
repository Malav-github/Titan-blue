"""The flows a person drives from the app: research runs, templates, the capability gate, connectors, evidence."""

import pytest
from fastapi.testclient import TestClient

from api.engine import Engine
from api.jobs import Jobs, run_kind
from api.registry import Registry
from api.server import create_app, terms_documents
from api.templates import load_templates
from connectors.manifest import Manifest
from connectors.registry import ConnectorRegistry
from evidence import EvidenceStore
from runs import RunStore
from tools.archive import archive_bytes

TOKEN = "t"
H = {"x-titan-token": TOKEN}
PAGE = "<html><body><article><p>The Starter plan costs $29 per month, billed annually.</p></article></body></html>"
QUOTE = "The Starter plan costs $29 per month, billed annually."


class FakeRunner:
    """Emits the same events as the real runner and pauses where it does."""

    def __init__(self, runs):
        self.runs = runs
        self.decisions = []

    def start(self, question, subject=None, require_approval=False, pause_after=None, budgets=None,
              allow_capabilities=False, run_id=None):
        self.runs.emit(run_id, "runner", "run_started", {"question": question, "subject": subject, "budgets": budgets or {},
                                                          "require_approval": require_approval})
        self.runs.emit(run_id, "plan", "plan_ready", {"queries": ["acme pricing"]})
        self.runs.emit(run_id, "runner", "awaiting_approval", {"question": question, "subject": subject,
                                                                "queries": ["acme pricing"], "checklist": ["Starter price"],
                                                                "page_categories": ["pricing"]})
        return {"run_id": run_id}

    def resume(self, run_id, decision=None, pause_after=None):
        self.decisions.append(decision)
        self.runs.emit(run_id, "runner", "resumed", {"decision": decision})
        if decision and decision.get("approved") is False:
            self.runs.emit(run_id, "runner", "run_stopped", {"reason": "plan rejected"})
        elif decision is not None and "capability" in decision:
            self.runs.emit(run_id, "runner", "run_finished", {"established": 0})
        elif any(e["type"] == "capability_needed" for e in self.runs.events(run_id)) is False and decision and decision.get("approved"):
            self.runs.emit(run_id, "extract", "claim_recorded", {"claim_id": 1, "claim": "Starter costs $29", "quote": QUOTE})
            self.runs.emit(run_id, "runner", "run_finished", {"established": 1})
        return {"run_id": run_id}

    def snapshot(self, run_id):
        return {"values": {"report": {"question": "q", "established": [{"claim_id": 1}], "partial": [], "gaps": []}}}


class FakeManager:
    def __init__(self, registry):
        self.registry = registry
        self.decisions = []

    def start(self, need, parent_run_id=None, request_id=None, known_api=None):
        self.known_api = known_api
        self.registry.request_event(request_id, "researching", {"need": need})
        self.registry.request_event(request_id, "awaiting_user", {})

    def pending(self, request_id):
        req = self.registry.get_request(request_id)
        if req["status"] != "awaiting_user":
            return None
        host = "data.sec.gov" if "sec" in req["need"].lower() else "api.example.org"
        return {"gate": {"type": "capability_gate", "need": req["need"], "candidates": [
            {"name": "X", "connector_id": "x", "integration_plan": {"host": host, "terms": {"url": None}}}]}}

    def resume(self, request_id, decision):
        self.decisions.append(decision)
        self.registry.request_event(request_id, "cancelled" if not decision.get("approve") else "ready", {})


@pytest.fixture
def parts(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "archive")
    runs = RunStore(str(tmp_path / "runs.db"))
    connectors = ConnectorRegistry(tmp_path / "c.db")
    store = EvidenceStore(tmp_path / "e.db")
    engine = Engine(Registry.load(), store, runs, {}, connectors=connectors, uploads_dir=tmp_path / "uploads", background=False)
    runner, manager = FakeRunner(runs), FakeManager(connectors)
    jobs = Jobs(runs, connectors, runner_factory=lambda: runner, manager_factory=lambda: manager, background=False)
    return {"runs": runs, "connectors": connectors, "store": store, "engine": engine, "runner": runner,
            "manager": manager, "jobs": jobs, "tmp": tmp_path}


@pytest.fixture
def client(parts):
    app = create_app(engine=parts["engine"], token=TOKEN, probe_map={}, store=parts["store"], jobs=parts["jobs"],
                     templates=load_templates(), dist_dir=None)
    return TestClient(app)


def start_research(client, **over):
    inputs = {"subject": "Acme", "question": "What does the Starter plan cost?", **over}
    r = client.post("/api/templates/research-company/run", headers=H, json={"inputs": inputs})
    assert r.status_code == 202, r.text
    return r.json()["run_id"]


# --- templates ---

def test_shipped_templates_are_valid_and_listed(client):
    listed = {t["id"]: t for t in client.get("/api/templates", headers=H).json()}
    assert set(listed) == {"research-company", "add-data-source"}
    assert listed["research-company"]["kind"] == "research" and "subject" in listed["research-company"]["inputs"]["properties"]


def test_template_forms_are_validated_with_every_problem_listed(client):
    r = client.post("/api/templates/research-company/run", headers=H, json={"inputs": {"subject": "A", "max_searches": 99, "bogus": 1}})
    assert r.status_code == 422
    text = " | ".join(r.json()["detail"]["problems"])
    for expected in ("unknown field 'bogus'", "question", "greater than the maximum of 20", "too short"):
        assert expected in text, expected
    assert client.post("/api/templates/none/run", headers=H, json={}).status_code == 404


def test_the_api_name_and_docs_url_go_together(client):
    r = client.post("/api/templates/add-data-source/run", headers=H, json={"inputs": {"need": "scholarly works", "api_name": "OpenAlex"}})
    assert r.status_code == 422 and "both the API name and its documentation URL" in " ".join(r.json()["detail"]["problems"])


# --- research runs ---

def test_a_research_run_waits_for_approval_and_shows_the_plan(client, parts):
    rid = start_research(client, max_fetches=5)
    run = client.get(f"/api/runs/{rid}", headers=H).json()
    assert run["kind"] == "research" and run["status"] == "awaiting_approval" and run["subject"] == "Acme"
    assert run["approval"]["queries"] == ["acme pricing"] and run["approval"]["checklist"] == ["Starter price"]
    assert run["budgets"]["max_fetches"] == 5 and run["report"] is None
    assert run_kind(parts["runs"].events(rid)) == "research"


def test_approving_with_edits_resumes_the_run_and_returns_the_report(client, parts):
    rid = start_research(client)
    r = client.post(f"/api/runs/{rid}/decision", headers=H,
                    json={"approved": True, "queries": ["acme pricing page"], "checklist": ["Starter price per month"]})
    assert r.status_code == 202
    assert parts["runner"].decisions[-1] == {"approved": True, "queries": ["acme pricing page"], "checklist": ["Starter price per month"]}
    run = client.get(f"/api/runs/{rid}", headers=H).json()
    assert run["status"] == "finished" and run["report"]["established"][0]["claim_id"] == 1


def test_rejecting_stops_the_run_and_a_second_decision_is_refused(client):
    rid = start_research(client)
    client.post(f"/api/runs/{rid}/decision", headers=H, json={"approved": False})
    assert client.get(f"/api/runs/{rid}", headers=H).json()["status"] == "stopped"
    assert client.post(f"/api/runs/{rid}/decision", headers=H, json={"approved": True}).status_code == 409


def test_a_queued_run_is_visible_before_it_starts(parts, client):
    parts["jobs"].pool = None
    rid = parts["runs"].emit("queuedrun", "api", "queued", {"kind": "research", "question": "q", "subject": "s"}) and "queuedrun"
    run = client.get(f"/api/runs/{rid}", headers=H).json()
    assert run["status"] == "queued" and run["kind"] == "research"
    assert any(r["run_id"] == rid and r["status"] == "queued" for r in client.get("/api/runs", headers=H).json())


def test_a_run_waiting_for_a_data_source_can_continue_with_or_without_one(client, parts):
    rid = start_research(client)
    parts["runs"].emit(rid, "runner", "capability_needed", {"request_id": "r1", "need": "patent data", "reason": "no pages"})
    run = client.get(f"/api/runs/{rid}", headers=H).json()
    assert run["status"] == "needs_capability" and run["capability"]["need"] == "patent data"
    assert client.post(f"/api/runs/{rid}/continue", headers=H, json={"capability": "nothere"}).status_code == 422   # not an active connector
    assert client.post(f"/api/runs/{rid}/continue", headers=H, json={"capability": None}).status_code == 202
    assert parts["runner"].decisions[-1] == {}                                                                    # carry on without one
    assert client.post(f"/api/runs/{rid}/continue", headers=H, json={}).status_code == 409                        # no longer waiting


def test_research_streams_end_where_a_person_has_to_act(client):
    rid = start_research(client)
    text = client.get(f"/api/runs/{rid}/stream", headers=H).text
    assert "event: run_started" in text and "event: awaiting_approval" in text


def test_a_failure_while_starting_is_recorded_not_lost(parts, client):
    class Broken:
        def start(self, *a, **k):
            raise ConnectionError("Ollama is not reachable")
    parts["jobs"]._runner = Broken()
    rid = start_research(client)
    run = client.get(f"/api/runs/{rid}", headers=H).json()
    assert run["status"] == "failed"
    assert any("Ollama is not reachable" in str(e["payload"]) for e in parts["runs"].events(rid))


# --- capability gate ---

def start_request(client, need="scholarly works", **inputs):
    r = client.post("/api/templates/add-data-source/run", headers=H, json={"inputs": {"need": need, **inputs}})
    assert r.status_code == 202, r.text
    return r.json()["request_id"]


def test_a_named_api_reaches_the_manager_and_the_gate_lists_the_terms_documents(client, parts):
    rid = start_request(client, "SEC filings", api_name="SEC EDGAR", docs_url="https://www.sec.gov/x")
    assert parts["manager"].known_api == {"name": "SEC EDGAR", "docs_url": "https://www.sec.gov/x"}
    req = client.get(f"/api/capability/requests/{rid}", headers=H).json()
    assert req["status"] == "awaiting_user"
    docs = req["gate"]["candidates"][0]["terms_documents"]
    urls = {d["url"] for d in docs}
    assert "https://www.sec.gov/about/privacy-information" in urls and "https://www.sec.gov/about/developer-resources" in urls
    assert all(d["url"].startswith("https://www.sec.gov/") and d["why"] for d in docs)


def test_a_provider_without_a_terms_document_shows_none_instead_of_inventing_one(client):
    rid = start_request(client)
    assert client.get(f"/api/capability/requests/{rid}", headers=H).json()["gate"]["candidates"][0]["terms_documents"] == []
    assert terms_documents({"integration_plan": {"host": "x.org", "terms": {"url": "https://x.org/terms"}}}) == \
        [{"url": "https://x.org/terms", "why": "terms of use found while researching"}]


def test_approval_needs_the_terms_box_and_only_works_while_waiting(client, parts):
    rid = start_request(client)
    no_terms = client.post(f"/api/capability/requests/{rid}/decision", headers=H, json={"approve": "x", "terms_ok": False})
    assert no_terms.status_code == 422 and "terms" in no_terms.json()["detail"]["problems"][0]
    assert parts["manager"].decisions == []                                      # nothing reached the manager
    ok = client.post(f"/api/capability/requests/{rid}/decision", headers=H, json={"approve": "x", "terms_ok": True})
    assert ok.status_code == 202 and parts["manager"].decisions == [{"approve": "x", "terms_ok": True}]
    assert client.post(f"/api/capability/requests/{rid}/decision", headers=H, json={"approve": None}).status_code == 409
    assert client.post("/api/capability/requests/none/decision", headers=H, json={}).status_code == 404


def test_cancelling_needs_no_terms_box(client, parts):
    rid = start_request(client)
    assert client.post(f"/api/capability/requests/{rid}/decision", headers=H, json={"approve": None}).status_code == 202
    assert parts["manager"].decisions == [{"approve": None}]
    assert {r["request_id"]: r["status"] for r in client.get("/api/capability/requests", headers=H).json()}[rid] == "cancelled"


# --- connectors and evidence ---

def test_active_connectors_are_listed_with_usage(client, parts):
    m = Manifest.model_validate({
        "connector_id": "openalex", "name": "OpenAlex", "description": "Works", "topics": ["works"],
        "base_url": "https://api.openalex.org", "tier": "secondary", "auth": {"type": "query", "name": "api_key"},
        "endpoints": [{"name": "listworks", "path": "/works", "description": "List works"}],
        "smoke_test": {"endpoint": "listworks", "params": {}}})
    reg = parts["connectors"]
    v = reg.propose(m)
    reg.event("openalex", v, "approved", {}, "user")
    reg.event("openalex", v, "tested_pass", {}, "test")
    reg.activate("openalex", v)
    listed = client.get("/api/connectors", headers=H).json()
    assert listed[0]["connector_id"] == "openalex" and listed[0]["host"] == "api.openalex.org" and listed[0]["calls_last_24h"] == 0
    detail = client.get("/api/connectors/openalex", headers=H).json()
    assert detail["manifest"]["auth"] == {"type": "query", "name": "api_key"} and detail["events"]
    assert "key" not in str(detail["manifest"]).lower().replace("api_key", "")      # a name only, never a value
    assert client.get("/api/connectors/nothere", headers=H).status_code == 404


def test_claims_are_listed_and_shown_with_their_quote_in_context(client, parts):
    store = parts["store"]
    sha, path = archive_bytes(PAGE.encode(), ".html")
    src = store.ingest({"url": "https://acme.com/p", "final_url": "https://acme.com/p", "content_type": "text/html",
                        "encoding": "utf-8", "method": "http", "fetched_at": "2026-11-03T00:00:00+00:00", "sha256": sha, "archive_path": path})
    eid = store.add_entity("Acme", domain="acme.com", domain_status="confirmed")
    cid = store.add_claim(eid, "pricing.starter", "$29", "The Starter plan costs $29 per month.", src, QUOTE)["claim_id"]
    store.record_support_check(cid, "supported", "the quote states the price", [], [], "gemma4:e4b", "v1")
    listed = client.get("/api/evidence/claims", headers=H).json()
    assert listed[0]["claim_id"] == cid and listed[0]["verdict"] == "supported" and listed[0]["source_tier"] == "primary"
    assert client.get("/api/evidence/claims?entity=nobody", headers=H).json() == []
    d = client.get(f"/api/evidence/claims/{cid}", headers=H).json()
    assert d["quote"] == QUOTE and d["source_url"] == "https://acme.com/p" and d["support_checks"][0]["verdict"] == "supported"
    assert d["entity"]["name"] == "Acme" and d["text_sha256"]
    assert client.get("/api/evidence/claims/999", headers=H).status_code == 404


# --- serving the app ---

def test_the_built_app_is_served_with_the_token_and_cannot_escape_its_folder(parts, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><head><title>Titan</title></head><body>app</body></html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    c = TestClient(create_app(engine=parts["engine"], token=TOKEN, probe_map={}, store=parts["store"], jobs=parts["jobs"],
                              templates={}, dist_dir=dist))
    page = c.get("/")
    assert page.status_code == 200 and f'<meta name="titan-token" content="{TOKEN}">' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert c.get("/runs/abc").text == page.text                                  # app routes fall back to the page
    assert c.get("/assets/app.js").text == "console.log(1)"
    assert "secret" not in c.get("/..%2fsecret.txt").text and "secret" not in c.get("/../secret.txt").text
    assert c.get("/api/nothing", headers=H).status_code == 404                   # unknown API paths are not the page
    assert c.get("/api/nothing").status_code == 401
    assert c.get("/", headers={"host": "evil.example"}).status_code == 400        # the token is never served to another host


def test_templates_appear_in_the_order_they_ask_for(client):
    assert [t["id"] for t in client.get("/api/templates", headers=H).json()] == ["research-company", "add-data-source"]
