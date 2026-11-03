import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.registry import BlockDef, PortType, Registry, RegistryError
from api.server import create_app, hostname
from evidence import EvidenceStore

TOKEN = "test-token"
H = {"x-titan-token": TOKEN}


@pytest.fixture
def client(tmp_path):
    probes = {"ollama": lambda: None, "searxng": lambda: "SearXNG is down", "network": lambda: None}
    app = create_app(Registry.load(), TOKEN, probes, EvidenceStore(str(tmp_path / "e.db")))
    return TestClient(app)


def block_dict(**over):
    base = {
        "id": "demo.block", "version": 1, "kind": "capability", "group": "find", "title": "t", "summary": "s",
        "inputs": [{"name": "q", "type": "Text"}], "outputs": [{"name": "r", "type": "Json"}],
        "settings": {"type": "object", "properties": {}}, "badges": {},
        "executor": {"type": "python", "ref": "api.executors:demo"},
    }
    return {**base, **over}


# --- shipped definitions ---

def test_shipped_definitions_load_and_cover_the_spec():
    reg = Registry.load()
    ids = {b.id for b in reg.list_latest()}
    assert {"search.web", "web.fetch_url", "evidence.ingest_file", "evidence.check_support",
            "sandbox.python", "api.call", "model.chat", "control.approve", "research.run"} <= ids
    assert len(ids) == 17


def test_every_shipped_block_uses_a_known_group_and_has_unique_executor():
    blocks = Registry.load().list_latest()
    assert len({b.executor.ref for b in blocks}) == len(blocks)


def test_shipped_defaults_match_settings_schema():
    from jsonschema import validate
    for b in Registry.load().list_latest():
        for name, prop in b.settings.get("properties", {}).items():
            if "default" in prop:
                validate(prop["default"], prop)   # a default must satisfy its own constraints


# --- validation ---

def test_invalid_id_version_and_schema_are_rejected():
    with pytest.raises(ValidationError):
        BlockDef(**block_dict(id="NoNamespace"))
    with pytest.raises(ValidationError):
        BlockDef(**block_dict(version=0))
    with pytest.raises(Exception):
        BlockDef(**block_dict(settings={"type": "nonsense"}))


def test_duplicate_port_names_rejected():
    with pytest.raises(ValidationError):
        BlockDef(**block_dict(inputs=[{"name": "a", "type": "Text"}, {"name": "a", "type": "Text"}]))


def test_unknown_port_type_and_duplicate_block_rejected():
    types = [PortType(name="Text", widget="text", label="Text")]
    with pytest.raises(RegistryError, match="unknown port type"):
        Registry(types, [BlockDef(**block_dict(outputs=[{"name": "r", "type": "Nope"}]))])
    b = BlockDef(**block_dict(outputs=[{"name": "r", "type": "Text"}]))
    with pytest.raises(RegistryError, match="duplicate block"):
        Registry(types, [b, b])


def test_assignability_follows_parent_types():
    reg = Registry.load()
    assert reg.is_assignable("URL", "Text") and reg.is_assignable("Query", "Text")
    assert not reg.is_assignable("Text", "URL")
    assert not reg.is_assignable("Claims", "Claim")
    assert not reg.is_assignable("Text", "Claim")   # free text can never become a claim


def test_latest_version_wins():
    types = [PortType(name="Text", widget="text", label="Text")]
    v1 = BlockDef(**block_dict(inputs=[], outputs=[]))
    v2 = BlockDef(**block_dict(version=2, inputs=[], outputs=[]))
    assert Registry(types, [v1, v2]).latest("demo.block").version == 2


# --- security ---

def test_api_requires_token(client):
    assert client.get("/api/blocks").status_code == 401
    assert client.get("/api/blocks", headers={"x-titan-token": "wrong"}).status_code == 401
    assert client.get("/api/blocks", headers=H).status_code == 200


def test_foreign_host_header_is_refused(client):
    r = client.get("/api/blocks", headers={**H, "host": "evil.example.com"})
    assert r.status_code == 400


def test_hostname_parsing():
    assert hostname("localhost:8000") == "localhost"
    assert hostname("127.0.0.1") == "127.0.0.1"
    assert hostname("[::1]:8000") == "[::1]"
    assert hostname("evil.com:80") == "evil.com"


def test_docs_and_openapi_are_not_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)            # 404, or the app's own page when the frontend is built: never the API's schema or docs UI
        assert r.status_code in (200, 404)
        assert "swagger" not in r.text.lower() and '"openapi"' not in r.text and '"paths"' not in r.text


# --- endpoints ---

def test_blocks_report_availability_with_reasons(client):
    blocks = {b["id"]: b for b in client.get("/api/blocks", headers=H).json()}
    assert blocks["search.web"]["availability"]["available"] is False
    assert "SearXNG is down" in blocks["search.web"]["availability"]["reasons"]
    assert blocks["control.approve"]["availability"]["available"] is True
    # a need with no probe is reported, never assumed met
    reasons = blocks["sandbox.python"]["availability"]["reasons"]
    assert any("cannot check requirement 'docker'" in r for r in reasons)


def test_block_detail_and_404(client):
    r = client.get("/api/blocks/web.fetch_url", headers=H)
    assert r.status_code == 200 and r.json()["inputs"][0]["type"] == "URL"
    assert client.get("/api/blocks/nope.nothing", headers=H).status_code == 404


def test_port_types_endpoint(client):
    names = {t["name"] for t in client.get("/api/port-types", headers=H).json()}
    assert {"Text", "URL", "Source", "Claim", "Claims", "Verdict", "Model", "File", "Json"} <= names
    assert "Table" not in names


def test_models_endpoint_reports_task_assignment(client, monkeypatch):
    import api.server as server
    monkeypatch.setattr(server, "ollama_models", lambda: ["qwen3:8b"])
    monkeypatch.setattr(server.config, "TASK_MODELS", {"claim_support": "gemma4:e4b"})
    data = client.get("/api/models", headers=H).json()
    tasks = {t["task"]: t for t in data["tasks"]}
    assert tasks["default"]["installed"] is True
    assert tasks["claim_support"] == {"task": "claim_support", "model": "gemma4:e4b", "installed": False}


def test_models_endpoint_when_ollama_is_down(client, monkeypatch):
    import api.server as server
    monkeypatch.setattr(server, "ollama_models", lambda: None)
    data = client.get("/api/models", headers=H).json()
    assert data["ollama_reachable"] is False and data["installed"] == []


def test_health_includes_probe_results_and_evidence_audit(client):
    data = client.get("/api/health", headers=H).json()
    assert data["checks"]["ollama"] == {"ok": True, "detail": None}
    assert data["checks"]["searxng"]["ok"] is False
    assert data["evidence"]["audit_problems"] == [] and data["evidence"]["claims"] == 0
    assert "BRAVE_API_KEY" not in json.dumps(data)


def test_token_is_generated_and_persisted(tmp_path, monkeypatch):
    import api.server as server
    monkeypatch.setattr(server, "TOKEN_FILE", tmp_path / "t.txt")
    monkeypatch.delenv("TITAN_API_TOKEN", raising=False)
    first = server.load_token()
    assert len(first) >= 32 and server.load_token() == first
    monkeypatch.setenv("TITAN_API_TOKEN", "from-env")
    assert server.load_token() == "from-env"


def test_wsl_keep_alive_starts_a_sleeping_process_and_tolerates_a_missing_wsl():
    from api.wsl import keep_awake
    seen = []

    class Proc:
        def terminate(self):
            seen.append("terminated")

    started = keep_awake(popen=lambda cmd, **kw: seen.append(cmd) or Proc())
    assert seen[0] == ["wsl", "-e", "sleep", "infinity"]
    started.terminate()
    assert seen[-1] == "terminated"

    def missing(cmd, **kw):
        raise FileNotFoundError("wsl")
    assert keep_awake(popen=missing) is None
