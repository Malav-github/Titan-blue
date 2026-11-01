import sqlite3

import httpx
import pytest

from tools.search import ledger, providers, search
from tools.search.fusion import fuse, normalize_url
from tools.search.providers import Hit


def hit(url, snippet="", engine="x"):
    return Hit(url, "t", snippet, engine)


def test_normalize_url_merges_trivial_variants():
    variants = [
        "http://www.Example.com/a/?utm_source=x",
        "https://example.com/a#section",
        "https://example.com/a/",
    ]
    assert len({normalize_url(u) for u in variants}) == 1
    assert normalize_url("https://example.com/a?id=1") != normalize_url("https://example.com/a?id=2")


def test_fusion_rewards_agreement_and_keeps_provenance():
    ranked = {
        "a": [hit("https://one.com/"), hit("https://shared.com/", "short")],
        "b": [hit("https://shared.com/", "a longer description"), hit("https://two.com/")],
    }
    out = fuse(ranked)
    assert out[0]["url"] == "https://shared.com/"
    assert [f["provider"] for f in out[0]["found_by"]] == ["a", "b"]
    assert out[0]["snippet"] == "a longer description"
    assert len(out) == 3


def test_ledger_is_append_only(tmp_path):
    path = tmp_path / "ledger.db"
    ledger.record("q", None, [], [], path=path)
    db = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE searches SET query = 'tampered'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM searches")
    db.close()


def fake_get(payload=None, status=200, raises=None):
    def _get(url, **kwargs):
        if raises:
            raise raises
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))
    return _get


def test_searxng_reports_degraded_engines(monkeypatch):
    payload = {
        "results": [{"url": "https://a.com/", "title": "A", "content": "c", "engines": ["brave"]}],
        "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
    }
    monkeypatch.setattr(providers.httpx, "get", fake_get(payload))
    report = providers.searxng("q", 10)
    assert report.ok and len(report.hits) == 1
    assert report.degraded == ["duckduckgo: CAPTCHA"]


def test_provider_failure_is_recorded_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(providers.httpx, "get", fake_get(raises=httpx.ConnectError("refused")))
    result = search("q", providers=["searxng"], ledger_path=tmp_path / "l.db")
    assert result["results"] == []
    assert not result["providers"][0]["ok"]
    assert any("ALL providers failed" in w for w in result["warnings"])


def test_brave_requires_key(monkeypatch):
    monkeypatch.setattr(providers, "BRAVE_API_KEY", "")
    report = providers.brave("q", 10)
    assert not report.ok and "BRAVE_API_KEY" in report.error


def test_brave_parsing_and_freshness(monkeypatch):
    seen = {}

    def _get(url, **kwargs):
        seen.update(kwargs)
        body = {"web": {"results": [{"url": "https://b.com/", "title": "B", "description": "d"}]}}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(providers, "BRAVE_API_KEY", "test-key")
    monkeypatch.setattr(providers.httpx, "get", _get)
    report = providers.brave("q", 5, recency="week")
    assert report.ok and report.hits[0].url == "https://b.com/"
    assert seen["params"]["freshness"] == "pw"
    assert seen["headers"]["X-Subscription-Token"] == "test-key"


def test_two_providers_one_down_still_returns_results(monkeypatch, tmp_path):
    def _get(url, **kwargs):
        if "brave.com" in url:
            raise httpx.ConnectError("down")
        body = {"results": [{"url": "https://a.com/", "engines": ["x"]}], "unresponsive_engines": []}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(providers, "BRAVE_API_KEY", "k")
    monkeypatch.setattr(providers.httpx, "get", _get)
    result = search("q", providers=["searxng", "brave"], ledger_path=tmp_path / "l.db")
    assert len(result["results"]) == 1
    assert any("brave failed" in w for w in result["warnings"])


def test_rejects_bad_arguments(tmp_path):
    with pytest.raises(ValueError):
        search("q", providers=["nope"], ledger_path=tmp_path / "l.db")
    with pytest.raises(ValueError):
        search("q", recency="decade", providers=["searxng"], ledger_path=tmp_path / "l.db")


@pytest.mark.network
def test_live_searxng_finds_known_site(tmp_path):
    result = search("Anthropic Claude", providers=["searxng"], ledger_path=tmp_path / "l.db")
    if not result["providers"][0]["ok"]:
        pytest.skip(f"SearXNG not running: {result['providers'][0]['error']}")
    assert any("anthropic.com" in r["url"] for r in result["results"])
