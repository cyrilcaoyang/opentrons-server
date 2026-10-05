"""Who may read run data (2026-10-05): everyone signed in sees the run list;
readings, plate reports and full records only open for the approver, members
and PIs of the run's ELN project, and admins; API keys read everything; no
identity, no run data. The assistant follows the same rule."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.assistant import Assistant
from opentrons_server.gateway.plan_results import PlanResultsStore
from opentrons_server.gateway.plans import PlanStep, StepResult
from opentrons_server.gateway.run_access import RunReader

SECRET = "edge-s3cret"


def _edge(user, *, projects="", pi="", role="user"):
    return {"X-Edge-Key": SECRET, "X-Auth-User": user, "X-Auth-Projects": projects,
            "X-Auth-Pi-Projects": pi, "X-Auth-Role": role}


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("OT2_PLAN_RESULTS_DIR", str(tmp_path / "results"))
    app = create_app(dry_run=True, ui=False, enforce_claims=True, require_login=True,
                     edge_secret=SECRET, api_keys={"workflow": "wf-key"})
    store = app.state.plans
    now = datetime.now(timezone.utc)
    # alice's finished run, filed to project "chem", with one balance reading
    ran = store.create([PlanStep(action="platebalance.read", args={})], created_by="assistant")
    ran.approved_by, ran.eln_project, ran.status = "alice@lab", "chem", "executed"
    ran.results = [StepResult(action="platebalance.read", outcome="ok", started_at=now, finished_at=now,
                              reading={"value": 0.1, "unit": "g", "stable": True,
                                       "observed_at": now.isoformat()})]
    PlanResultsStore(tmp_path / "results", device_id="ot2", start_worker=False).save(
        ran, approved_by="alice@lab", equipment_id="ot2", gateway_version="v")
    draft = store.create([PlanStep(action="lights.set", args={"on": True})], created_by="assistant")
    return TestClient(app), ran.plan_id, draft.plan_id, app


def _paths(pid):
    return [f"/plans/results/{pid}", f"/plans/plate-report?plan_id={pid}",
            f"/plans/plate-report.xlsx?plan_id={pid}", f"/plans/plate-report.html?plan_id={pid}"]


def test_no_identity_reads_no_run_data(gateway):
    client, pid, _draft, _app = gateway
    for path in ["/plans", "/plans/results", f"/plans/{pid}", *_paths(pid)]:
        assert client.get(path).status_code == 401, path
    # A forged identity without the edge secret is no identity.
    assert client.get("/plans/results", headers={"X-Auth-User": "alice@lab"}).status_code == 401


@pytest.mark.parametrize("headers", [
    _edge("alice@lab"),                         # approved it
    _edge("bob@lab", projects="chem"),          # member of its project
    _edge("pi@lab", pi="chem"),                 # PI of its project
    _edge("root@lab", role="admin"),            # admin
    {"X-Api-Key": "wf-key"},                    # a lab service
])
def test_who_may_open_a_runs_data(gateway, headers):
    client, pid, _draft, _app = gateway
    for path in _paths(pid):
        assert client.get(path, headers=headers).status_code == 200, (path, headers)
    assert client.get("/plans/results", headers=headers).json()[0]["can_open"] is True
    view = client.get(f"/plans/{pid}", headers=headers).json()
    assert view["results"][0]["reading"]["value"] == 0.1 and "redacted" not in view


def test_anyone_else_signed_in_sees_the_run_but_not_its_data(gateway):
    client, pid, _draft, _app = gateway
    carol = _edge("carol@lab", projects="other")
    listing = client.get("/plans/results", headers=carol).json()
    assert listing[0]["plan_id"] == pid and listing[0]["can_open"] is False
    for path in _paths(pid):
        assert client.get(path, headers=carol).status_code == 403, path
    view = client.get(f"/plans/{pid}", headers=carol).json()
    assert view["redacted"] is True and view["status"] == "executed"
    assert "reading" not in view["results"][0] and view["results"][0]["outcome"] == "ok"
    listed = next(p for p in client.get("/plans", headers=carol).json() if p["plan_id"] == pid)
    assert "reading" not in listed["results"][0]


def test_an_unapproved_draft_is_open_to_everyone_signed_in(gateway):
    client, _pid, draft, _app = gateway
    view = client.get(f"/plans/{draft}", headers=_edge("carol@lab")).json()
    assert "redacted" not in view


def test_the_assistant_reads_with_the_same_rule(gateway):
    _client, pid, _draft, app = gateway
    plans = app.state.plans
    carol = Assistant(app.state.service, plans, config=None, reader=RunReader(user="carol@lab"))
    tools = carol._tools()
    with pytest.raises(PermissionError):
        tools["get_plate_report"]({"plan_ids": [pid]})
    assert tools["get_plan"]({"plan_id": pid})["redacted"] is True
    listed = next(p for p in tools["list_plans"]({}) if p["plan_id"] == pid)
    assert listed["readings"] == [] and listed["redacted"] is True
    alice = Assistant(app.state.service, plans, config=None, reader=RunReader(user="alice@lab"))
    assert alice._tools()["get_plan"]({"plan_id": pid})["results"][0]["reading"]["value"] == 0.1
