"""In the browser: archive the evidence store from the app, then read the archived run and claim through the same pages,
marked read-only. Its own server and its own temporary store, so archiving does not empty the store other browser tests use."""

import os
import socket
import stat
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

import stores
from api.engine import Engine
from api.jobs import Jobs
from api.registry import Registry
from api.server import create_app
from connectors.registry import ConnectorRegistry
from evidence import EvidenceStore
from runs import RunStore
from tests.test_e2e import DIST_DIR, PAGE, QUOTE
from tools.archive import archive_bytes

pytestmark = pytest.mark.skipif(not (DIST_DIR / "index.html").exists(), reason="frontend not built")


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e-archives")
    mp = pytest.MonkeyPatch()
    mp.setattr("tools.archive.ARCHIVE_DIR", tmp / "archive")
    runs, connectors, store = RunStore(str(tmp / "runs.db")), ConnectorRegistry(tmp / "c.db"), EvidenceStore(tmp / "e.db")
    sha, path = archive_bytes(PAGE.encode(), ".html")
    src = store.ingest({"url": "https://acme.com/pricing", "final_url": "https://acme.com/pricing", "content_type": "text/html", "encoding": "utf-8",
                        "method": "http", "fetched_at": "2026-11-03T00:00:00+00:00", "sha256": sha, "archive_path": path})
    eid = store.add_entity("Acme", domain="acme.com", domain_status="confirmed")
    store.add_claim(eid, "pricing.starter", "$29", "The Starter plan costs $29 per month.", src, QUOTE)
    runs.emit("run1", "api", "queued", {"kind": "research", "question": "What does the Starter plan cost?"})
    runs.emit("run1", "runner", "run_started", {"question": "What does the Starter plan cost?", "subject": "Acme"})
    runs.emit("run1", "runner", "run_finished", {"established": 1})
    engine = Engine(Registry.load(), store, runs, {}, connectors=connectors, uploads_dir=tmp / "uploads", background=False)
    jobs = Jobs(runs, connectors, background=False)
    app = create_app(engine=engine, token="e2e-token", probe_map={}, store=store, jobs=jobs, templates={}, dist_dir=DIST_DIR,
                     layout=stores.Layout(tmp, tmp / "e.db"))
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
    for p in tmp.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)


@pytest.fixture
def page(base_url):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_context(base_url=base_url).new_page()
        pg.set_default_timeout(8000)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        yield pg
        assert errors == [], errors
        browser.close()


def test_archive_the_store_from_the_app_and_read_the_archived_evidence(page: Page):
    page.goto("/stores")
    expect(page.get_by_role("heading", name="Current store")).to_be_visible()
    expect(page.get_by_text("1 runs · 1 claims")).to_be_visible()
    button = page.get_by_role("button", name="Archive and start a new store")
    expect(button).to_be_disabled()                                   # not without ticking the box
    page.get_by_label("Name for the archive").fill("before ontologies")
    page.get_by_label("Why (kept in the archive)").fill("source labels changed")
    page.get_by_role("checkbox").check()
    button.click()
    expect(page.get_by_role("status")).to_contain_text("The new store is empty")
    expect(page.get_by_text("0 runs · 0 claims")).to_be_visible()

    page.get_by_role("link", name="before ontologies").first.click()
    expect(page.get_by_text("Why it was archived: source labels changed")).to_be_visible()
    page.get_by_role("button", name="Check it again now").click()
    expect(page.get_by_role("status")).to_contain_text("Unchanged")

    page.get_by_role("link", name="#1").click()
    expect(page.get_by_text("Archived claim, read only")).to_be_visible()
    expect(page.locator("mark")).to_have_text(QUOTE)
    page.get_by_role("link", name="Back to this archive").click()
    page.get_by_role("link", name="What does the Starter plan cost?").click()
    expect(page.get_by_text("Archived run, read only")).to_be_visible()
    expect(page.locator("header").get_by_text("Finished", exact=True)).to_be_visible()

    page.goto("/evidence")                                            # the live store is new
    expect(page.get_by_text("No claims yet")).to_be_visible()
