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
    assert store.summary() == {"delivery_enabled": True, "pending": 1, "filing": 0}

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
        "delivery_enabled": False, "pending": 0, "filing": 0}


def test_me_reports_identity_and_projects_only_through_the_edge():
    client = TestClient(create_app(dry_run=True, ui=False, edge_secret="s3cret"))
    headers = {"X-Auth-User": "ada@lab", "X-Auth-Projects": "Complexation HTE, Polymers,"}
    assert client.get("/me", headers=headers).json() == {"user": None, "projects": []}
    assert client.get("/me", headers={**headers, "X-Edge-Key": "s3cret"}).json() == {
        "user": "ada@lab", "projects": ["Complexation HTE", "Polymers"]}


# ── live run records (2026-10-05): saved as the run goes, survive restarts ──


def test_a_running_plan_is_saved_as_running_and_never_delivered(tmp_path):
    sent = []
    store = _store(tmp_path, results_url="http://central/x", transport=sent.append)
    plan = _plan()
    plan.status = "executing"
    plan.results[1].finished_at = None
    plan.results[1].outcome = "pending"
    bundle = store.save_progress(plan, approved_by="ada@lab", equipment_id="ot2_complexation",
                                 gateway_version="0.4.0")
    assert bundle["delivery"]["state"] == "running"
    assert bundle["status"] == "executing" and bundle["steps_done"] == 1
    assert store.summary()["pending"] == 0
    assert store.deliver_pending() == 0 and sent == []
    assert store.list()[0]["delivery"]["state"] == "running"


def test_a_run_cut_off_by_a_restart_is_recovered_as_interrupted(tmp_path):
    store = _store(tmp_path, results_url="http://central/x", transport=lambda p: {"ok": True})
    plan = _plan()
    plan.status = "executing"
    plan.steps.append(PlanStep(action="lights.set", args={"on": False}))
    plan.results[1].finished_at = None
    plan.results[1].outcome = "pending"  # started, never finished
    plan.results.append(StepResult(action="lights.set"))  # never started
    store.save_progress(plan, approved_by="ada@lab", equipment_id="ot2_complexation",
                        gateway_version="0.4.0")

    # This process wrote it, so it is live here: never closed out.
    assert store.recover() == []
    _from_a_dead_process(store, "p1")

    reopened = _store(tmp_path, results_url="http://central/x", transport=lambda p: {"ok": True})
    assert reopened.recover() == ["p1"]
    bundle = reopened.get("p1")
    assert bundle["status"] == "interrupted" and "never completed" in bundle["halt_reason"]
    assert [r["outcome"] for r in bundle["plan"]["results"]] == ["ok", "unknown", "skipped"]
    assert bundle["steps_unknown"] == 1 and bundle["steps_skipped"] == 1
    # Like any ended run with a project, it goes to the ELN.
    assert bundle["delivery"]["state"] == PENDING
    assert reopened.recover() == []  # only once


def _from_a_dead_process(store, plan_id):
    """Rewrite a record's writer as a process that no longer exists."""
    import json as _json

    path = store._path(plan_id)
    bundle = _json.loads(path.read_text())
    bundle["writer"] = {"token": "gone", "pid": 2 ** 22 + 7, "host": bundle["writer"]["host"]}
    path.write_text(_json.dumps(bundle))


def test_a_record_still_written_by_another_live_process_is_left_alone(tmp_path):
    import json as _json
    import os

    store = _store(tmp_path)
    plan = _plan("live-elsewhere")
    plan.status = "executing"
    store.save_progress(plan, approved_by="ada@lab", equipment_id="e", gateway_version="v")
    path = store._path("live-elsewhere")
    bundle = _json.loads(path.read_text())
    bundle["writer"] = {"token": "other", "pid": os.getppid(), "host": bundle["writer"]["host"]}
    path.write_text(_json.dumps(bundle))
    assert store.recover() == []
    assert store.get("live-elsewhere")["delivery"]["state"] == "running"


def test_a_delivery_retry_does_not_make_an_old_record_look_recent(tmp_path):
    import time

    store = _store(tmp_path, results_url="http://central/x", transport=lambda p: {"ok": True})
    store.save(_plan("old"), approved_by="ada@lab", equipment_id="e", gateway_version="v")
    time.sleep(0.05)
    store.save(_plan("new", eln_project=None), approved_by="ada@lab", equipment_id="e",
               gateway_version="v")
    time.sleep(0.05)
    assert store.deliver_pending() == 1  # rewrites "old"
    assert [b["plan_id"] for b in store.list(limit=1)] == ["new"]


def test_list_returns_the_newest_records_first_and_honours_the_limit(tmp_path):
    import os
    import time

    store = _store(tmp_path)
    for i, pid in enumerate(["old", "mid", "new"]):
        store.save(_plan(pid), approved_by="ada@lab", equipment_id="e", gateway_version="v")
        os.utime(store._path(pid), (time.time() + i, time.time() + i))
    assert [b["plan_id"] for b in store.list(limit=2)] == ["new", "mid"]


def test_the_executor_reports_each_step_as_it_starts_and_ends():
    from unittest.mock import Mock

    from opentrons_server.gateway.claims import ClaimManager
    from opentrons_server.gateway.models import ClaimedBy
    from opentrons_server.gateway.plans import PlanExecutor, PlanStore

    store = PlanStore()
    service = Mock()
    service.claims = ClaimManager()
    service.allowed_actions.return_value = ["lights.set", "plate.unload"]
    claim = ClaimedBy(session_id="s", owner="ada@lab",
                      expires_at=datetime.now(timezone.utc).replace(year=2099))
    plan = store.create([PlanStep(action="lights.set", args={"on": True}),
                         PlanStep(action="plate.unload", args={})], created_by="agent")
    store.approve(plan.plan_id, step_hash=plan.step_hash, claimed_by=claim)
    seen = []
    executor = PlanExecutor(service, store)
    executor.on_progress = lambda snap: seen.append(
        [(r.outcome, r.started_at is not None) for r in snap.results])
    done = executor.execute(plan.plan_id, claimed_by=claim)
    assert done.status == "executed"
    # start, then (step started, step done) per step
    assert seen == [
        [("pending", False), ("pending", False)],
        [("pending", True), ("pending", False)],
        [("ok", True), ("pending", False)],
        [("ok", True), ("pending", True)],
        [("ok", True), ("ok", True)],
    ]


def test_a_run_that_cannot_be_recorded_stops_before_its_next_step():
    from unittest.mock import Mock

    from opentrons_server.gateway.claims import ClaimManager
    from opentrons_server.gateway.models import ClaimedBy
    from opentrons_server.gateway.plans import PlanExecutor, PlanStore

    store = PlanStore()
    service = Mock()
    service.claims = ClaimManager()
    service.allowed_actions.return_value = ["lights.set"]
    claim = ClaimedBy(session_id="s", owner="ada@lab",
                      expires_at=datetime.now(timezone.utc).replace(year=2099))
    service.allowed_actions.return_value = ["lights.set", "plate.unload"]
    plan = store.create([PlanStep(action="lights.set", args={"on": True}),
                         PlanStep(action="plate.unload", args={})], created_by="agent")
    store.approve(plan.plan_id, step_hash=plan.step_hash, claimed_by=claim)
    executor = PlanExecutor(service, store)
    calls = []

    def fails_after_step_one(_snap):
        calls.append(1)
        if len(calls) > 3:  # run start, step 1 start, step 1 end succeed
            raise OSError("disk full")

    executor.on_progress = fails_after_step_one
    done = executor.execute(plan.plan_id, claimed_by=claim)
    assert done.status == "failed"
    assert "disk full" in done.halt_reason and "nothing runs unrecorded" in done.halt_reason
    service.set_lights.assert_called_once()
    service.unload_plate.assert_not_called()  # its start could not be recorded
    assert done.results[1].outcome == "skipped" and done.results[1].started_at is None
    assert "disk full" in executor.progress_error


def test_the_plate_report_outlives_a_gateway_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("OT2_PLAN_RESULTS_DIR", str(tmp_path / "results"))
    # A finished run saved by an earlier process; this one never had it in memory.
    _store(tmp_path).save(_plan("ran-before"), approved_by="ada@lab",
                          equipment_id="ot2_complexation", gateway_version="0.4.0")
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))
    assert client.get("/plans/ran-before").status_code == 404  # not in memory
    report = client.get("/plans/plate-report", params={"plan_id": "ran-before"})
    assert report.status_code == 200, report.text
    assert report.json()["plans"][0]["plan_id"] == "ran-before"
    assert client.get("/plans/plate-report.xlsx", params={"plan_id": "ran-before"}).status_code == 200
    assert client.get("/plans/plate-report", params={"plan_id": "never"}).status_code == 404


def test_a_restart_mid_run_is_recorded_when_the_gateway_comes_back(tmp_path, monkeypatch):
    monkeypatch.setenv("OT2_PLAN_RESULTS_DIR", str(tmp_path / "results"))
    plan = _plan("cut-off")
    plan.status = "executing"
    plan.results[1].finished_at = None
    plan.results[1].outcome = "pending"
    earlier = _store(tmp_path)
    earlier.save_progress(plan, approved_by="ada@lab", equipment_id="ot2_complexation",
                          gateway_version="0.4.0")
    _from_a_dead_process(earlier, "cut-off")
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))
    record = client.get("/plans/results/cut-off").json()
    assert record["status"] == "interrupted"
    assert [r["outcome"] for r in record["plan"]["results"]] == ["ok", "unknown"]


def test_the_liveness_check_never_signals_on_windows(monkeypatch):
    """os.kill(pid, 0) is a probe on POSIX but sends CTRL_C_EVENT on Windows,
    where the gateways run: the check must use a query there instead."""
    import ctypes
    from ctypes import wintypes  # noqa: F401 — loaded before os.name is faked

    from opentrons_server.gateway import plan_results as pr

    def forbidden(*_a, **_k):
        raise AssertionError("os.kill must not be called on Windows")

    monkeypatch.setattr(pr.os, "name", "nt")
    monkeypatch.setattr(pr.os, "kill", forbidden)
    calls = []

    class _K32:
        class _F:
            def __init__(self, fn):
                self.fn = fn
                self.restype = None

            def __call__(self, *a):
                return self.fn(*a)

        def __init__(self):
            self.OpenProcess = self._F(lambda access, inherit, pid: calls.append(("open", access, pid)) or 1)
            self.GetExitCodeProcess = self._F(lambda h, ref: (setattr(ref._obj, "value", 259), True)[1])
            self.CloseHandle = self._F(lambda h: calls.append(("close", h)) or True)

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: _K32(), raising=False)
    assert pr._pid_alive(4242) is True
    assert calls[0] == ("open", 0x1000, 4242) and calls[-1] == ("close", 1)


def test_a_file_time_failure_never_reports_a_saved_record_as_lost(tmp_path, monkeypatch):
    from opentrons_server.gateway import plan_results as pr

    def no_utime(*_a, **_k):
        raise OSError("read-only metadata")

    monkeypatch.setattr(pr.os, "utime", no_utime)
    store = _store(tmp_path)
    store.save(_plan(), approved_by="ada@lab", equipment_id="e", gateway_version="v")
    assert store.get("p1")["plan_id"] == "p1"


# ── what the ELN did with it (2026-10-05: "filed" was shown for a held run) ──


def test_delivered_is_not_filed_until_the_central_server_says_so(tmp_path):
    central = {"state": "pending", "last_error": None, "experiment_id": None}
    store = _store(tmp_path, results_url="http://central/api/ingest/plan-results",
                   transport=lambda p: {"status": "accepted"},
                   status_transport=lambda pid: dict(central))
    store.save(_plan(), approved_by="ada@lab", equipment_id="e", gateway_version="v")
    store.deliver_pending()
    assert store.get("p1")["delivery"]["eln"]["state"] == "filing"
    assert store.check_filing() == 0
    assert store.summary()["filing"] == 1

    central.update(state="held", last_error="project 'basf-solubility' does not exist")
    assert store.check_filing() == 1
    eln = store.get("p1")["delivery"]["eln"]
    assert eln["state"] == "held" and "does not exist" in eln["last_error"]
    assert store.summary()["filing"] == 0


def test_a_filed_run_carries_its_experiment(tmp_path):
    store = _store(tmp_path, results_url="http://central/x", transport=lambda p: {"ok": True},
                   status_transport=lambda pid: {"state": "filed", "experiment_id": "exp-1"})
    store.save(_plan(), approved_by="ada@lab", equipment_id="e", gateway_version="v")
    store.deliver_pending()
    store.check_filing()
    assert store.get("p1")["delivery"]["eln"] == {
        "state": "filed", "last_error": None, "experiment_id": "exp-1",
        "checked_at": store.get("p1")["delivery"]["eln"]["checked_at"]}


def test_runs_delivered_before_outcomes_were_tracked_are_checked_on_start(tmp_path):
    import json as _json

    store = _store(tmp_path)
    store.save(_plan(), approved_by="ada@lab", equipment_id="e", gateway_version="v")
    path = store._path("p1")
    bundle = _json.loads(path.read_text())
    bundle["delivery"] = {"state": "delivered", "attempts": 1, "last_error": None,
                          "delivered_at": "2026-10-05T17:11:13Z", "receipt": {"status": "accepted"}}
    path.write_text(_json.dumps(bundle))
    reopened = _store(tmp_path, results_url="http://central/x", transport=lambda p: {},
                      status_transport=lambda pid: {"state": "held", "last_error": "missing"})
    assert reopened.summary()["filing"] == 1
    assert reopened.check_filing() == 1
    assert reopened.get("p1")["delivery"]["eln"]["state"] == "held"


def test_a_failed_status_check_is_recorded_and_retried(tmp_path):
    calls = []

    def flaky(pid):
        calls.append(pid)
        if len(calls) == 1:
            raise RuntimeError("central down")
        return {"state": "filed"}

    store = _store(tmp_path, results_url="http://central/x", transport=lambda p: {},
                   status_transport=flaky)
    store.save(_plan(), approved_by="ada@lab", equipment_id="e", gateway_version="v")
    store.deliver_pending()
    assert store.check_filing() == 0
    assert "central down" in store.get("p1")["delivery"]["eln"]["check_error"]
    assert store.check_filing() == 1
    assert "check_error" not in store.get("p1")["delivery"]["eln"]
