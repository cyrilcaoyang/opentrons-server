"""Finished plans' results: saved on the gateway, delivered for the ELN.

Live loss 2026-10-05: the 192 balance readings of a 483-step plan existed only
in the in-memory plan record, so a restart or a dismissed card discarded them.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.plan_results import DELIVERED, LOCAL_ONLY, PENDING, PlanResultsStore
from opentrons_server.gateway.plans import Plan, PlanStep, StepResult

CLAIM = {"owner": "ada@lab", "session_id": "sess-1", "ttl_s": 30.0}


def _plan(plan_id="p1", *, eln_project="Complexation HTE", reading=True) -> Plan:
    now = datetime.now(timezone.utc)
    steps = [PlanStep(action="lights.set", args={"on": True}),
             PlanStep(action="platebalance.read", args={})]
    results = [
        StepResult(action="lights.set", outcome="ok", started_at=now, finished_at=now),
        StepResult(action="platebalance.read", outcome="ok", started_at=now, finished_at=now,
                   reading={"value": 0.1004, "unit": "g", "stable": True,
                            "observed_at": now.isoformat()} if reading else None),
    ]
    return Plan(plan_id=plan_id, steps=steps, step_hash="h", status="executed", created_at=now,
                created_by="assistant", results=results, eln_project=eln_project)


def _store(tmp_path, **kw) -> PlanResultsStore:
    kw.setdefault("start_worker", False)
    return PlanResultsStore(tmp_path / "results", device_id="ot2_complexation", **kw)


def test_saves_the_full_record_and_survives_a_new_store(tmp_path):
    store = _store(tmp_path)
    bundle = store.save(_plan(), approved_by="ada@lab", equipment_id="ot2_complexation",
                        gateway_version="0.4.0")

    assert bundle["approved_by"] == "ada@lab"
    assert bundle["proposed_by"] == "assistant"
    assert bundle["steps_ok"] == 2 and bundle["steps_total"] == 2
    assert bundle["plan"]["results"][1]["reading"]["value"] == 0.1004
    # No delivery configured: kept locally, not queued.
    assert bundle["delivery"]["state"] == LOCAL_ONLY

    reopened = _store(tmp_path)
    assert reopened.get("p1")["plan"]["plan_id"] == "p1"
    summary = reopened.list()
    assert [s["plan_id"] for s in summary] == ["p1"]
    assert "plan" not in summary[0]


def test_queues_for_the_eln_and_retries_until_acknowledged(tmp_path):
    attempts = []

    def flaky(payload):
        attempts.append(payload)
        if len(attempts) == 1:
            raise RuntimeError("central server down")
        return {"status": "accepted", "plan_id": payload["plan_id"]}

    store = _store(tmp_path, results_url="http://central/api/ingest/plan-results", transport=flaky)
    store.save(_plan(), approved_by="ada@lab", equipment_id="ot2_complexation", gateway_version="0.4.0")
    assert store.summary() == {"delivery_enabled": True, "pending": 1}

    assert store.deliver_pending() == 0
    first = store.get("p1")["delivery"]
    assert first["state"] == PENDING and first["attempts"] == 1
    assert "central server down" in first["last_error"]

    # A restart in between keeps the outbox.
    store = _store(tmp_path, results_url="http://central/api/ingest/plan-results", transport=flaky)
    assert store.summary()["pending"] == 1
    assert store.deliver_pending() == 1
    done = store.get("p1")["delivery"]
    assert done["state"] == DELIVERED and done["receipt"]["status"] == "accepted"
    assert store.summary()["pending"] == 0
    # What was sent is the record, never the local delivery bookkeeping.
    assert "delivery" not in attempts[-1]
    assert attempts[-1]["eln_project"] == "Complexation HTE"
    assert attempts[-1]["plate_report"]["wells"] is not None


def test_no_project_means_no_delivery(tmp_path):
    sent = []
    store = _store(tmp_path, results_url="http://central/x", transport=sent.append)
    bundle = store.save(_plan(eln_project=None), approved_by="ada@lab",
                        equipment_id="ot2_complexation", gateway_version="0.4.0")
    assert bundle["delivery"]["state"] == LOCAL_ONLY
    assert store.deliver_pending() == 0 and sent == []


def test_a_simulation_never_delivers(tmp_path):
    sent = []
    store = _store(tmp_path, results_url="http://central/x", transport=sent.append, simulation=True)
    bundle = store.save(_plan(), approved_by="ada@lab", equipment_id="ot2_complexation",
                        gateway_version="0.4.0")
    assert bundle["simulation"] is True
    assert bundle["delivery"]["state"] == LOCAL_ONLY
    assert store.deliver_pending() == 0 and sent == []


def test_a_plan_without_readings_has_no_plate_report(tmp_path):
    bundle = _store(tmp_path).save(_plan(reading=False), approved_by="ada@lab",
                                   equipment_id="ot2_complexation", gateway_version="0.4.0")
    assert bundle["plate_report"] is None and bundle["plate_report_error"] is None


def test_plan_ids_cannot_escape_the_results_directory(tmp_path):
    with pytest.raises(ValueError):
        _store(tmp_path).get("../ot2_tip_state")


def test_approve_records_the_project_and_execute_saves_the_results(tmp_path, monkeypatch):
    monkeypatch.setenv("OT2_PLAN_RESULTS_DIR", str(tmp_path / "results"))
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))
    plan = client.post("/plans", json={"steps": [{"action": "lights.set", "args": {"on": True}}],
                                       "created_by": "agent"}).json()
    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]
    approved = client.post(f"/plans/{plan['plan_id']}/approve",
                           json={"step_hash": plan["step_hash"], "eln_project": "Complexation HTE"},
                           headers={"X-Claim-Token": token}).json()
    assert approved["eln_project"] == "Complexation HTE"
    client.post(f"/plans/{plan['plan_id']}/execute", headers={"X-Claim-Token": token})

    saved = client.get("/plans/results").json()
    assert [s["plan_id"] for s in saved] == [plan["plan_id"]]
    assert saved[0]["eln_project"] == "Complexation HTE"
    assert saved[0]["approved_by"] == "ada@lab"
    assert saved[0]["simulation"] is True
    full = client.get(f"/plans/results/{plan['plan_id']}").json()
    assert full["plan"]["status"] == "executed"
    assert client.get("/plans/results/nope").status_code == 404
    assert client.get("/status").json()["details"]["plan_results"] == {
        "delivery_enabled": False, "pending": 0}


def test_me_reports_identity_and_projects_only_through_the_edge():
    client = TestClient(create_app(dry_run=True, ui=False, edge_secret="s3cret"))
    headers = {"X-Auth-User": "ada@lab", "X-Auth-Projects": "Complexation HTE, Polymers,"}
    assert client.get("/me", headers=headers).json() == {"user": None, "projects": []}
    assert client.get("/me", headers={**headers, "X-Edge-Key": "s3cret"}).json() == {
        "user": "ada@lab", "projects": ["Complexation HTE", "Polymers"]}
