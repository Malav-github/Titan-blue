"""Calls real APIs through the guarded caller. Skipped unless the connector is active and its key is set."""

import json
from pathlib import Path

import pytest

from connectors.caller import call_api, get_key
from connectors.registry import ConnectorRegistry
from evidence import EvidenceStore

pytestmark = pytest.mark.network


@pytest.fixture
def openalex():
    registry = ConnectorRegistry()
    active = registry.active("openalex")
    if active is None or get_key(active[1].key_env) is None:
        pytest.skip("the openalex connector is not active here (see manage_capability.py)")
    return registry


def test_openalex_list_works_is_archived_and_never_contains_the_key(openalex, tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "archive")
    fetched = call_api(openalex, "openalex", "listworks", {"filter": "title.search:polyolefin recycling", "per_page": 2},
                       run_id="live-test")
    assert fetched["status"] == 200
    key = get_key("OPENALEX_API_KEY")
    assert key not in fetched["url"] and key not in fetched.get("final_url", "")
    assert key.encode() not in Path(fetched["archive_path"]).read_bytes()
    store = EvidenceStore(tmp_path / "e.db")
    sha = store.ingest(fetched)
    text = Path(store.get_source(sha)["text_path"]).read_text(encoding="utf-8")
    assert len(json.loads(text)["results"]) == 2
