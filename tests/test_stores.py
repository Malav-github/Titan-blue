"""Archiving the evidence store and starting a new one: nothing is deleted, the archive is checked before the live store is
emptied, and archived evidence stays readable (runs, reports, claims with their quotes in context)."""

import json
import os
import stat

import pytest

import stores
from evidence import EvidenceStore
from tests.test_runner import env, make_runner  # noqa: F401


@pytest.fixture
def live(env, tmp_path):
    env["ckpt"] = str(tmp_path / "checkpoints.db")          # where the store layout expects the saved run states
    layout = stores.Layout(tmp_path, tmp_path / "evidence.db")
    yield env, layout
    for p in tmp_path.rglob("*"):                           # archived files are read-only; let the test folder be removed
        if p.is_file():
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)


def finished_run(env):
    runner = make_runner(env)
    result = runner.start("What does Acme charge?")
    assert result["report"]["established"]
    return runner, result


def test_archiving_keeps_everything_readable_and_starts_an_empty_store(live):
    env, layout = live
    runner, result = finished_run(env)
    claim_id = result["report"]["established"][0]["claim_id"]
    before = env["store"].counts()

    m = stores.archive("Before ontologies", reason="testing", by="test", layout=layout)

    assert m["id"].endswith("-before-ontologies") and m["runs"] == 1 and m["audit"]["problems"] == 0
    assert m["counts"]["evidence"]["claims"] == before["claims"] and m["folders"]["files"]["files"] > 0
    # the live store is new and empty, and still works
    assert all(n == 0 for n in env["store"].counts().values())
    assert env["runs"].runs() == [] and not any(layout.folders["files"].iterdir())
    again = runner.start("What does Acme charge?")
    assert again["report"]["established"]
    # the archive: listed, read-only, and readable through the same calls as the live store
    assert [a["id"] for a in stores.list_archives(layout)] == [m["id"]]
    old = stores.ArchivedStore(m["id"], layout)
    assert [r["run_id"] for r in old.runs.runs()] == [result["run_id"]] and old.runs.status(result["run_id"]) == "finished"
    assert old.report(result["run_id"])["established"][0]["claim_id"] == claim_id
    detail = old.evidence.claim_detail(claim_id)
    assert detail["quote"] and "$29" in detail["quote"] + detail["after"] + detail["before"]
    assert old.verify()["unchanged"]
    assert not os.access(old.dir / "evidence.db", os.W_OK)
    with pytest.raises(Exception):
        old.evidence.add_entity("Someone")
    log = stores.read_log(layout)
    assert log[-1]["action"] == "archive" and log[-1]["id"] == m["id"] and log[-1]["by"] == "test"
    assert stores.preview(layout)["since"] == log[-1]["at"]


def test_a_running_job_stops_the_archive_and_nothing_changes(live):
    env, layout = live
    finished_run(env)
    env["runs"].emit("busy1", "api", "queued", {"kind": "research", "question": "Still going?"})
    with pytest.raises(stores.StoreError, match="queued"):
        stores.archive(layout=layout)
    with pytest.raises(stores.StoreError, match="capability"):
        stores.archive(layout=layout, extra_blockers=["a capability request is researching"])
    assert env["store"].counts()["claims"] > 0 and stores.list_archives(layout) == []


def test_a_run_cut_off_long_ago_does_not_block_and_is_closed_in_its_own_log(live, monkeypatch):
    env, layout = live
    finished_run(env)
    env["runs"].emit("old1", "api", "queued", {"kind": "research", "question": "Cut off by a restart"})
    env["runs"].emit("old1", "runner", "run_started", {"question": "Cut off by a restart"})
    later = stores._now() + stores.timedelta(minutes=stores.STALE_AFTER_MINUTES + 5)
    monkeypatch.setattr(stores, "_now", lambda: later)
    p = stores.preview(layout)
    assert p["blockers"] == [] and [r["run_id"] for r in p["abandoned"]] == ["old1"]
    m = stores.archive(layout=layout)
    assert [r["run_id"] for r in m["abandoned_runs_closed"]] == ["old1"]
    old = stores.ArchivedStore(m["id"], layout)
    assert old.runs.status("old1") == "failed" and "abandoned" in old.runs.events("old1")[-1]["payload"]["error"]


def test_an_archive_that_does_not_check_out_is_undone(live, monkeypatch):
    env, layout = live
    finished_run(env)
    counts = env["store"].counts()
    files = sorted(p.name for p in layout.folders["files"].iterdir())
    real = EvidenceStore.audit
    monkeypatch.setattr(EvidenceStore, "audit", lambda self: real(self) + (["claim 1: quote no longer verifies"] if self.readonly else []))
    with pytest.raises(stores.StoreError, match="audit of the archive"):
        stores.archive(layout=layout)
    assert env["store"].counts() == counts and sorted(p.name for p in layout.folders["files"].iterdir()) == files
    assert not layout.archives.exists() or not any(layout.archives.iterdir())
    assert env["store"].audit() == []


def test_an_unfinished_run_is_archived_as_it_stands_and_listed_in_the_manifest(live):
    env, layout = live
    finished_run(env)
    env["runs"].emit("wait1", "api", "queued", {"kind": "research", "question": "Waiting for you"})
    env["runs"].emit("wait1", "runner", "awaiting_approval", {})
    m = stores.archive(layout=layout)
    assert [r["run_id"] for r in m["unfinished_runs"]] == ["wait1"]


def test_an_archive_id_cannot_reach_outside_the_archives_folder(live):
    _, layout = live
    for bad in ("..", "../data", "20261107-101500/../../x", ""):
        with pytest.raises(KeyError):
            stores.ArchivedStore(bad, layout)


def test_reloading_definitions_forgets_what_was_read():
    import ontology
    ontology.sources()
    assert ontology.sources.cache_info().currsize == 1
    cleared = stores.reload_definitions()
    assert "ontology.sources" in cleared and ontology.sources.cache_info().currsize == 0
    assert json.dumps(cleared)


def test_the_app_archives_the_store_and_serves_the_archived_evidence(live, tmp_path):
    from fastapi.testclient import TestClient
    from api.engine import Engine
    from api.jobs import Jobs
    from api.registry import Registry
    from api.server import create_app
    env, layout = live
    runner, result = finished_run(env)
    claim_id = result["report"]["established"][0]["claim_id"]
    engine = Engine(Registry.load(), env["store"], env["runs"], {}, connectors=env["registry"], uploads_dir=tmp_path / "uploads", background=False)
    jobs = Jobs(env["runs"], env["registry"], runner_factory=lambda: runner, background=False)
    c = TestClient(create_app(engine=engine, token="t", probe_map={}, store=env["store"], jobs=jobs, templates={}, dist_dir=None, layout=layout))
    H = {"x-titan-token": "t"}

    overview = c.get("/api/stores", headers=H).json()
    assert overview["live"]["runs"] == 1 and overview["live"]["blockers"] == [] and overview["archives"] == []
    assert c.post("/api/stores/archive", headers=H, json={"label": "first"}).status_code == 422       # not confirmed
    r = c.post("/api/stores/archive", headers=H, json={"label": "first", "reason": "new ontology", "confirm": True})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]

    assert c.get("/api/evidence/claims", headers=H).json() == [] and c.get("/api/runs", headers=H).json() == []
    assert [x["id"] for x in c.get("/api/stores", headers=H).json()["archives"]] == [aid]
    assert [x["run_id"] for x in c.get(f"/api/archives/{aid}", headers=H).json()["runs"]] == [result["run_id"]]
    run = c.get(f"/api/archives/{aid}/runs/{result['run_id']}", headers=H).json()
    assert run["status"] == "finished" and run["archive"] == aid and run["report"]["established"][0]["claim_id"] == claim_id
    assert c.get(f"/api/archives/{aid}/runs/{result['run_id']}/events", headers=H).json()
    assert c.get(f"/api/archives/{aid}/evidence/claims", headers=H).json()[0]["claim_id"]
    assert c.get(f"/api/archives/{aid}/evidence/claims/{claim_id}", headers=H).json()["quote"]
    assert c.post(f"/api/archives/{aid}/verify", headers=H).json()["unchanged"]
    assert c.get("/api/archives/nope", headers=H).status_code == 404
    assert c.get(f"/api/archives/{aid}/evidence/claims/999999", headers=H).status_code == 404
    assert "ontology.sources" in c.post("/api/maintenance/reload", headers=H).json()["cleared"]
