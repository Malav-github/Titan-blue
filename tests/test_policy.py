import httpx
import pytest

import policy
from policy import Limiter, Policy, PolicyError
from connectors.caller import call_api
from connectors.manifest import Manifest
from connectors.registry import ConnectorRegistry
from tools import primary, web
from tools.archive import USER_AGENT


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        self.t += seconds


@pytest.fixture
def clock(tmp_path, monkeypatch):
    c = Clock()
    monkeypatch.setattr(policy, "_limiter", Limiter(tmp_path / "state.db", clock=c.now, sleep=c.sleep))
    monkeypatch.setattr(policy, "USER_AGENT", "TitanResearchBot/0.1 (ops@example.org)")   # what the senders declare
    return c


SEC = policy.policy_for("https://www.sec.gov/")


# --- the shipped policy ---

def test_hosts_match_the_real_domain_and_its_subdomains_only():
    for url in ("https://sec.gov/x", "https://www.sec.gov/x", "https://data.sec.gov/submissions/CIK1.json", "data.sec.gov"):
        assert policy.policy_for(url).id == "sec_edgar", url
    for url in ("https://notsec.gov/x", "https://sec.gov.evil.example/x", "https://example.com/", "https://sec.gov.cn/"):
        assert policy.policy_for(url) is None, url


def test_shipped_numbers_stay_inside_the_published_limits():
    e = SEC.enforced
    assert e["max_requests_per_second"] <= 10            # the SEC's stated maximum
    assert e["cooldown_minutes_after_block"] >= 10       # the SEC's stated wait
    assert e["require_contact_in_user_agent"] and not e["allow_crawling"] and not e["allow_browser_rendering"]
    assert {403, 429} <= set(e["block_statuses"])


def test_every_rule_carries_its_source_and_a_verbatim_quote():
    assert len(SEC.rules) >= 8
    for rule in SEC.rules:
        assert rule["quote"].strip() and rule["source_url"].startswith("https://www.sec.gov/"), rule["id"]
        assert rule["enforced_by"], rule["id"]


def test_the_policy_loader_refuses_incomplete_files(tmp_path):
    (tmp_path / "bad.json").write_text('{"id":"x","name":"x","hosts":["x.org"],"enforced":{},"rules":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        policy.load_policies(tmp_path)


# --- request budget ---

def test_never_more_than_the_allowed_requests_in_any_second(clock):
    stamps = []
    for _ in range(40):
        policy.before_request("https://data.sec.gov/submissions/CIK0000320193.json")
        stamps.append(clock.now())
    limit = SEC.enforced["max_requests_per_second"]
    for i, t in enumerate(stamps):
        assert sum(1 for s in stamps if t - 1.0 < s <= t) <= limit, f"window ending at request {i}"
    assert clock.sleeps > 0                              # it waited instead of bursting


def test_the_budget_is_shared_between_processes(clock, tmp_path):
    other = Limiter(tmp_path / "state.db", clock=clock.now, sleep=clock.sleep)   # a second process on the same file
    for _ in range(SEC.enforced["max_requests_per_second"]):
        other.acquire(SEC)
    with pytest.raises(PolicyError, match="exhausted"):
        policy.limiter().acquire(SEC, max_wait=0.0)


def test_other_sites_are_never_throttled_or_blocked(clock):
    for _ in range(100):
        policy.before_request("https://example.com/page")
    policy.after_response("https://example.com/page", 403)
    policy.before_request("https://example.com/page")
    assert clock.sleeps == 0


# --- pauses after a refusal ---

def test_a_403_or_429_pauses_every_request_until_the_cooldown_ends(clock):
    policy.after_response("https://www.sec.gov/Archives/x", 403)
    with pytest.raises(PolicyError, match="paused"):
        policy.before_request("https://data.sec.gov/submissions/CIK1.json")      # a different subdomain is paused too
    clock.t += 14 * 60
    with pytest.raises(PolicyError, match="paused"):
        policy.before_request("https://www.sec.gov/")
    clock.t += 2 * 60                                                             # 16 minutes in: past the 15 minute pause
    policy.before_request("https://www.sec.gov/")


@pytest.mark.parametrize("status, blocks", [(200, False), (404, False), (500, False), (403, True), (429, True)])
def test_only_refusal_statuses_pause_requests(clock, status, blocks):
    policy.after_response("https://data.sec.gov/x", status)
    if blocks:
        with pytest.raises(PolicyError):
            policy.before_request("https://data.sec.gov/x")
    else:
        policy.before_request("https://data.sec.gov/x")


def test_a_person_can_lift_a_pause_after_reviewing(clock):
    policy.after_response("https://www.sec.gov/", 429)
    policy.limiter().clear_block("sec_edgar")
    policy.before_request("https://www.sec.gov/")


# --- identity, crawling, browser ---

@pytest.mark.parametrize("agent", ["TitanResearchBot/0.1", "Bot (not-an-email)", "Bot (someone@)", "Bot (@nobody.org)"])
def test_requests_need_a_declared_contact_email(clock, monkeypatch, agent):
    monkeypatch.setattr(policy, "USER_AGENT", agent)
    with pytest.raises(PolicyError, match="TITAN_CONTACT"):
        policy.before_request("https://data.sec.gov/x")
    policy.before_request("https://example.com/")        # sites without a rule do not need it


def test_crawling_and_browser_rendering_are_refused_but_only_for_sites_that_forbid_them(clock):
    for kind in ("crawl", "browser"):
        with pytest.raises(PolicyError):
            policy.before_request("https://www.sec.gov/", kind=kind)
        policy.before_request("https://example.com/", kind=kind)


def test_requested_compression_matches_the_sample_headers():
    assert policy.request_headers("https://www.sec.gov/x") == {"Accept-Encoding": "gzip, deflate"}
    assert policy.request_headers("https://example.com/x") == {}


# --- wired into the real request paths ---

def mock_client(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(web.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(web, "check_public_url", lambda url: None)


def test_page_fetcher_sends_the_declared_headers_and_obeys_a_block(clock, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(403 if request.url.path == "/denied" else 200, text="ok", headers={"content-type": "text/plain"})

    mock_client(monkeypatch, handler)
    assert web.download("https://www.sec.gov/ok").status == 200
    assert seen[0]["accept-encoding"] == "gzip, deflate" and seen[0]["user-agent"] == USER_AGENT
    web.download("https://www.sec.gov/denied")                     # the SEC says no: everything pauses
    with pytest.raises(PolicyError, match="paused"):
        web.download("https://www.sec.gov/ok")
    assert len(seen) == 2                                          # the third request never left the machine
    assert web.download("https://example.com/ok").status == 200    # other sites carry on


def test_page_fetcher_refuses_before_sending_without_a_contact(clock, monkeypatch):
    calls = []
    mock_client(monkeypatch, lambda r: calls.append(r) or httpx.Response(200, text="ok"))
    monkeypatch.setattr(policy, "USER_AGENT", "TitanResearchBot/0.1")
    with pytest.raises(PolicyError):
        web.download("https://www.sec.gov/ok")
    assert calls == []


def test_site_discovery_refuses_to_crawl_sec_gov(clock, monkeypatch):
    monkeypatch.setattr(primary, "check_public_url", lambda url: None)
    with pytest.raises(PolicyError, match="crawling"):
        primary.discover_pages("www.sec.gov")


def test_browser_refuses_sec_gov_before_starting(clock, monkeypatch):
    from tools import browser
    monkeypatch.setattr(browser, "check_public_url", lambda url: None)
    with pytest.raises(PolicyError, match="browser"):
        browser.render_url("https://www.sec.gov/")


def sec_manifest() -> Manifest:
    return Manifest.model_validate({
        "connector_id": "sec_edgar", "name": "SEC EDGAR", "description": "Filings", "topics": ["filings"],
        "base_url": "https://data.sec.gov", "tier": "regulatory", "auth": {"type": "none"},
        "endpoints": [{"name": "submissions", "path": "/submissions/CIK{cik}.json", "description": "Filing history",
                       "params": [{"name": "cik", "in": "path"}]}],
        "smoke_test": {"endpoint": "submissions", "params": {"cik": "0000320193"}}})


def test_connector_calls_follow_the_same_rules(clock, tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "archive")
    registry = ConnectorRegistry(tmp_path / "c.db")
    m = sec_manifest()
    v = registry.propose(m)
    registry.event(m.connector_id, v, "approved", {}, "user")
    registry.event(m.connector_id, v, "tested_pass", {}, "test")
    registry.activate(m.connector_id, v)
    seen = []

    def handler(request):
        seen.append(request.headers)
        return httpx.Response(429 if len(seen) == 2 else 200, json={"cik": "320193"})

    t = httpx.MockTransport(handler)
    r = call_api(registry, "sec_edgar", "submissions", {"cik": "0000320193"}, run_id="r1", transport=t)
    assert r["status"] == 200 and seen[0]["accept-encoding"] == "gzip, deflate" and seen[0]["user-agent"] == USER_AGENT
    refused = call_api(registry, "sec_edgar", "submissions", {"cik": "0000320193"}, run_id="r1", transport=t)
    assert refused["status"] == 429 and not refused["ok"]            # the SEC answers 429 ...
    with pytest.raises(PolicyError, match="paused"):               # ... so the next call never goes out
        call_api(registry, "sec_edgar", "submissions", {"cik": "0000320193"}, run_id="r1", transport=t)
    assert len(seen) == 2


# --- the quote re-check ---

def test_the_rule_checker_flags_a_rule_the_provider_no_longer_states(tmp_path):
    import check_policies
    page = tmp_path / "p.html"
    page.write_text("<html><body><p>Current guidelines limit each user to a total of no more than 10 requests per second, "
                    "regardless of the number of machines used to submit requests.</p></body></html>", encoding="utf-8")

    def fetch(url, max_chars=0):
        return {"archive_path": str(page), "content_type": "text/html", "encoding": "utf-8", "final_url": url}

    pol = Policy("t", "T", ("t.org",), {}, (
        {"id": "still_there", "source_url": "https://t.org/p", "quote": "no more than 10 requests per second, regardless of the number of machines"},
        {"id": "reworded", "source_url": "https://t.org/p", "quote": "no more than 5 requests per second, regardless of the number of machines"}))
    results = {r["id"]: r for r in check_policies.check(pol, fetch)}
    assert results["still_there"]["ok"] and not results["reworded"]["ok"]


# --- manifests ---

def test_a_manifest_for_sec_gov_must_use_no_key_and_stay_within_the_budget(tmp_path):
    registry = ConnectorRegistry(tmp_path / "c.db")
    keyed = Manifest.model_validate({**sec_manifest().model_dump(by_alias=True), "auth": {"type": "header", "name": "X-Key"}})
    with pytest.raises(ValueError, match="needs no API key"):
        registry.propose(keyed)
    fast = Manifest.model_validate({**sec_manifest().model_dump(by_alias=True), "rate_limit": {"requests_per_minute": 600}})
    with pytest.raises(ValueError, match="at most 8 requests a second"):
        registry.propose(fast)
    assert registry.propose(sec_manifest()) == 1                     # the compliant manifest is accepted
    assert registry.list_active() == []                              # (proposed only: activation still needs the gate)


def test_manifests_for_other_hosts_are_not_affected(tmp_path):
    other = Manifest.model_validate({**sec_manifest().model_dump(by_alias=True), "connector_id": "openalex",
                                     "base_url": "https://api.openalex.org", "auth": {"type": "query", "name": "api_key"},
                                     "rate_limit": {"requests_per_minute": 600}})
    assert ConnectorRegistry(tmp_path / "c.db").propose(other) == 1
