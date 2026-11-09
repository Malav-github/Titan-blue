"""Time saved without losing integrity: stop when the answer is in, order model work to avoid swapping, overlap network waiting."""

import threading
import time

from runner import Relevance
from support import SupportVerdict
from tests.test_runner import ScriptedLLM, default_handlers, env, make_runner  # noqa: F401
from tools.archive import archive_bytes, utc_now

QUOTE = "The Starter plan costs $29 per month."


def add_source(env, url, text=f"<p>{QUOTE}</p>"):
    sha, path = archive_bytes(f"<html><body><article>{text}</article></body></html>".encode(), ".html")
    return env["store"].ingest({"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8",
                                "method": "http", "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "",
                                "truncated": False})


def add_claims(env, urls, domain=""):
    """One claim for fact 0 from each url (each page has its own text, so it is its own source)."""
    store = env["store"]
    entity = store.add_entity("Acme", domain=domain, domain_status="confirmed" if domain else None)
    ids = {}
    for n, url in enumerate(urls):
        quote = f"The Starter plan costs ${29 + n} per month."
        sha = add_source(env, url, f"<p>{quote}</p>")
        r = store.add_claim(entity, "pricing.starter", f"${29 + n}", f"Acme's Starter plan costs ${29 + n} per month.", sha, quote)
        assert r["ok"], r
        ids[str(r["claim_id"])] = 0
    return ids


def state(items, **extra):
    return {"run_id": "r1", "question": "What does Acme charge?", "checklist": ["Starter plan price"], "claim_items": items,
            "new_claim_ids": [int(c) for c in items], "verdicts": {}, "relevant": {}, **extra}


def runner_with_log(env, log):
    def support(p):
        log.append("support")
        return SupportVerdict(verdict="supported", reasoning="stated")

    def relevance(p):
        log.append("relevance")
        return Relevance(addresses=True, reason="states the price")

    return make_runner(env, SupportVerdict=support, Relevance=relevance)


def test_checking_stops_once_two_independent_sites_back_the_fact(env):
    log = []
    runner = runner_with_log(env, log)
    items = add_claims(env, ["https://a.example/p", "https://b.example/p", "https://c.example/p", "https://d.example/p"])
    out = runner.n_check(state(items, settle_sites=2))
    assert log.count("support") == 3 and log.count("relevance") == 3          # one wave of three, then the fact is settled
    skipped = [e for e in env["runs"].events("r1") if e["type"] == "check_skipped"]
    assert len(skipped) == 1 and len(out["verdicts"]) == 3                      # the fourth claim stays on record, unchecked


def test_a_premise_needs_only_one_supported_claim(env):
    log = []
    runner = runner_with_log(env, log)
    items = add_claims(env, ["https://a.example/p", "https://b.example/p", "https://c.example/p"])
    runner.CHECK_WAVE = 1
    runner.n_check(state(items, settle_sites=1))
    assert log == ["support", "relevance"]
    assert sum(e["type"] == "check_skipped" for e in env["runs"].events("r1")) == 2


def test_a_primary_source_settles_a_fact_alone_and_is_checked_first(env):
    log = []
    runner = runner_with_log(env, log)
    items = add_claims(env, ["https://news.example/p", "https://acme.example/p"], domain="acme.example")
    runner.CHECK_WAVE = 1
    runner.n_check(state(items, settle_sites=2))
    assert log == ["support", "relevance"]                       # the company's own page was checked first and settled it
    assert sum(e["type"] == "check_skipped" for e in env["runs"].events("r1")) == 1


def test_work_is_ordered_support_checks_first_then_relevance_so_models_are_not_swapped_per_claim(env):
    log = []
    runner = runner_with_log(env, log)
    items = add_claims(env, ["https://a.example/p", "https://b.example/p", "https://c.example/p"])
    runner.n_check(state(items, settle_sites=3))
    assert log == ["support"] * 3 + ["relevance"] * 3


def test_one_claim_per_site_is_checked_before_a_second_from_the_same_site(env):
    runner = runner_with_log(env, [])
    items = add_claims(env, ["https://a.example/1", "https://a.example/2", "https://b.example/1"])
    ids = sorted(int(c) for c in items)
    order = runner._check_order(state(items), ids)
    sites = [env["store"].get_source(env["store"].get_claim(c)["source_sha256"])["final_url"].split("/")[2] for c in order]
    assert sites[:2] == ["a.example", "b.example"]


def test_extraction_stops_when_every_fact_is_backed_and_keeps_the_rest_for_later(env):
    from tests.scripted import Extraction, ExtractedClaim
    one = Extraction(claims=[ExtractedClaim(predicate="pricing.starter", value="$29", claim="Acme's Starter plan costs $29 per month.",
                                            quote=QUOTE, needed_fact=1)])
    runner = make_runner(env, Extraction=lambda p: one)
    urls = ["https://a.example/p", "https://b.example/p", "https://c.example/p"]
    shas = [add_source(env, u, f"<p>{QUOTE} Page {i}.</p>") for i, u in enumerate(urls)]
    out = runner.n_extract({**state({}, settle_sites=2), "budgets": {"max_chunks_per_source": 3}, "new_sources": shas, "rejected": 0,
                            "entity_id": env["store"].add_entity("Acme"), "all_claim_ids": []})
    assert out["backlog"] == [shas[2]] and any(e["type"] == "extract_stopped" for e in env["runs"].events("r1"))
    nxt = runner.n_fetch({"run_id": "r1", "selected": [], "fetches_used": 0, "fetched_keys": [], "fetch_failures": 0,
                          "backlog": out["backlog"]})
    assert nxt["new_sources"] == [shas[2]] and nxt["backlog"] == []             # read later if the gap remains


def test_downloads_overlap_in_time_and_keep_their_order_and_failures_apart(env):
    runner = make_runner(env)
    runner.network_workers = 4
    seen = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow(item):
        with lock:
            seen["now"] += 1
            seen["peak"] = max(seen["peak"], seen["now"])
        time.sleep(0.15)
        with lock:
            seen["now"] -= 1
        if item == 2:
            raise RuntimeError("blocked")
        return item * 10

    start = time.time()
    out = runner._side_by_side(slow, [0, 1, 2, 3])
    assert time.time() - start < 0.5 and seen["peak"] >= 2
    assert out[0] == 0 and out[1] == 10 and isinstance(out[2], RuntimeError) and out[3] == 30
    runner.network_workers = 1
    assert runner._side_by_side(lambda x: x + 1, [1, 2]) == [2, 3]
