"""Agent discovery must describe real capabilities without touching hardware."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.assistant import Assistant, AssistantConfig
from opentrons_server.gateway.plans import PlanStore


@pytest.fixture
def docs_client(tmp_path, monkeypatch):
    for key in ("PLATE", "DECK", "TIP"):
        monkeypatch.setenv(f"OT2_{key}_STATE_PATH", str(tmp_path / f"{key}.json"))
    app = create_app(
        dry_run=True, auto_reconnect=False, ui=True, trust_local_ui=False,
        edge_secret="test-edge", require_login=True,
        api_keys={"workflow": "test-key"}, proposer_keys={"agent": "proposal-key"},
    )
    # Any service access by documentation is a bug. Check with the production
    # identity/edge gates enabled, before startup and without a claim.
    monkeypatch.setattr(
        app.state.service, "get_status", Mock(side_effect=AssertionError("state read"))
    )
    with TestClient(app) as client:
        yield client


def test_guide_is_read_only_and_does_not_relax_control_auth(docs_client):
    for path in ("/docs/agent", "/openapi.json", "/docs", "/redoc"):
        assert docs_client.get(path).status_code == 200
    assert docs_client.post("/control/home").status_code == 423
    assert docs_client.get("/ui/").status_code == 404
    docs_client.app.state.service.get_status.assert_not_called()
    assert docs_client.app.state.service.claims.current() is None


def test_documented_schemas_match_catalog_and_blow_out_is_exposed(docs_client):
    guide = docs_client.get("/docs/agent").json()
    catalog = docs_client.get("/plans/actions").json()
    schema = docs_client.get("/openapi.json").json()
    assert guide["actions"] == catalog["actions"]
    assert "/docs/agent" in schema["paths"]
    for path in guide["links"].values():
        if path != "/status":
            assert docs_client.get(path).status_code == 200
    blowout = guide["liquid_handling"]["blow_out"]
    assert blowout["gateway_http_endpoint"] in schema["paths"]
    assert any(item["action"] == "blow_out" for item in catalog["actions"])
    assert not any(item["action"] == "stop" for item in catalog["actions"])


def test_assistant_reads_same_guide_without_service_access(docs_client):
    service = Mock()
    assistant = Assistant(service, PlanStore(), Mock(spec=AssistantConfig))
    assert assistant._tools()["get_equipment_docs"]({}) == docs_client.get("/docs/agent").json()
    assert service.mock_calls == []
