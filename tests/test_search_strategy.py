"""Search that does not fail on one engine: the Brave API carries the search, SearXNG supplements it; an engine that is blocked
or rate-limited is rested; SearXNG is started again when it does not answer; a hiccup in one engine is a note, not a failure;
and a page that refuses Titan is looked for elsewhere (another copy of the same document), never worked around."""

import json
import subprocess

import httpx
import pytest

from tools.search import health, providers
from tools.search import search as do_search
from tests.test_runner import GOOD_QUOTE, PAGE_TEXT, env, make_runner, types  # noqa: F401
from tools.archive import archive_bytes, utc_now


@pytest.fixture(autouse=True)
def health_file(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "PATH", tmp_path / "search_health.json")
    health._engines_cache.clear()


def test_a_blocked_engine_rests_longer_than_a_slow_one():
    now = 1_000_000.0
    assert health.rest("duckduckgo", "CAPTCHA", now) == now + 30 * 60
    assert health.rest("google", "timeout", now) == now + 10 * 60
    assert set(health.resting(now + 60)) == {"duckduckgo", "google"}
    assert set(health.resting(now + 11 * 60)) == {"duckduckgo"}                 # the slow one is back first


def test_resting_engines_are_left_out_of_searxng_queries(monkeypatch):
    monkeypatch.setattr(health, "general_engines", lambda base=None, ttl=0: ["google", "bing", "duckduckgo", "mojeek"])
    assert health.engines_to_use() is None                                      # nothing resting: SearXNG chooses
    health.rest("duckduckgo", "CAPTCHA")
    assert health.engines_to_use() == ["google", "bing", "mojeek"]


def test_searxng_is_started_again_when_it_does_not_answer_but_not_on_every_query(monkeypatch):
    started = []
    monkeypatch.setattr(httpx, "get", lambda *a, **k: None)
    fake = lambda cmd, **kw: started.append(cmd) or subprocess.CompletedProcess(cmd, 0, "titan-searxng", "")   # noqa: E731
    assert "started again" in health.restart_searxng(run=fake, now=1000.0)
    assert health.restart_searxng(run=fake, now=1100.0) is None                 # tried a moment ago: not again
    assert started == [["wsl", "-e", "docker", "start", "titan-searxng"]]


def test_an_engine_hiccup_is_a_note_and_the_engine_is_rested(monkeypatch, tmp_path):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"url": "https://a.example/x", "title": "A", "content": "a", "engine": "google"}],
                    "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]}

    monkeypatch.setattr(providers.httpx, "get", lambda *a, **k: Resp())
    monkeypatch.setitem(providers.PROVIDERS, "fakebrave", lambda q, n, r=None: providers.Report("fakebrave", ok=True, hits=[
        providers.Hit("https://b.example/y", "B", "b", "fakebrave")]))
    out = do_search("acme", providers=["searxng", "fakebrave"], ledger_path=tmp_path / "ledger.db")
    assert out["warnings"] == [] and out["notes"] == ["searxng engine duckduckgo: CAPTCHA"]
    assert "duckduckgo" in health.resting()


def test_a_provider_that_fails_is_still_a_warning(monkeypatch, tmp_path):
    monkeypatch.setitem(providers.PROVIDERS, "down", lambda q, n, r=None: providers.Report("down", ok=False, error="ConnectError"))
    out = do_search("acme", providers=["down"], ledger_path=tmp_path / "ledger.db")
    assert any("ALL providers failed" in w for w in out["warnings"])


# --- a page that refuses Titan ---

BLOCKED = "https://investor.acme.example/news/acme-evolves-its-operating-model-and-names-new-leaders"
TITLE = "Acme Evolves Its Operating Model to Accelerate Agentic Solutions and Names New Leaders"
COPY = "https://www.prnewswire.com/news-releases/acme-evolves-its-operating-model-302900001.html"


def test_a_page_that_refuses_titan_is_read_from_another_copy_of_the_same_document(env):
    from runner import Selection
    world = env["world"]
    searched = []

    def search(query, max_results=8, **kw):
        searched.append(query)
        if query.startswith('"'):
            return {"search_id": "s2", "providers": [], "warnings": [], "results": [
                {"url": BLOCKED, "title": TITLE, "snippet": "", "score": 1.0},
                {"url": "https://blog.example/opinion", "title": TITLE, "snippet": "my take", "score": 0.9},
                {"url": COPY, "title": TITLE, "snippet": "Acme announces", "score": 0.8}]}
        return {"search_id": "s1", "providers": [], "warnings": [], "results": [{"url": BLOCKED, "title": f"{TITLE} - Acme Investor Relations", "snippet": "", "score": 1.0}]}

    def fetch(url, max_chars=2000):
        if url == BLOCKED:
            return {"url": url, "final_url": url, "status": 403}
        world.fetched.append(url)
        sha, path = archive_bytes(f"<html><body><article><p>Acme announces a new operating model. {PAGE_TEXT}</p></article></body></html>".encode(), ".html")
        return {"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8", "method": "http",
                "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}

    runner = make_runner(env, Selection=lambda p: Selection(numbers=[1]))
    runner.search_fn, runner.fetch_fn = search, fetch
    result = runner.start("What does Acme charge?", budgets={"max_iterations": 1})
    events = env["runs"].events(result["run_id"])
    found = next(e["payload"] for e in events if e["type"] == "copy_found")
    assert found["blocked"] == BLOCKED and found["copy"] == COPY            # the blog with the same title is not a copy
    assert f'"{TITLE}"' in searched and world.fetched == [COPY]
    assert any(r["source_url"] == COPY for r in result["report"]["established"])
