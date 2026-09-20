"""Agent discovery must describe real capabilities without touching hardware."""

import re
from unittest.mock import Mock
from urllib.parse import urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.assistant import Assistant, AssistantConfig
from opentrons_server.gateway.documentation import router as documentation_router
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


# ---------------------------------------------------------------------------
# The lab-standard Markdown documentation surface (GET /agent-docs,
# /agent-docs/api-reference, /llms.txt), mirroring
# torry-pines-shaker-server/tests/test_documentation.py.
# ---------------------------------------------------------------------------

# Relative on purpose: the gateway is reached both on its own port and behind
# the edge's `/ot2/<robot>/` prefix, and an absolute path resolves off the mount.
INDEX_LINKS = {
    "agent-docs",
    "agent-docs/api-reference",
    "openapi.json",
    "docs/agent",
    "plans/actions",
}


def test_markdown_docs_are_served_without_a_claim_or_hardware(docs_client):
    guide = docs_client.get("/agent-docs")
    assert guide.status_code == 200
    assert guide.headers["content-type"].startswith("text/markdown")
    assert "allowed_actions" in guide.text and "unknown_outcome" in guide.text

    reference = docs_client.get("/agent-docs/api-reference")
    assert reference.status_code == 200
    assert reference.headers["content-type"].startswith("text/markdown")
    assert "/control/aspirate" in reference.text

    index = docs_client.get("/llms.txt")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/plain")
    assert "(agent-docs/api-reference)" in index.text and "(openapi.json)" in index.text

    # Same guarantee the JSON guide carries: no state read, no claim taken.
    docs_client.app.state.service.get_status.assert_not_called()
    assert docs_client.app.state.service.claims.current() is None


def test_index_points_at_the_existing_json_resources(docs_client):
    links = set(re.findall(r"\]\(([^)]+)\)", docs_client.get("/llms.txt").text))
    assert links == INDEX_LINKS
    for link in sorted(links):
        assert docs_client.get(f"/{link}").status_code == 200


@pytest.mark.parametrize("prefix", ["", "/ot2", "/api/equipment/ot2_complexation/documentation"])
def test_index_links_resolve_under_any_proxy_prefix(prefix):
    service = FastAPI()
    service.include_router(documentation_router)
    app = FastAPI()
    app.mount(prefix or "/", service)
    with TestClient(app) as client:
        response = client.get(f"{prefix}/llms.txt")
        assert response.status_code == 200
        links = re.findall(r"\]\(([^)]+)\)", response.text)
        assert set(links) == INDEX_LINKS
        for link in links:
            resolved = urljoin(str(response.url), link)
            assert resolved == f"http://testserver{prefix}/{link}"
        # Only the documentation routes live on this bare router app; the two
        # JSON device resources come from the gateway app, exercised above.
        for link in ("agent-docs", "agent-docs/api-reference", "openapi.json"):
            assert client.get(urljoin(str(response.url), link)).status_code == 200


def test_openapi_lists_the_documentation_routes(docs_client):
    paths = docs_client.get("/openapi.json").json()["paths"]
    assert {"/agent-docs", "/agent-docs/api-reference", "/llms.txt"} <= set(paths)


def test_api_reference_documents_every_openapi_path(docs_client):
    """Drift guard: a new route that nobody documented fails here, not on a robot."""
    reference = docs_client.get("/agent-docs/api-reference").text
    paths = docs_client.get("/openapi.json").json()["paths"]
    undocumented = sorted(path for path in paths if path not in reference)
    assert not undocumented, f"undocumented in API_REFERENCE.md: {undocumented}"
