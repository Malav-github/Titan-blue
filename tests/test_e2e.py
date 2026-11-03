"""Drives the real app in a real browser against the real API, with fake runner and manager (no models, no network).

Needs the built frontend:  cd frontend && npm run build
"""

import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from api.engine import Engine
from api.jobs import Jobs
from api.registry import Registry
from api.server import DIST_DIR, create_app
from api.templates import load_templates
from connectors.registry import ConnectorRegistry
from evidence import EvidenceStore
from runs import RunStore
from tests.test_app_flows import FakeManager, FakeRunner, PAGE, QUOTE
from tools.archive import archive_bytes

pytestmark = pytest.mark.skipif(not (DIST_DIR / "index.html").exists(), reason="build the frontend first: cd frontend && npm run build")


class RealisticRunner(FakeRunner):
    """Same pauses as the real runner; the report points at the claim that really is in the evidence store."""

    def snapshot(self, run_id):
        store = self.store
        c = store.list_claims()[0]
        return {"values": {"report": {
            "question": "What does the Starter plan cost?", "subject": "Acme", "summary": f"The Starter plan costs $29 per month. [claim:{c['claim_id']}]",
            "established": [{"claim_id": c["claim_id"], "claim": c["claim_text"], "value": c["value"], "quote": c["quote"],
                             "source_url": c["source_url"], "source_sha256": c["source_sha256"], "source_tier": c["source_tier"], "why": "the quote states the price"}],
            "partial": [], "not_supported": 0, "unchecked": 0, "off_topic": 0, "gaps": ["Team plan price"], "rejected_quotes": 1,
            "warnings": [], "budget_used": {"searches": 2, "fetches": 3, "iterations": 1}}}}


class RealisticManager(FakeManager):
    def pending(self, request_id):
        req = self.registry.get_request(request_id)
        if req["status"] != "awaiting_user":
            return None
        host = "data.sec.gov" if "filings" in req["need"] else "api.example.org"
        fact = {"field": "free_tier", "value": "free", "quote": "Anyone can access and download this information for free.",
                "source_url": f"https://{host}/about", "official": True}
        outside = {"field": "rate_limit", "value": "10 a second", "quote": "caps traffic at 10 requests per second",
                   "source_url": "https://blog.example.net/post", "official": False}
        return {"gate": {"type": "capability_gate", "need": req["need"], "note": "",
                         "recommendation": {"choice": "demo", "reasoning": "The only candidate."}, "your_steps": [],
                         "candidates": [{"name": "Demo API", "connector_id": "demo", "kind": "rest", "docs_url": f"https://{host}/docs",
                                         "cost_label": "free (verified)", "verified_facts": [fact, outside], "unverified": ["terms_storage"],
                                         "warnings": ["1 of 2 verified facts are quoted from sites that are not the provider's own."],
                                         "integration_plan": {"host": host, "tier": "regulatory", "auth": "none", "key_env": None,
                                                              "endpoints": {"submissions": "/submissions/CIK{cik}.json"},
                                                              "smoke_test": {"endpoint": "submissions"},
                                                              "budget": {"max_calls_per_day": 500, "max_calls_per_run": 30},
                                                              "rate_limit_per_minute": 30, "signup_url": None, "terms": {"url": None}}}]}}

    def resume(self, request_id, decision):
        if decision.get("approve"):      # like the real manager: tests are recorded first, then "ready"
            self.registry.request_event(request_id, "test_item", {"item": "Read-only smoke call succeeds", "status": "pass", "detail": "HTTP 200"})
        super().resume(request_id, decision)


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    mp = pytest.MonkeyPatch()
    mp.setattr("tools.archive.ARCHIVE_DIR", tmp / "archive")
    runs, connectors, store = RunStore(str(tmp / "runs.db")), ConnectorRegistry(tmp / "c.db"), EvidenceStore(tmp / "e.db")
    sha, path = archive_bytes(PAGE.encode(), ".html")
    src = store.ingest({"url": "https://acme.com/pricing", "final_url": "https://acme.com/pricing", "content_type": "text/html", "encoding": "utf-8",
                        "method": "http", "fetched_at": "2026-11-03T00:00:00+00:00", "sha256": sha, "archive_path": path})
    eid = store.add_entity("Acme", domain="acme.com", domain_status="confirmed")
    cid = store.add_claim(eid, "pricing.starter", "$29", "The Starter plan costs $29 per month.", src, QUOTE)["claim_id"]
    store.record_support_check(cid, "supported", "The quote states the price.", [], [], "gemma4:e4b", "v1")
    engine = Engine(Registry.load(), store, runs, {}, connectors=connectors, uploads_dir=tmp / "uploads", background=False)
    runner, manager = RealisticRunner(runs), RealisticManager(connectors)
    runner.store = store
    jobs = Jobs(runs, connectors, runner_factory=lambda: runner, manager_factory=lambda: manager, background=False)
    app = create_app(engine=engine, token="e2e-token", probe_map={"ollama": lambda: None}, store=store, jobs=jobs,
                     templates=load_templates(), dist_dir=DIST_DIR)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    mp.undo()


@pytest.fixture
def page(base_url):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(base_url=base_url)
        pg = ctx.new_page()
        pg.set_default_timeout(8000)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        yield pg
        assert errors == [], errors          # no page may throw while a flow runs
        browser.close()


def test_home_lists_the_apps(page: Page):
    page.goto("/")
    expect(page.get_by_role("heading", name="What would you like to do?")).to_be_visible()
    expect(page.get_by_role("heading", name="Research a company")).to_be_visible()
    expect(page.get_by_role("heading", name="Add a data source (API)")).to_be_visible()


def test_research_from_form_to_verified_claim(page: Page):
    page.goto("/apps/research-company")
    page.get_by_label("Company or subject").fill("A")                         # too short: the server says so, in words
    page.get_by_label("What do you want to know?").fill("What does the Starter plan cost?")
    page.get_by_role("button", name="Start research").click()
    expect(page.get_by_role("alert")).to_contain_text("too short")

    page.get_by_label("Company or subject").fill("Acme")
    page.get_by_role("button", name="Start research").click()
    expect(page.get_by_role("heading", name="Approve the plan")).to_be_visible()
    expect(page.get_by_text("Needs your approval")).to_be_visible()
    page.get_by_label("Facts to establish").fill("Starter plan price per month")
    page.get_by_role("button", name="Approve and run").click()

    expect(page.locator("header").get_by_text("Finished", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Result")).to_be_visible()
    expect(page.get_by_text("“The Starter plan costs $29 per month, billed annually.”")).to_be_visible()   # the quote is shown
    expect(page.get_by_text("Primary source")).to_be_visible()
    expect(page.get_by_text("Not established")).to_be_visible()                                              # gaps are not hidden
    expect(page.get_by_text("Run started")).to_be_visible()                                                 # the history is shown
    expect(page.get_by_text("Continued", exact=True)).to_be_visible()

    page.get_by_role("link", name="Open claim #1 with the quote in context").click()
    expect(page.locator("mark")).to_have_text("The Starter plan costs $29 per month, billed annually.")
    expect(page.get_by_text("Supported", exact=True).first).to_be_visible()
    expect(page.get_by_text("Proof it has not changed")).to_be_visible()


def test_runs_page_shows_the_run_and_evidence_lists_the_claim(page: Page):
    page.goto("/runs")
    expect(page.get_by_role("link", name="What does the Starter plan cost?").first).to_be_visible()
    page.get_by_role("link", name="Evidence").click()
    expect(page.get_by_text("The Starter plan costs $29 per month.")).to_be_visible()
    page.get_by_label("Company or subject").fill("nobody")
    page.get_by_role("button", name="Filter").click()
    expect(page.get_by_text("No claims yet")).to_be_visible()


def test_rejecting_a_plan_stops_the_run(page: Page):
    page.goto("/apps/research-company")
    page.get_by_label("Company or subject").fill("Acme")
    page.get_by_label("What do you want to know?").fill("Anything about the Team plan?")
    page.get_by_role("button", name="Start research").click()
    page.get_by_role("button", name="Reject").click()
    expect(page.locator("header").get_by_text("Stopped", exact=True)).to_be_visible()
    expect(page.get_by_text("This run was stopped before doing any research.")).to_be_visible()


def test_the_approval_gate_needs_the_terms_box_and_shows_the_documents(page: Page):
    page.goto("/apps/add-data-source")
    page.get_by_label("What data do you need?").fill("company filings")
    page.get_by_role("button", name="Start").click()
    expect(page.get_by_role("heading", name="Your decision")).to_be_visible()
    expect(page.get_by_text("third-party site")).to_be_visible()                       # facts quoted from other sites are labelled
    expect(page.get_by_role("link", name="https://www.sec.gov/about/privacy-information")).to_be_visible()   # the terms document is linked
    approve = page.get_by_role("button", name="Approve and integrate")
    expect(approve).to_be_disabled()                                                    # not without ticking that the terms were read
    page.get_by_label("I have read the provider’s terms and usage rules linked above", exact=False).check()
    expect(approve).to_be_enabled()
    approve.click()
    expect(page.get_by_text("Read-only smoke call succeeds")).to_be_visible()
    expect(page.locator("header").get_by_text("Ready", exact=True)).to_be_visible()


def test_a_provider_without_terms_gets_a_clear_warning_and_can_be_cancelled(page: Page):
    page.goto("/apps/add-data-source")
    page.get_by_label("What data do you need?").fill("scholarly works")
    page.get_by_role("button", name="Start").click()
    expect(page.get_by_text("No terms document was found for this provider")).to_be_visible()
    page.get_by_role("button", name="Cancel this request").click()
    expect(page.locator("header").get_by_text("Cancelled", exact=True)).to_be_visible()


def test_health_page_renders_checks(page: Page):
    page.goto("/health")
    expect(page.get_by_role("heading", name="Health")).to_be_visible()
    expect(page.get_by_text("Audit passed")).to_be_visible()


def test_the_page_works_with_the_keyboard_and_has_landmarks(page: Page):
    page.goto("/")
    expect(page.get_by_role("navigation", name="Main")).to_be_visible()
    expect(page.get_by_role("main")).to_be_visible()
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to content")).to_be_focused()
    page.keyboard.press("Enter")
    page.goto("/nonsense")
    expect(page.get_by_role("heading", name="Page not found")).to_be_visible()


def test_api_is_not_open_without_the_token(base_url):
    import httpx
    assert httpx.get(f"{base_url}/api/runs").status_code == 401
    assert 'name="titan-token"' in httpx.get(f"{base_url}/").text
