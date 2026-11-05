"""The flowchart is drawn from the real research graph, and a file that drifts from it is refused."""

import json

import pytest
from fastapi.testclient import TestClient

import harness
from api.registry import Registry
from api.server import create_app
from evidence import EvidenceStore


def test_the_description_file_matches_the_real_graph():
    data = harness.research()
    nodes, edges = harness.graph_structure()
    assert {n["id"] for n in data["nodes"]} == nodes
    assert {(e["from"], e["to"]) for e in data["edges"]} == edges
    assert ("assess", "discover") in edges and ("capability", "report") in edges     # the loop and the pause both exist
    assert all(n["does"] and n["label"] for n in data["nodes"])


def test_a_file_that_drifts_from_the_graph_is_refused(tmp_path, monkeypatch):
    spec = json.loads(harness.PATH.read_text(encoding="utf-8"))
    spec["edges"].append({"from": "report", "to": "plan"})
    del spec["nodes"]["select"]
    bad = tmp_path / "research.json"
    bad.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(harness, "PATH", bad)
    with pytest.raises(ValueError, match="does not match the research graph"):
        harness.research()


def test_layout_has_no_two_steps_in_the_same_place():
    spots = [(n["col"], n["row"]) for n in harness.research()["nodes"]]
    assert len(spots) == len(set(spots))


def test_endpoint_serves_the_flowchart(tmp_path):
    app = create_app(Registry.load(), "t", {"ollama": lambda: None, "searxng": lambda: None, "network": lambda: None},
                     EvidenceStore(str(tmp_path / "e.db")))
    c = TestClient(app)
    assert c.get("/api/harness/research").status_code == 401
    data = c.get("/api/harness/research", headers={"x-titan-token": "t"}).json()
    assert len(data["nodes"]) == 13 and len(data["edges"]) == 20
