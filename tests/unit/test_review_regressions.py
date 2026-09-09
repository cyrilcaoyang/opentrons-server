"""Offline regressions found while reviewing the assistant and control boundary."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.assistant import Assistant
from opentrons_server.gateway.models import ClaimedBy
from opentrons_server.gateway.plans import PlanExecutor, PlanStep, PlanStore
from opentrons_server.gateway.service import OT2Service, OT2ServiceState


def test_verified_owner_cannot_reclaim_another_owners_public_session_id():
    client = TestClient(create_app(dry_run=True, auto_reconnect=False, require_login=True,
                                  api_keys={"alice": "alice-test-key", "bob": "bob-test-key"}))
    body = {"owner": "ignored", "session_id": "public-session", "ttl_s": 30}
    first = client.post("/control/claim", json=body, headers={"X-Api-Key": "alice-test-key"})
    other = client.post("/control/claim", json=body, headers={"X-Api-Key": "bob-test-key"})
    assert other.status_code == 409
    assert first.json()["claim_token"] not in other.text
    assert client.get("/status").json()["details"]["claimed_by"]["owner"] == "api:alice"


def test_tokenless_release_cannot_clear_another_callers_claim():
    client = TestClient(create_app(dry_run=True, auto_reconnect=False))
    token = client.post("/control/claim", json={"owner": "alice", "session_id": "one"}).json()["claim_token"]
    client.post("/control/release")
    assert client.post("/control/heartbeat", headers={"X-Claim-Token": token}).status_code == 200


@pytest.mark.parametrize("path", ["/assistant/chat", "/assistant/chat/stream"])
def test_assistant_requires_callers_token_even_when_someone_else_holds_claim(monkeypatch, path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
    monkeypatch.setattr(Assistant, "chat", Mock(return_value={"reply": "hello"}))
    monkeypatch.setattr(Assistant, "chat_events", Mock(return_value=iter([])))
    client = TestClient(create_app(dry_run=True, auto_reconnect=False))
    client.post("/control/claim", json={"owner": "alice", "session_id": "one"})
    response = client.post(path, json={"messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 423
    Assistant.chat.assert_not_called()
    Assistant.chat_events.assert_not_called()


@pytest.mark.parametrize("state", [OT2ServiceState.ERROR, OT2ServiceState.PAUSED, OT2ServiceState.EXTERNAL_CONTROL])
def test_legacy_liquid_actions_honor_advertised_state_gate(state):
    service = OT2Service(dry_run=True)
    service.dry_run = False
    service.state = state
    service.control = Mock()
    command = Mock()
    with pytest.raises(RuntimeError):
        service._run_action("aspirate", command, idempotent=False)
    command.assert_not_called()
    assert service.state == state


def approved_plan():
    store = PlanStore()
    claim = ClaimedBy(owner="alice", session_id="one", expires_at=datetime.now(timezone.utc) + timedelta(seconds=30))
    plan = store.create([PlanStep(action="lights.set", args={"on": True}), PlanStep(action="plate.unload", args={})], created_by="assistant")
    store.approve(plan.plan_id, step_hash=plan.step_hash, claimed_by=claim)
    service = Mock()
    service.claims.current.return_value = claim
    service.allowed_actions.return_value = ["lights.set", "plate.unload"]
    return store, claim, plan, service


def test_abort_during_first_step_prevents_later_steps_and_preserves_abort():
    store, claim, plan, service = approved_plan()
    service.set_lights.side_effect = lambda _: store.abort(plan.plan_id, reason="operator cancelled")
    result = PlanExecutor(service, store).execute(plan.plan_id, claimed_by=claim)
    assert result.status == "aborted"
    assert [r.outcome for r in result.results] == ["ok", "skipped"]
    service.unload_plate.assert_not_called()


def test_lost_claim_between_plan_steps_blocks_the_next_step():
    store, claim, plan, service = approved_plan()
    service.set_lights.side_effect = lambda _: setattr(service.claims.current, "return_value", None)
    result = PlanExecutor(service, store).execute(plan.plan_id, claimed_by=claim)
    assert result.status == "failed"
    service.unload_plate.assert_not_called()


def test_missing_plan_execute_returns_404_instead_of_internal_error():
    client = TestClient(create_app(dry_run=True, auto_reconnect=False))
    token = client.post("/control/claim", json={"owner": "alice", "session_id": "one"}).json()["claim_token"]
    response = client.post("/plans/nonexistent/execute", headers={"X-Claim-Token": token})
    assert response.status_code == 404


def test_running_plan_cannot_be_executed_twice_revised_or_deleted():
    import threading
    from opentrons_server.gateway.plans import PlanStateError
    store, claim, plan, service = approved_plan()
    entered, finish = threading.Event(), threading.Event()
    def block(_):
        entered.set()
        assert finish.wait(5)
    service.set_lights.side_effect = block
    executor = PlanExecutor(service, store)
    results = []
    thread = threading.Thread(target=lambda: results.append(executor.execute(plan.plan_id, claimed_by=claim)))
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(PlanStateError):
            executor.execute(plan.plan_id, claimed_by=claim)
        with pytest.raises(PlanStateError):
            store.replace_steps(plan.plan_id, [PlanStep(action="home", args={})])
        store.abort(plan.plan_id, reason="operator cancelled")
        with pytest.raises(PlanStateError):
            store.delete(plan.plan_id)
    finally:
        finish.set()
        thread.join(5)
    assert results[0].status == "aborted"
    service.set_lights.assert_called_once()
    service.unload_plate.assert_not_called()


def test_owner_change_with_same_session_id_invalidates_plan_approval():
    store, claim, plan, service = approved_plan()
    service.set_lights.side_effect = lambda _: setattr(service.claims.current, "return_value", claim.model_copy(update={"owner": "bob"}))
    assert PlanExecutor(service, store).execute(plan.plan_id, claimed_by=claim).status == "failed"
    service.unload_plate.assert_not_called()


def test_propose_only_key_stays_read_only_when_cooperative_claims_disabled():
    client = TestClient(create_app(dry_run=True, auto_reconnect=False, enforce_claims=False,
                                  require_login=True, api_keys={"workflow": "full-test-key"},
                                  proposer_keys={"assistant": "draft-test-key"}))
    response = client.post("/control/home", headers={"X-Api-Key": "draft-test-key"})
    assert response.status_code == 403


@pytest.mark.parametrize("action,args", [
    ("aspirate", {"pipette": "p300; forbidden() #", "volume_ul": 50, "location": {"labware_nickname": "plate", "position": "A1"}}),
    ("blow_out", {"pipette": "left", "location": {"labware_nickname": "plate", "position": "A1']; forbidden() #"}}),
    ("touch_tip", {"pipette": "left", "labware_nickname": "plate; forbidden() #", "position": "A1"}),
    ("setup", {"labware": [{"nickname": "plate; forbidden() #", "ot_default": True, "loadname": "plate", "location": "1"}]}),
])
def test_python_expressions_are_rejected_while_still_draft_text(action, args):
    from opentrons_server.gateway.plans import StepValidationError
    with pytest.raises(StepValidationError):
        PlanStore().create([PlanStep(action=action, args=args)], created_by="assistant")
