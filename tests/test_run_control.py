"""Pausing, resuming and stopping a research run part-way (Runner.stop, RunStore.requested, the /pause /resume /stop API).

A pause takes effect at the next safe point: between steps, or inside a long step between model calls. What was done is
kept and a resume carries on from there. A stop skips what is left and writes the report from what was gathered."""

from runner import Plan, Selection
from tests.scripted import ExtractedClaim, Extraction
from tests.test_runner import PAGE_TEXT, env, make_runner, types  # noqa: F401
from tools.archive import archive_bytes, utc_now

PAGES = {
    "https://acme.example/pricing": PAGE_TEXT,
    "https://news.example/acme": ("Acme news. " * 10 + "Acme's Team plan costs $99 per month with priority support, the company said. "
                                  + "More news follows. " * 10),
}
STARTER = "The Starter plan costs $29 per month, billed annually."
TEAM = "Acme's Team plan costs $99 per month with priority support, the company said."


def two_page_runner(env, on_extract=None, **handlers):
    """Two pages to read, one claim on each: a pause between them lands inside the extraction step."""
    def fetch(url, max_chars=2000):
        env["world"].fetched.append(url)
        sha, path = archive_bytes(f"<html><body><article><p>{PAGES[url]}</p></article></body></html>".encode(), ".html")
        return {"url": url, "final_url": url, "status": 200, "content_type": "text/html", "encoding": "utf-8", "method": "http",
                "fetched_at": utc_now(), "sha256": sha, "archive_path": path, "text": "", "truncated": False}

    def extraction(prompt):
        if on_extract:
            on_extract(prompt)
        quote = STARTER if "Starter plan costs" in prompt else TEAM
        return Extraction(claims=[ExtractedClaim(predicate="pricing", quote=quote, value=quote.split()[4 if quote is STARTER else 5])])

    runner = make_runner(env, Plan=lambda p: Plan(subject="Acme", queries=["acme pricing"], checklist=["Starter plan price", "Team plan price"]),
                         Selection=lambda p: Selection(numbers=[1, 2]), Extraction=extraction, **handlers)
    runner.fetch_fn = fetch
    return runner


def test_a_pause_during_extraction_stops_between_model_calls_and_resume_carries_on(env):
    runs, asked = env["runs"], []

    def press_pause(prompt):                       # the person presses Pause while the first page is being read
        asked.append(prompt)
        if len(asked) == 1:
            runs.emit(rid[0], "api", "pause_requested", {})

    rid = []
    runner = two_page_runner(env, on_extract=press_pause)
    import uuid
    rid.append(uuid.uuid4().hex)
    paused = runner.start("What does Acme charge?", budgets={"max_iterations": 1}, run_id=rid[0])
    assert paused["status"] == "paused" and runs.status(rid[0]) == "paused"
    assert paused["next"] == ["extract"] and len(asked) == 1                # the second page was not sent to the model
    assert "extract_paused" in types(env, rid[0]) and env["store"].counts()["claims"] == 1

    done = runner.resume(rid[0])
    assert done["status"] == "finished" and len(asked) == 2                 # carried on with the second page only
    assert "extract_resumed" in types(env, rid[0])
    assert env["store"].counts()["claims"] == 2                             # nothing read twice, nothing lost
    assert sorted(r["quote"] for r in done["report"]["established"]) == sorted([STARTER, TEAM])


def test_a_pause_between_steps_is_kept_across_a_new_process(env):
    import uuid
    rid = uuid.uuid4().hex
    world = env["world"]
    plain = world.search

    def search(query, max_results=8):
        env["runs"].emit(rid, "api", "pause_requested", {})                # pressed while searching
        return plain(query, max_results)

    runner = make_runner(env)
    runner.search_fn = lambda query, max_results=8, **extra: search(query, max_results)
    paused = runner.start("What does Acme charge?", budgets={"max_iterations": 1}, run_id=rid)
    assert paused["status"] == "paused" and paused["next"] == ["select"] and world.fetched == []
    again = make_runner(env)                                                 # a fresh runner, as after a restart
    done = again.resume(rid)
    assert done["status"] == "finished" and done["report"]["established"]


def test_stop_during_extraction_writes_the_report_from_what_was_gathered(env):
    import uuid
    rid = uuid.uuid4().hex
    runner = two_page_runner(env, on_extract=lambda p: env["runs"].emit(rid, "api", "stop_requested", {}))
    result = runner.start("What does Acme charge?", budgets={"max_iterations": 1}, run_id=rid)
    assert result["status"] == "stopped" and env["runs"].status(rid) == "stopped"
    report = result["report"]
    assert report["stopped_early"] and any("Stopped by you" in w for w in report["warnings"])
    assert env["store"].counts()["claims"] == 1                              # the second page was never read
    assert "Team plan price" in report["gaps"]
    stopped = next(e["payload"] for e in env["runs"].events(rid) if e["type"] == "run_stopped")
    assert "stopped by you" in stopped["reason"]


def test_a_paused_run_can_be_stopped_and_still_reports(env):
    import uuid
    rid = uuid.uuid4().hex
    asked = []
    runner = two_page_runner(env, on_extract=lambda p: asked.append(p) or (len(asked) == 1 and env["runs"].emit(rid, "api", "pause_requested", {})))
    assert runner.start("What does Acme charge?", budgets={"max_iterations": 1}, run_id=rid)["status"] == "paused"
    env["runs"].emit(rid, "api", "stop_requested", {})
    result = runner.stop(rid)
    assert result["status"] == "stopped" and result["report"]["stopped_early"] and len(asked) == 1


def test_a_run_waiting_for_approval_can_be_stopped_without_searching(env):
    runner = make_runner(env)
    waiting = runner.start("What does Acme charge?", require_approval=True)
    assert waiting["status"] == "awaiting_approval"
    env["runs"].emit(waiting["run_id"], "api", "stop_requested", {})
    result = runner.stop(waiting["run_id"])
    assert result["status"] == "stopped" and env["world"].queries == [] and result["report"]["stopped_early"]


def test_resuming_clears_a_pause_request(env):
    runs = env["runs"]
    runs.emit("r1", "api", "pause_requested", {})
    assert runs.requested("r1") == "pause" and runs.status("r1") == "pausing"
    runs.emit("r1", "runner", "resumed", {})
    assert runs.requested("r1") is None
    runs.emit("r1", "api", "stop_requested", {})
    assert runs.requested("r1") == "stop" and runs.status("r1") == "stopping"


# --- the API ------------------------------------------------------------------------------------------------------------

def api(env, tmp_path):
    from fastapi.testclient import TestClient
    from api.engine import Engine
    from api.jobs import Jobs
    from api.registry import Registry
    from api.server import create_app
    from api.templates import load_templates
    runner = make_runner(env)
    engine = Engine(Registry.load(), env["store"], env["runs"], {}, connectors=env["registry"], uploads_dir=tmp_path / "up", background=False)
    jobs = Jobs(env["runs"], env["registry"], runner_factory=lambda: runner, background=False)
    app = create_app(engine=engine, token="t", probe_map={}, store=env["store"], jobs=jobs, templates=load_templates(), dist_dir=None)
    return TestClient(app), jobs, runner


H = {"x-titan-token": "t"}


def test_the_api_stops_a_waiting_run_and_returns_its_report(env, tmp_path):
    client, jobs, _ = api(env, tmp_path)
    rid = jobs.start_research("What does Acme charge?", "Acme", {"max_iterations": 1}, False, require_approval=True)
    assert client.get(f"/api/runs/{rid}", headers=H).json()["status"] == "awaiting_approval"
    assert client.post(f"/api/runs/{rid}/pause", headers=H).status_code == 409        # not running: nothing to pause
    r = client.post(f"/api/runs/{rid}/stop", headers=H)
    assert r.status_code == 202 and r.json()["status"] == "stopping"
    run = client.get(f"/api/runs/{rid}", headers=H).json()
    assert run["status"] == "stopped" and run["report"]["stopped_early"]
    assert client.post(f"/api/runs/{rid}/stop", headers=H).status_code == 409         # already stopped
    assert client.post(f"/api/runs/{rid}/resume", headers=H).status_code == 409


def test_the_api_resumes_a_paused_run_and_a_pause_can_be_taken_back(env, tmp_path):
    client, jobs, runner = api(env, tmp_path)
    rid = jobs.start_research("What does Acme charge?", "Acme", {"max_iterations": 1}, False, require_approval=False)
    assert client.post(f"/api/runs/{rid}/pause", headers=H).status_code == 409        # it already finished
    other = "r-pausing"
    env["runs"].emit(other, "api", "queued", {"kind": "research", "question": "q"})
    env["runs"].emit(other, "runner", "run_started", {"question": "q"})
    assert client.post(f"/api/runs/{other}/pause", headers=H).json()["status"] == "pausing"
    assert client.post(f"/api/runs/{other}/resume", headers=H).json()["status"] == "running"   # changed your mind
    assert env["runs"].requested(other) is None

    asked = []

    def press_pause_once(p):
        asked.append(p)
        if len(asked) == 1:
            env["runs"].emit(rid2, "api", "pause_requested", {})
    paused_runner = two_page_runner(env, on_extract=press_pause_once)
    jobs._runner = paused_runner
    rid2 = "r-two-pages"
    jobs._start_research(rid2, "What does Acme charge?", "Acme", {"max_iterations": 1}, False, False, ask_sources=False)
    assert client.get(f"/api/runs/{rid2}", headers=H).json()["status"] == "paused"
    assert client.post(f"/api/runs/{rid2}/resume", headers=H).status_code == 202
    assert client.get(f"/api/runs/{rid2}", headers=H).json()["status"] == "finished" and len(asked) == 2
