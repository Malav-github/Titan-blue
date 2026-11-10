"""The verification agent (verification.py) and a person's own check of a claim (EvidenceStore.record_review)."""

import pytest

from tests.test_runner import PAGE_TEXT, env, make_runner  # noqa: F401
from tools.archive import archive_bytes, utc_now
from verification import Verifier

FILLER = "Other news of the week follows here. " * 8


def page(url, text):
    sha, path = archive_bytes(f"<html><body><article><p>{text}</p></article></body></html>".encode(), ".html")
    return {"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8", "method": "http",
            "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}


def answered(env):
    """A finished research run whose answer is Acme's own pricing page (Tier 1)."""
    runner = make_runner(env)
    result = runner.start("What does Acme charge?", budgets={"max_iterations": 1})
    assert result["status"] == "finished" and result["report"]["established"]
    return runner, result["run_id"]


def with_web(runner, pages: dict, results: list[str]):
    """The web the verifier sees: search results and what each address serves now."""
    runner.search_fn = lambda query, max_results=8, **extra: {"results": [{"url": u, "title": u, "snippet": "", "published": None} for u in results]}
    runner.fetch_fn = lambda url, max_chars=2000: page(url, pages[url]) if url in pages else {"url": url, "final_url": url, "status": 404}


def test_a_second_independent_source_with_the_same_value_corroborates_and_is_recorded_verbatim(env):
    runner, rid = answered(env)
    agree = FILLER + "Acme said its Starter plan costs $29 per month for small teams. " + FILLER
    with_web(runner, {"https://acme.example/pricing": PAGE_TEXT, "https://news.example/acme-prices": agree},
             ["https://acme.example/pricing", "https://news.example/acme-prices"])
    out = Verifier(runner).start(rid)
    assert out["status"] == "finished" and out["counts"]["corroborated"] == 1
    [v] = env["store"].verifications_of_run(rid)
    checks = {c["check"]: c for c in v["checks"]}
    assert checks["source"]["result"] == "unchanged"
    assert checks["second_source"]["result"] == "found" and checks["second_source"]["url"] == "https://news.example/acme-prices"
    assert checks["second_source"]["quote"] == "Acme said its Starter plan costs $29 per month for small teams."
    assert checks["second_source"]["tier"] == 3                                       # an ordinary site: Tier 3, said so
    stored = env["store"].get_claim(checks["second_source"]["claim_id"])
    assert stored["quote"] == stored["claim_text"]                                    # word for word, like any claim


def test_another_source_giving_a_different_figure_is_a_contradiction(env):
    runner, rid = answered(env)
    other = FILLER + "Acme raised its Starter plan price to $39 per month this week. " + FILLER
    with_web(runner, {"https://acme.example/pricing": PAGE_TEXT, "https://news.example/acme-raises": other},
             ["https://news.example/acme-raises"])
    out = Verifier(runner).start(rid)
    assert out["counts"]["contradicted"] == 1
    checks = {c["check"]: c for c in env["store"].verifications_of_run(rid)[0]["checks"]}
    assert checks["contradiction"]["result"] == "found" and "$39" in checks["contradiction"]["quote"]


def test_a_quote_no_longer_on_its_page_is_a_contradiction_and_nothing_found_is_unconfirmed(env):
    runner, rid = answered(env)
    with_web(runner, {"https://acme.example/pricing": "Acme pricing has moved. " * 20}, [])
    out = Verifier(runner).start(rid)
    assert out["counts"]["contradicted"] == 1
    checks = {c["check"]: c for c in env["store"].verifications_of_run(rid)[0]["checks"]}
    assert checks["source"]["result"] == "quote gone"

    runner2, rid2 = answered(env)
    with_web(runner2, {"https://acme.example/pricing": PAGE_TEXT}, [])
    assert Verifier(runner2).start(rid2)["counts"]["unconfirmed"] == 1


def test_verification_can_be_paused_resumed_and_stopped_between_claims(env):
    runner, rid = answered(env)
    with_web(runner, {"https://acme.example/pricing": PAGE_TEXT}, [])
    v = Verifier(runner)
    import uuid
    vid = uuid.uuid4().hex
    env["runs"].emit(vid, "api", "pause_requested", {})
    paused = v.start(rid, verify_id=vid)
    assert paused["status"] == "paused" and env["store"].verifications_of_run(rid) == []
    done = v.resume(vid)
    assert done["status"] == "finished" and len(env["store"].verifications_of_run(rid)) == 1

    vid2 = uuid.uuid4().hex
    env["runs"].emit(vid2, "api", "stop_requested", {})
    stopped = v.start(rid, verify_id=vid2)
    assert stopped["status"] == "stopped" and env["runs"].status(vid2) == "stopped"


def test_a_persons_review_is_kept_with_the_claim_and_only_known_verdicts_are_accepted(env):
    runner, rid = answered(env)
    store = env["store"]
    store.record_review(1, "confirmed", "checked against the page", rid)
    store.record_review(1, "wrong", "the price changed today", rid)
    assert store.latest_reviews([1])[1]["verdict"] == "wrong"                       # the newest mark is the current one
    assert [r["verdict"] for r in store.claim_detail(1)["reviews"]] == ["confirmed", "wrong"]   # both kept
    with pytest.raises(ValueError):
        store.record_review(1, "looks fine")


def test_the_api_records_reviews_starts_verification_and_reports_it(env, tmp_path):
    from tests.test_run_control import H, api
    client, jobs, runner = api(env, tmp_path)
    rid = jobs.start_research("What does Acme charge?", "Acme", {"max_iterations": 1}, False, require_approval=False, ask_sources=False)
    assert client.get(f"/api/runs/{rid}", headers=H).json()["status"] == "finished"
    r = client.post("/api/evidence/claims/1/review", headers=H, json={"verdict": "confirmed", "note": "matches the page", "run_id": rid})
    assert r.status_code == 201 and r.json()["latest"]["verdict"] == "confirmed"
    assert client.post("/api/evidence/claims/1/review", headers=H, json={"verdict": "meh"}).status_code == 422
    report = client.get(f"/api/runs/{rid}", headers=H).json()["report"]
    assert report["established"][0]["review"]["verdict"] == "confirmed"           # shown with the claim

    with_web(runner, {"https://acme.example/pricing": PAGE_TEXT}, [])
    vid = client.post(f"/api/runs/{rid}/verify", headers=H, json={}).json()["verify_run_id"]
    got = client.get(f"/api/runs/{rid}/verification", headers=H).json()
    assert got["verify_run_id"] == vid and got["status"] == "finished" and got["results"][0]["outcome"] == "unconfirmed"
    verify = client.get(f"/api/runs/{vid}", headers=H).json()
    assert verify["kind"] == "verify" and verify["of_run"] == rid and verify["results"]


def revenue_run(env):
    from runner import Plan
    from tests.scripted import ExtractedClaim, Extraction
    quote = "Acme reported revenue of $3,678 million for the third quarter of 2026."
    text = FILLER + quote + " " + FILLER
    runner = make_runner(env, Plan=lambda p: Plan(subject="Acme", company="Acme", queries=["acme q3 revenue"],
                                                  checklist=["Acme's revenue for the third quarter of 2026"]),
                         Extraction=lambda p: Extraction(claims=[ExtractedClaim(quote=quote, value="$3,678 million")]))
    runner.fetch_fn = lambda url, max_chars=2000: page(url, text)
    result = runner.start("What was Acme's revenue for the third quarter of 2026?", budgets={"max_iterations": 1})
    assert result["report"]["established"]
    return runner, result["run_id"], text


@pytest.mark.parametrize("theirs, outcome", [
    ("Acme posted revenue of $3.68 billion in the third quarter of 2026, the company said.", "corroborated"),   # the same amount, written otherwise
    ("Acme posted revenue of $3.9 billion in the third quarter of 2026, the company said.", "contradicted"),
])
def test_amounts_are_compared_as_amounts_not_as_text(env, theirs, outcome):
    runner, rid, own = revenue_run(env)
    with_web(runner, {"https://acme.example/pricing": own, "https://news.example/acme-q3": FILLER + theirs + " " + FILLER},
             ["https://news.example/acme-q3"])
    assert Verifier(runner).start(rid)["counts"][outcome] == 1
