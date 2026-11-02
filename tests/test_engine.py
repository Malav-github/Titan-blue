import pytest
from fastapi.testclient import TestClient

from api import executors as ex
from api.engine import Engine, RunError
from api.registry import BlockDef, PortType, Registry
from api.server import create_app
from evidence import EvidenceStore
from runs import RunStore
from tools.archive import archive_bytes

PAGE = ("<html><body><article><h1>Acme Pricing</h1>"
        "<p>The Starter plan costs $29 per month, billed annually.</p></article></body></html>")
QUOTE = "The Starter plan costs $29 per month, billed annually."
TOKEN = "t"
H = {"x-titan-token": TOKEN}


def demo(block_id, group="find", inputs=(("text", "Text", True),), outputs=(("text", "Text"),), ref=None, settings=None):
    return BlockDef(
        id=block_id, version=1, kind="capability", group=group, title=block_id, summary="s",
        inputs=[{"name": n, "type": t, "required": r} for n, t, r in inputs],
        outputs=[{"name": n, "type": t} for n, t in outputs],
        settings=settings or {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2}}},
        badges={}, executor={"type": "python", "ref": ref or f"api.executors:{block_id.replace('.', '_')}"})


TYPES = [PortType(name="Text", widget="t", label="Text"), PortType(name="Json", widget="j", label="Json"),
         PortType(name="Claim", widget="c", label="Claim"), PortType(name="Claims", widget="cs", label="Claims"),
         PortType(name="Source", widget="s", label="Source")]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "archive")
    return EvidenceStore(tmp_path / "e.db")


def make_engine(store, tmp_path, executors, blocks, probes=None, registry=None):
    return Engine(registry or Registry(TYPES, blocks), store, RunStore(str(tmp_path / "runs.db")), probes or {},
                  llm=lambda *a, **k: None, uploads_dir=tmp_path / "uploads", executors=executors, background=False)


def events(engine, run_id):
    return [e["type"] for e in engine.runs.events(run_id)]


def echo(ctx, inputs, settings):
    return {"text": inputs["text"].upper()}


APPROVE = demo("demo.gate", "control", inputs=(("subject", "Json", True),), outputs=(("decision", "Json"),),
               ref="api.executors:approve")


# --- one path for single blocks and graphs ---

def test_single_block_run_records_the_whole_history(store, tmp_path):
    e = make_engine(store, tmp_path, {"api.executors:demo_echo": echo}, [demo("demo.echo")])
    graph, inputs = e.single_block_graph("demo.echo", {}, {"text": "hello"})
    rid = e.start(graph, inputs, "Echo")
    assert events(e, rid) == ["run_started", "node_started", "node_finished", "run_finished"]
    d = e.describe(rid)
    assert d["status"] == "finished" and d["outputs"] == {"n1": {"text": "HELLO"}}
    assert e.runs.runs()[0]["question"] == "Echo"


def test_chained_graph_passes_outputs_along(store, tmp_path):
    e = make_engine(store, tmp_path, {"api.executors:demo_echo": echo}, [demo("demo.echo")])
    graph = {"nodes": [{"id": "a", "block": "demo.echo"}, {"id": "b", "block": "demo.echo"}],
             "edges": [{"from": "a.text", "to": "b.text"}]}
    rid = e.start(graph, {"a.text": "hi"}, "Chain")
    assert e.describe(rid)["outputs"] == {"b": {"text": "HI"}}


def test_failure_is_recorded_and_stops_the_run(store, tmp_path):
    def boom(ctx, inputs, settings):
        raise RuntimeError("nope")
    e = make_engine(store, tmp_path, {"api.executors:demo_echo": boom}, [demo("demo.echo")])
    rid = e.start(*e.single_block_graph("demo.echo", {}, {"text": "x"}), "Boom")
    assert events(e, rid)[-2:] == ["node_failed", "run_failed"]
    assert e.describe(rid)["status"] == "failed"


# --- validation refuses before anything runs ---

def test_validation_lists_every_problem(store, tmp_path):
    e = make_engine(store, tmp_path, {"api.executors:demo_echo": echo}, [demo("demo.echo")])
    graph, _ = e.single_block_graph("demo.echo", {"n": 99, "bogus": 1}, {})
    with pytest.raises(RunError) as err:
        e.start(graph, {"n1.extra": "x"}, "T")
    text = " | ".join(err.value.problems)
    for expected in ("unknown setting 'bogus'", "n1.text is required", "unknown input 'n1.extra'", "greater than the maximum of 5"):
        assert expected in text, expected
    assert e.runs.runs() == []                       # nothing was logged


def test_type_mismatch_cycle_and_double_wiring_are_refused(store, tmp_path):
    blocks = [demo("demo.echo"), demo("demo.claims", outputs=(("claims", "Claims"),)),
              demo("demo.takes_claim", inputs=(("claim", "Claim", True),))]
    e = make_engine(store, tmp_path, {}, blocks)
    with pytest.raises(RunError, match="cannot connect"):
        e.plan({"nodes": [{"id": "a", "block": "demo.claims"}, {"id": "b", "block": "demo.takes_claim"}],
                "edges": [{"from": "a.claims", "to": "b.claim"}]}, {"a.text": "x"})
    with pytest.raises(RunError, match="cycle"):
        e.plan({"nodes": [{"id": "a", "block": "demo.echo"}, {"id": "b", "block": "demo.echo"}],
                "edges": [{"from": "a.text", "to": "b.text"}, {"from": "b.text", "to": "a.text"}]}, {})
    with pytest.raises(RunError, match="more than one"):
        e.plan({"nodes": [{"id": "a", "block": "demo.echo"}, {"id": "b", "block": "demo.echo"}, {"id": "c", "block": "demo.echo"}],
                "edges": [{"from": "a.text", "to": "c.text"}, {"from": "b.text", "to": "c.text"}]}, {"a.text": "x", "b.text": "y"})


def test_unavailable_or_unwired_blocks_are_refused(store, tmp_path):
    needy = demo("demo.echo")
    needy.badges.needs = ["ollama"]
    e = make_engine(store, tmp_path, {"api.executors:demo_echo": echo}, [needy], probes={"ollama": lambda: "Ollama is down"})
    with pytest.raises(RunError, match="Ollama is down"):
        e.start(*e.single_block_graph("demo.echo", {}, {"text": "x"}), "T")
    e2 = make_engine(store, tmp_path, {}, [demo("demo.echo")])
    with pytest.raises(RunError, match="cannot be run from the engine yet"):
        e2.start(*e2.single_block_graph("demo.echo", {}, {"text": "x"}), "T")


def test_every_shipped_block_is_wired_or_explicitly_pending():
    refs = {b.executor.ref for b in Registry.load().list_latest()}
    assert refs == set(ex.EXECUTORS) | ex.ENGINE_HANDLED | ex.NOT_WIRED_YET


# --- integrity: claims and sources cannot be forged ---

def test_claims_and_sources_must_be_real_references(store, tmp_path):
    e = make_engine(store, tmp_path, {}, [], registry=Registry.load())
    for bad in ({"claim_id": 999}, "a claim written as text", {"claim_id": True}, {}):
        with pytest.raises(ValueError):
            e.check_value("Claim", bad)
    with pytest.raises(ValueError):
        e.check_value("Source", {"sha256": "0" * 64})
    with pytest.raises(ValueError):
        e.check_value("File", {"upload_id": "../../etc/passwd"})


def test_a_block_cannot_emit_a_forged_claim(store, tmp_path):
    def forge(ctx, inputs, settings):
        return {"claims": [{"claim_id": 12345}]}
    e = make_engine(store, tmp_path, {"api.executors:demo_claims": forge}, [demo("demo.claims", outputs=(("claims", "Claims"),))])
    rid = e.start(*e.single_block_graph("demo.claims", {}, {"text": "x"}), "Forge")
    assert e.describe(rid)["status"] == "failed"


# --- approvals survive a restart ---

def approval_graph():
    return {"nodes": [{"id": "a", "block": "demo.echo"}, {"id": "gate", "block": "demo.gate"}, {"id": "b", "block": "demo.echo"}],
            "edges": [{"from": "a.text", "to": "b.text"}]}


def approval_engine(store, tmp_path):
    return make_engine(store, tmp_path, {"api.executors:demo_echo": echo},
                       [demo("demo.echo"), APPROVE, demo("demo.json", outputs=(("j", "Json"),))])


def test_run_waits_for_approval_then_continues_after_restart(store, tmp_path):
    e = approval_engine(store, tmp_path)
    rid = e.start(approval_graph(), {"a.text": "hi", "gate.subject": {"what": "plan"}}, "Gated")
    assert e.describe(rid)["status"] == "awaiting_approval"
    assert "node_finished" in events(e, rid) and e.describe(rid)["outputs"] is None

    e2 = approval_engine(store, tmp_path)                       # a new process: only the run log carries over
    e2.resume(rid, True, "looks fine")
    d = e2.describe(rid)
    assert d["status"] == "finished" and d["outputs"]["b"] == {"text": "HI"}
    assert events(e2, rid).count("node_started") == 2           # nothing ran twice


def test_rejection_stops_the_run_and_double_decisions_are_refused(store, tmp_path):
    e = approval_engine(store, tmp_path)
    rid = e.start(approval_graph(), {"a.text": "hi", "gate.subject": {}}, "Gated")
    e.resume(rid, False, "no")
    assert e.describe(rid)["status"] == "stopped"
    with pytest.raises(RunError, match="not waiting"):
        e.resume(rid, True)


# --- real executors on local data ---

def test_uploaded_file_becomes_a_user_provided_source(store, tmp_path):
    e = make_engine(store, tmp_path, ex.EXECUTORS, [], registry=Registry.load())
    f = ex.save_upload(tmp_path / "uploads", "../../notes.txt", f"Internal note. {QUOTE} End.".encode())
    assert f["name"] == "notes.txt"
    rid = e.start(*e.single_block_graph("evidence.ingest_file", {}, {"file": f}), "Read")
    src = e.describe(rid)["outputs"]["n1"]["source"]
    assert src["url"] == "upload://notes.txt" and QUOTE in src["preview"]
    eid = store.add_entity("Acme")
    claim = store.add_claim(eid, "pricing.starter", "$29", "Starter costs $29 per month.", src["sha256"], QUOTE)
    assert claim["ok"] and claim["source_tier"] == "user_provided"


def test_upload_limits(tmp_path):
    with pytest.raises(ValueError):
        ex.save_upload(tmp_path, "a.txt", b"")
    with pytest.raises(ValueError):
        ex.save_upload(tmp_path, "a.txt", b"x" * (ex.MAX_UPLOAD_BYTES + 1))


class FakeLLM:
    def __init__(self, result):
        self.result = result

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        return self.result


def test_extraction_keeps_verified_quotes_and_logs_rejections(store, tmp_path):
    from runner import Extraction, ExtractedClaim
    good = ExtractedClaim(predicate="pricing.starter", value="$29", claim="The Starter plan costs $29 per month.",
                          quote=QUOTE, needed_fact=1)
    made_up = ExtractedClaim(predicate="pricing.team", value="$99", claim="The Team plan costs $99 per month.",
                             quote="The Team plan costs $99 per month, billed monthly.", needed_fact=1)
    e = make_engine(store, tmp_path, ex.EXECUTORS, [], registry=Registry.load(), probes={"ollama": lambda: None})
    e.llm = lambda *a, **k: FakeLLM(Extraction(claims=[good, made_up]))
    sha = store.ingest({"url": "https://acme.com/p", "final_url": "https://acme.com/p", "content_type": "text/html",
                        "encoding": "utf-8", "method": "http", "fetched_at": "2026-11-02T00:00:00+00:00",
                        **dict(zip(("sha256", "archive_path"), archive_bytes(PAGE.encode(), ".html")))})
    src = ex.source_ref(store, sha)
    rid = e.start(*e.single_block_graph("evidence.extract_claims", {}, {"source": src, "need": "Starter plan price", "subject": "Acme"}), "X")
    out = e.describe(rid)["outputs"]["n1"]["claims"]
    assert [c["quote"] for c in out] == [QUOTE]
    assert store.counts()["rejected_claims"] == 1
    assert "claim_rejected" in events(e, rid) and "claim_recorded" in events(e, rid)


# --- HTTP layer ---

@pytest.fixture
def http(store, tmp_path):
    e = make_engine(store, tmp_path, {"api.executors:demo_echo": echo}, [demo("demo.echo"), APPROVE,
                                                                       demo("demo.json", outputs=(("j", "Json"),))])
    return TestClient(create_app(e.registry, TOKEN, {}, store, engine=e)), e


def test_http_run_stream_and_errors(http):
    client, e = http
    r = client.post("/api/blocks/demo.echo/run", headers=H, json={"inputs": {"text": "hi"}})
    assert r.status_code == 202
    rid = r.json()["run_id"]
    assert client.get(f"/api/runs/{rid}", headers=H).json()["status"] == "finished"
    assert [x["type"] for x in client.get(f"/api/runs/{rid}/events", headers=H).json()][-1] == "run_finished"
    assert [x["type"] for x in client.get(f"/api/runs/{rid}/events?after=2", headers=H).json()] == ["node_finished", "run_finished"]
    stream = client.get(f"/api/runs/{rid}/stream", headers=H).text
    assert "event: run_started" in stream and "event: run_finished" in stream
    bad = client.post("/api/blocks/demo.echo/run", headers=H, json={"inputs": {}})
    assert bad.status_code == 422 and "n1.text is required" in bad.json()["detail"]["problems"]
    assert client.post("/api/blocks/none.such/run", headers=H, json={}).status_code == 404
    assert client.get("/api/runs/nope", headers=H).status_code == 404
    assert client.post(f"/api/runs/{rid}/decision", headers=H, json={"approved": True}).status_code == 409
    assert client.post("/api/blocks/demo.echo/run", json={"inputs": {"text": "x"}}).status_code == 401


def test_http_upload_then_ingest(store, tmp_path):
    e = make_engine(store, tmp_path, ex.EXECUTORS, [], registry=Registry.load())
    client = TestClient(create_app(e.registry, TOKEN, {}, store, engine=e))
    up = client.post("/api/uploads?filename=n.txt", headers=H, content=f"Memo. {QUOTE}".encode())
    assert up.status_code == 201
    run = client.post("/api/blocks/evidence.ingest_file/run", headers=H, json={"inputs": {"file": up.json()}})
    assert run.status_code == 202
    assert client.get(f"/api/runs/{run.json()['run_id']}", headers=H).json()["status"] == "finished"
    assert client.post("/api/uploads?filename=e.txt", headers=H, content=b"").status_code == 422
