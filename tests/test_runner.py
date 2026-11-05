import sqlite3

import pytest

from connectors.registry import ConnectorRegistry
from evidence import EvidenceStore
from runner import (Extraction, ExtractedClaim, Advice, FollowUps, Plan, PremiseList, PremiseScreen, Relevance, Runner, Selection, Summary, top_chunks)
from runs import RunStore
from support import SupportVerdict
from tools.archive import archive_bytes, utc_now

PAGE_TEXT = ("Acme pricing overview. " * 12 + "The Starter plan costs $29 per month, billed annually. "
             "The Team plan costs $99 per month with priority support. " + "More details follow. " * 10)
GOOD_QUOTE = "The Starter plan costs $29 per month, billed annually."


class ScriptedLLM:
    """Stands in for a chat model: with_structured_output(schema).invoke(prompt) -> handlers[schema.__name__](prompt)."""
    def __init__(self, handlers):
        self.handlers, self.model = handlers, "scripted"

    def with_structured_output(self, schema):
        handler = self.handlers[schema.__name__]

        class Call:
            def invoke(self_, prompt):
                out = handler(prompt)
                if isinstance(out, Exception):
                    raise out
                return out
        return Call()


def default_handlers(**override):
    handlers = {
        "Plan": lambda p: Plan(subject="Acme", queries=["acme pricing"], checklist=["Starter plan price"]),
        "PremiseList": lambda p: PremiseList(premise=[]),
        "PremiseScreen": lambda p: PremiseScreen(reported=[1], reason="the first result reports it"),
        "Advice": lambda p: Advice(),
        "Selection": lambda p: Selection(numbers=[1, 99]),  # 99 does not exist and must be ignored
        "Extraction": lambda p: Extraction(claims=[
            ExtractedClaim(predicate="pricing.starter", value="$29", claim="Acme's Starter plan costs $29 per month.", quote=GOOD_QUOTE, needed_fact=1),
            ExtractedClaim(predicate="pricing.team", value="$89", claim="The Team plan costs $89.", quote="The Team plan costs $89 per month with priority support.", needed_fact=1),
        ]),
        "SupportVerdict": lambda p: SupportVerdict(verdict="supported", reasoning="stated directly"),
        "Relevance": lambda p: Relevance(addresses=True, reason="states the price"),
        "FollowUps": lambda p: FollowUps(new_queries=["acme starter plan price official"]),
        "Summary": lambda p: Summary(text="Starter costs $29 [claim:1]. Another [claim:999]."),
    }
    handlers.update(override)
    return handlers


class World:
    """Fake web: one search result page, one fetchable page."""
    def __init__(self):
        self.queries, self.fetched, self.search_error = [], [], None

    def search(self, query, max_results=8):
        if self.search_error:
            raise self.search_error
        self.queries.append(query)
        return {"search_id": "s1", "providers": [{"provider": "fake", "ok": True}], "warnings": [],
                "results": [{"url": "https://acme.example/pricing", "title": "Acme pricing", "snippet": "plans", "score": 0.9},
                            {"url": "https://news.example/acme", "title": "Acme news", "snippet": "news", "score": 0.5}]}

    def fetch(self, url, max_chars=2000):
        self.fetched.append(url)
        sha, path = archive_bytes(f"<html><body><article><p>{PAGE_TEXT}</p></article></body></html>".encode(), ".html")
        return {"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8",
                "method": "http", "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr("runner.tracing.callbacks", lambda: [])
    return {"store": EvidenceStore(tmp_path / "evidence.db"), "runs": RunStore(str(tmp_path / "runs.db")),
            "ckpt": str(tmp_path / "ckpt.db"), "world": World(), "registry": ConnectorRegistry(tmp_path / "connectors.db")}


def make_runner(env, scout=False, **handler_overrides):
    runner = _make_runner(env, scout, **handler_overrides)
    runner.network_workers = 1          # one at a time: tests count calls in order
    return runner


def _make_runner(env, scout=False, **handler_overrides):
    return Runner(scout=scout, store=env["store"], runs=env["runs"], checkpoint_path=env["ckpt"], registry=env["registry"],
                  llm_factory=lambda task: ScriptedLLM(default_handlers(**handler_overrides)),
                  search_fn=env["world"].search, fetch_fn=env["world"].fetch)


def types(env, run_id):
    return [e["type"] for e in env["runs"].events(run_id)]


# --- the full pipeline ----------------------------------------------------------------------------

def test_full_run_keeps_only_verified_supported_claims(env):
    result = make_runner(env).start("What does Acme charge?")
    assert result["status"] == "finished"
    report = result["report"]
    assert [r["claim_id"] for r in report["established"]] == [1]
    assert report["established"][0]["quote"] == GOOD_QUOTE
    assert report["rejected_quotes"] == 1                       # the invented $89 quote never became a claim
    assert env["store"].counts()["claims"] == 1
    assert "[claim:999]" not in report["summary"] and "invalid citation removed" in report["summary"]
    assert report["invalid_citations_removed"] == [999]
    assert report["gaps"] == []
    assert env["store"].audit() == []
    assert env["world"].fetched == ["https://acme.example/pricing"]  # model's bogus choice #99 was ignored


def test_event_log_tells_the_story_in_order(env):
    run_id = make_runner(env).start("q")["run_id"]
    seq = types(env, run_id)
    for earlier, later in [("run_started", "plan_ready"), ("plan_ready", "search_done"), ("search_done", "source_fetched"),
                           ("source_fetched", "claim_recorded"), ("claim_recorded", "support_checked"),
                           ("support_checked", "coverage_assessed"), ("coverage_assessed", "report_ready"),
                           ("report_ready", "run_finished")]:
        assert seq.index(earlier) < seq.index(later), seq
    assert "claim_rejected" in seq and env["runs"].status(run_id) == "finished"


def test_run_events_are_append_only(env):
    run_id = make_runner(env).start("q")["run_id"]
    db = sqlite3.connect(env["runs"].path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE run_events SET type = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM run_events")
    db.close()
    assert env["runs"].runs()[0]["run_id"] == run_id


# --- approval, pause and resume --------------------------------------------------------------------

def test_approval_pauses_before_any_search_and_accepts_edits(env):
    runner = make_runner(env)
    result = runner.start("q", require_approval=True)
    assert result["status"] == "awaiting_approval" and result["approval_request"]["queries"] == ["acme pricing"]
    assert env["world"].queries == []                            # nothing searched yet
    done = runner.resume(result["run_id"], {"approved": True, "queries": ["acme starter plan price"]})
    assert done["status"] == "finished" and env["world"].queries == ["acme starter plan price"]


def test_rejected_plan_stops_the_run(env):
    runner = make_runner(env)
    run_id = runner.start("q", require_approval=True)["run_id"]
    assert runner.resume(run_id, {"approved": False})["status"] == "stopped"
    assert env["world"].queries == [] and env["store"].counts()["claims"] == 0


def test_pause_after_a_step_then_resume_from_a_new_process(env):
    first = make_runner(env)
    paused = first.start("q", pause_after=["fetch"])
    assert paused["status"] == "paused" and paused["next"] == ["api_lookup"]
    assert env["store"].counts()["claims"] == 0                  # extraction has not happened yet
    second = make_runner(env)                                    # a fresh Runner: state comes from the checkpoint
    done = second.resume(paused["run_id"])
    assert done["status"] == "finished" and done["report"]["established"]


def test_failed_step_is_retried_from_the_checkpoint(env):
    runner = make_runner(env)
    env["world"].search_error = ConnectionError("network down")
    failed = runner.start("q")
    assert failed["status"] == "failed" and "network down" in failed["error"]
    env["world"].search_error = None
    done = runner.resume(failed["run_id"])
    assert done["status"] == "finished"
    assert types(env, failed["run_id"]).count("plan_ready") == 1  # planning was not repeated


# --- guards around model behaviour -----------------------------------------------------------------

def test_planner_failure_falls_back_and_is_recorded(env):
    result = make_runner(env, Plan=lambda p: ValueError("bad json")).start("What does Acme charge?")
    assert result["status"] == "finished" and env["world"].queries == ["What does Acme charge?"]
    assert any("planner failed" in w for w in result["report"]["warnings"])
    assert "llm_failed" in types(env, result["run_id"])


def test_not_supported_claims_are_excluded_from_the_answer(env):
    result = make_runner(env, SupportVerdict=lambda p: SupportVerdict(verdict="not_supported", reasoning="no")).start("q")
    report = result["report"]
    assert report["established"] == [] and report["not_supported"] >= 1  # a 2nd round fetched another page
    assert report["gaps"] == ["Starter plan price"]              # nothing established, so it is reported as a gap


def test_loop_stops_at_the_iteration_budget_and_reports_gaps(env):
    result = make_runner(env, SupportVerdict=lambda p: SupportVerdict(verdict="not_supported", reasoning="no"), FollowUps=lambda p: FollowUps(new_queries=["another query"])).start(
        "q", budgets={"max_iterations": 2})
    assert result["status"] == "finished" and result["report"]["budget_used"]["iterations"] == 2
    assert result["report"]["gaps"] == ["Starter plan price"]
    assert len(env["world"].queries) == 2                        # one per round, never unbounded


def test_unreadable_pages_are_logged_not_used(env):
    env["world"].fetch = lambda url, max_chars=2000: {**World().fetch(url), "status": 403}
    runner = Runner(store=env["store"], runs=env["runs"], checkpoint_path=env["ckpt"], registry=env["registry"],
                    llm_factory=lambda task: ScriptedLLM(default_handlers()),
                    search_fn=env["world"].search, fetch_fn=env["world"].fetch)
    result = runner.start("q")
    assert "source_failed" in types(env, result["run_id"]) and result["report"]["established"] == []
    assert env["store"].counts()["sources"] == 0


def test_top_chunks_prefers_relevant_text():
    text = "filler " * 1500 + "the starter plan pricing is listed here " + "filler " * 1500
    best = top_chunks(text, {"starter", "pricing"}, 1)[0]
    assert "starter plan pricing" in best


def test_claims_that_address_no_needed_fact_are_dropped_and_cover_nothing(env):
    off_topic = Extraction(claims=[ExtractedClaim(predicate="x", value="$29", claim="The Starter plan costs $29.",
                                                  quote=GOOD_QUOTE, needed_fact=7)])
    result = make_runner(env, Extraction=lambda p: off_topic).start("q", budgets={"max_iterations": 1})
    assert env["store"].counts()["claims"] == 0 and result["report"]["gaps"] == ["Starter plan price"]
    reasons = [e["payload"].get("reason") for e in env["runs"].events(result["run_id"]) if e["type"] == "claim_rejected"]
    assert "does not address any needed fact" in reasons


def test_page_without_script_text_is_rendered_in_the_browser(env):
    world = env["world"]
    empty = lambda url, max_chars=2000: {**world.fetch(url), **_empty_page()}  # noqa: E731
    rendered = []

    def render(url, max_chars=2000):
        rendered.append(url)
        return world.fetch(url)

    runner = Runner(store=env["store"], runs=env["runs"], checkpoint_path=env["ckpt"], registry=env["registry"],
                    llm_factory=lambda task: ScriptedLLM(default_handlers()), search_fn=world.search,
                    fetch_fn=empty, render_fn=render)
    result = runner.start("q", budgets={"max_iterations": 1})
    assert rendered and "source_rendered" in types(env, result["run_id"])
    assert result["report"]["established"]


def _empty_page():
    sha, path = archive_bytes(b"<html><body>Enable JavaScript</body></html>", ".html")
    return {"sha256": sha, "archive_path": path}


def test_badly_filled_value_is_blanked_not_fatal_and_non_statements_are_dropped(env):
    sloppy = Extraction(claims=[
        ExtractedClaim(predicate="p", value="totally unrelated", claim="Acme's Starter plan costs $29 monthly.",
                       quote=GOOD_QUOTE, needed_fact=1),
        ExtractedClaim(predicate="p", value="x", claim="No claims", quote="The Team plan costs $99 per month with priority support.",
                       needed_fact=1),
    ])
    result = make_runner(env, Extraction=lambda p: sloppy).start("q", budgets={"max_iterations": 1})
    [row] = result["report"]["established"]
    assert row["claim_id"] == 1 and row["value"] == ""
    reasons = [e["payload"].get("reason") for e in env["runs"].events(result["run_id"]) if e["type"] == "claim_rejected"]
    assert "not a statement of a fact" in reasons


def test_true_but_off_topic_claims_do_not_count_as_established_or_covering(env):
    result = make_runner(env, Relevance=lambda p: Relevance(addresses=False, reason="about something else")).start(
        "q", budgets={"max_iterations": 1})
    report = result["report"]
    assert report["established"] == [] and report["off_topic"] >= 1
    assert report["gaps"] == ["Starter plan price"]


def test_remarks_about_the_passage_are_not_claims(env):
    remarks = Extraction(claims=[
        ExtractedClaim(predicate="p", value="", claim="The passage does not provide exact patent numbers.", quote=GOOD_QUOTE, needed_fact=1),
        ExtractedClaim(predicate="p", value="", claim="quote: EP3143075B1 active Active", quote=GOOD_QUOTE, needed_fact=1),
    ])
    result = make_runner(env, Extraction=lambda p: remarks).start("q", budgets={"max_iterations": 1})
    assert env["store"].counts()["claims"] == 0
