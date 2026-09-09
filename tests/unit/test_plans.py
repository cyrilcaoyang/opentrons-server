"""The human-approval gate for agent-proposed plans.

These tests are the specification of the gate, not incidental coverage. The
property each one pins is the reason the gate exists: an autonomous agent may
propose robot motion and may pull the trigger, but only on a step list a
present human approved, and only for as long as that human is still there.

Nothing here touches hardware — every service is `dry_run=True` or a Mock.
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.models import ClaimedBy
from opentrons_server.gateway.plans import (
    PLAN_ACTIONS,
    ApprovalRequiresClaim,
    PlanExecutor,
    PlanHashMismatch,
    PlanNotFound,
    PlanStateError,
    PlanStep,
    PlanStore,
    StepValidationError,
    compute_step_hash,
)

CLAIM = {"owner": "ada@lab", "session_id": "sess-1", "ttl_s": 30.0}
OTHER_CLAIM = {"owner": "ada@lab", "session_id": "sess-2", "ttl_s": 30.0}


def _claimed_by(session_id: str = "sess-1", owner: str = "ada@lab") -> ClaimedBy:
    return ClaimedBy(
        session_id=session_id,
        owner=owner,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )


def _steps(*actions: str) -> list[PlanStep]:
    canned = {
        "home": {},
        "lights.set": {"on": True},
        "plate.unload": {},
        "aspirate": {
            "pipette": "p300",
            "volume_ul": 50,
            "location": {"labware_nickname": "plate", "position": "A1"},
        },
    }
    return [PlanStep(action=a, args=dict(canned[a])) for a in actions]


# ---------------------------------------------------------------------------
# Proposal — interlock layer 1, applied while it is still text
# ---------------------------------------------------------------------------


def test_unknown_action_is_refused_at_proposal():
    """An agent cannot invent a verb. The catalog is the whole vocabulary."""
    store = PlanStore()
    with pytest.raises(StepValidationError, match="unknown action"):
        store.create([PlanStep(action="rm -rf /", args={})], created_by="agent")


def test_bad_arguments_are_refused_at_proposal():
    """The step's args are parsed with the same model the matching /control/*
    endpoint uses, so a malformed step never reaches an approval screen."""
    store = PlanStore()
    with pytest.raises(StepValidationError, match="aspirate"):
        store.create(
            [PlanStep(action="aspirate", args={"pipette": "p300"})],  # no volume/location
            created_by="agent",
        )


def test_misnamed_arguments_are_refused_not_silently_dropped():
    """The live bug of 2026-08-11: ``pick_up_tip {"slot": 9, "well": "A1"}``
    validated cleanly because Pydantic ignored the unknown keys — the reviewed
    step silently became a *bare* pick, and something other than the operator's
    intent ran. Unknown keys are now a proposal-time refusal (StrictRequest)."""
    store = PlanStore()
    with pytest.raises(StepValidationError, match="pick_up_tip"):
        store.create(
            [
                PlanStep(
                    action="pick_up_tip",
                    args={"pipette": "left", "slot": 9, "well": "A1"},
                )
            ],
            created_by="agent",
        )


def test_delete_dismisses_settled_plans_only():
    """Dismiss is for terminal plans (the failed plan's banner); a live plan
    must be aborted instead, so the proposing agent can see the rejection."""
    store = PlanStore()
    plan = store.create(_steps("home"), created_by="agent")
    with pytest.raises(PlanStateError, match="abort it instead"):
        store.delete(plan.plan_id)

    plan.status = "failed"
    store.delete(plan.plan_id)
    with pytest.raises(PlanNotFound):
        store.get(plan.plan_id)


def test_lifecycle_and_recovery_actions_are_not_plannable():
    """startup/shutdown/pause/resume/reconcile are deliberately absent.

    `reconcile` is the sharpest of these: it clears `unknown_outcome`, the
    state that means "nobody knows whether that aspirate happened". A plan
    that could clear its own ambiguity would erase the one signal a human is
    supposed to adjudicate.
    """
    for excluded in ("startup", "shutdown", "pause", "resume", "reconcile"):
        assert excluded not in PLAN_ACTIONS


def test_step_hash_is_canonical_but_sensitive():
    """Key order must not change the hash; an argument value must."""
    a = [PlanStep(action="lights.set", args={"on": True})]
    b = [PlanStep(action="lights.set", args={"on": True})]
    c = [PlanStep(action="lights.set", args={"on": False})]
    assert compute_step_hash(a) == compute_step_hash(b)
    assert compute_step_hash(a) != compute_step_hash(c)


def test_non_idempotent_actions_are_surfaced_for_review():
    """The reviewer is told which steps cannot be safely repeated."""
    store = PlanStore()
    plan = store.create(_steps("home", "aspirate"), created_by="agent")
    assert plan.non_idempotent_actions == ["aspirate"]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_a_draft_cannot_execute():
    store = PlanStore()
    plan = store.create(_steps("home"), created_by="agent")
    with pytest.raises(PlanStateError, match="not approved"):
        store.check_executable(plan.plan_id, claimed_by=_claimed_by())


def test_approval_requires_a_live_claim():
    """No claim means no human present, so there is nobody to approve."""
    store = PlanStore()
    plan = store.create(_steps("home"), created_by="agent")
    with pytest.raises(ApprovalRequiresClaim):
        store.approve(plan.plan_id, step_hash=plan.step_hash, claimed_by=None)
    assert store.get(plan.plan_id).status == "draft"


def test_approving_a_stale_hash_is_refused():
    """The reviewer sends back the hash they were shown. If it no longer
    matches, they reviewed something else."""
    store = PlanStore()
    plan = store.create(_steps("home"), created_by="agent")
    with pytest.raises(PlanHashMismatch):
        store.approve(plan.plan_id, step_hash="0" * 64, claimed_by=_claimed_by())
    assert store.get(plan.plan_id).status == "draft"


def test_editing_after_approval_voids_it():
    """The property the whole design exists for.

    An agent must not be able to get a harmless plan approved and then swap in
    different steps. Revising resets the plan to draft and discards the
    approval — it is a new plan that has never been reviewed.
    """
    store = PlanStore()
    plan = store.create(_steps("lights.set"), created_by="agent")
    store.approve(plan.plan_id, step_hash=plan.step_hash, claimed_by=_claimed_by())
    assert store.get(plan.plan_id).status == "approved"

    store.replace_steps(plan.plan_id, _steps("aspirate"))

    revised = store.get(plan.plan_id)
    assert revised.status == "draft"
    assert revised.approval is None
    with pytest.raises(PlanStateError):
        store.check_executable(plan.plan_id, claimed_by=_claimed_by())


def test_execution_requires_the_same_claim_session_that_approved():
    """Approval is bound to a session, not just to a person.

    A fresh claim — even by the same operator in a new tab — is a different
    session and must re-review. This is what makes an approval decay when the
    operator walks away and their claim lapses.
    """
    store = PlanStore()
    plan = store.create(_steps("home"), created_by="agent")
    store.approve(plan.plan_id, step_hash=plan.step_hash, claimed_by=_claimed_by("sess-1"))

    with pytest.raises(ApprovalRequiresClaim, match="different session"):
        store.check_executable(plan.plan_id, claimed_by=_claimed_by("sess-2"))
    with pytest.raises(ApprovalRequiresClaim, match="no live claim"):
        store.check_executable(plan.plan_id, claimed_by=None)

    assert store.check_executable(plan.plan_id, claimed_by=_claimed_by("sess-1"))


def test_expired_approval_falls_back_to_draft():
    """A standing permission to move a robot should not outlive the operator's
    attention. On expiry the plan reverts to draft rather than lingering."""
    store = PlanStore()
    plan = store.create(_steps("home"), created_by="agent")
    store.approve(plan.plan_id, step_hash=plan.step_hash, claimed_by=_claimed_by())
    plan.approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(PlanStateError, match="expired"):
        store.check_executable(plan.plan_id, claimed_by=_claimed_by())
    assert store.get(plan.plan_id).status == "draft"


def test_missing_plan_is_a_lookup_error():
    with pytest.raises(PlanNotFound):
        PlanStore().get("nope")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _approved(store: PlanStore, *actions: str):
    plan = store.create(_steps(*actions), created_by="agent")
    store.approve(plan.plan_id, step_hash=plan.step_hash, claimed_by=_claimed_by())
    return plan


def test_executes_every_step_in_order_then_spends_the_approval():
    store = PlanStore()
    service = Mock()
    service.claims.current.return_value = _claimed_by()
    service.allowed_actions.return_value = ["lights.set", "plate.unload"]
    plan = _approved(store, "lights.set", "plate.unload")

    done = PlanExecutor(service, store).execute(plan.plan_id, claimed_by=_claimed_by())

    assert done.status == "executed"
    assert [r.outcome for r in done.results] == ["ok", "ok"]
    service.set_lights.assert_called_once_with(True)
    service.unload_plate.assert_called_once()
    # Spent: re-running the same steps is a new decision needing new approval.
    assert done.approval is None
    with pytest.raises(PlanStateError):
        store.check_executable(plan.plan_id, claimed_by=_claimed_by())


def test_a_step_the_device_now_refuses_halts_the_plan():
    """The layer-3 re-check. A plan approved against a ready robot must not
    fire into one that has since faulted or been seized by an external run."""
    store = PlanStore()
    service = Mock()
    service.claims.current.return_value = _claimed_by()
    service.allowed_actions.return_value = ["lights.set"]  # plate.unload withdrawn
    plan = _approved(store, "lights.set", "plate.unload")

    done = PlanExecutor(service, store).execute(plan.plan_id, claimed_by=_claimed_by())

    assert done.status == "failed"
    assert [r.outcome for r in done.results] == ["ok", "skipped"]
    assert "plate.unload" in done.halt_reason
    service.unload_plate.assert_not_called()


@pytest.mark.parametrize("final_step", [False, True])
def test_stop_between_steps_or_after_final_ack_prevents_plan_success(final_step):
    store = PlanStore()
    service = Mock()
    service.claims.current.return_value = _claimed_by()
    service._stop_latched = False
    service.allowed_actions.side_effect = lambda: (["stop", "shutdown"] if service._stop_latched else ["lights.set", "plate.unload"])
    service.set_lights.side_effect = lambda _: setattr(service, "_stop_latched", True)
    plan = _approved(store, *(["lights.set"] if final_step else ["lights.set", "plate.unload"]))
    done = PlanExecutor(service, store).execute(plan.plan_id, claimed_by=_claimed_by())
    assert done.status == "failed"
    assert done.approval is None
    service.unload_plate.assert_not_called()


def test_a_failing_step_halts_and_skips_the_rest():
    """Fail-fast, never continue-past-error: later steps of a pipetting
    sequence assume the earlier ones happened."""
    store = PlanStore()
    service = Mock()
    service.claims.current.return_value = _claimed_by()
    service.allowed_actions.return_value = ["lights.set", "plate.unload"]
    service.set_lights.side_effect = RuntimeError("robot said no")
    plan = _approved(store, "lights.set", "plate.unload")

    done = PlanExecutor(service, store).execute(plan.plan_id, claimed_by=_claimed_by())

    assert done.status == "failed"
    assert [r.outcome for r in done.results] == ["failed", "skipped"]
    assert "robot said no" in done.results[0].message
    service.unload_plate.assert_not_called()


def test_execute_refuses_an_unapproved_plan_without_touching_the_device():
    store = PlanStore()
    service = Mock()
    service.claims.current.return_value = _claimed_by()
    plan = store.create(_steps("lights.set"), created_by="agent")

    with pytest.raises(PlanStateError):
        PlanExecutor(service, store).execute(plan.plan_id, claimed_by=_claimed_by())

    service.set_lights.assert_not_called()
    service.allowed_actions.assert_not_called()


def test_abort_marks_pending_steps_skipped():
    store = PlanStore()
    plan = _approved(store, "lights.set", "plate.unload")
    aborted = store.abort(plan.plan_id, reason="aborted by operator")
    assert aborted.status == "aborted"
    assert [r.outcome for r in aborted.results] == ["skipped", "skipped"]
    assert aborted.approval is None


# ---------------------------------------------------------------------------
# HTTP surface — where the agent/human boundary actually lands
# ---------------------------------------------------------------------------


def _client() -> TestClient:
    return TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))


def _propose(client: TestClient, action: str = "lights.set", args=None) -> dict:
    resp = client.post(
        "/plans",
        json={"steps": [{"action": action, "args": args or {"on": True}}], "created_by": "agent"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_proposing_needs_no_claim_but_approving_does():
    """The load-bearing asymmetry.

    An agent has no claim token — the token lives in the operator's browser
    and is never handed out. So an agent can propose, revise and read, but
    every route that moves the robot is closed to it.
    """
    client = _client()
    plan = _propose(client)  # no claim, no token: accepted

    unapproved = client.post(
        f"/plans/{plan['plan_id']}/approve", json={"step_hash": plan["step_hash"]}
    )
    assert unapproved.status_code == 423  # claim token required

    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]
    ok = client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={"step_hash": plan["step_hash"]},
        headers={"X-Claim-Token": token},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved"
    assert ok.json()["executable"] is True


def test_approve_over_http_rejects_a_stale_hash():
    client = _client()
    plan = _propose(client)
    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]

    resp = client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={"step_hash": "0" * 64},
        headers={"X-Claim-Token": token},
    )
    assert resp.status_code == 409
    assert "re-read" in resp.json()["detail"]


def test_an_agent_cannot_run_even_an_approved_plan():
    """Hermes proposes; the human runs.

    `execute` is claim-gated, so a tokenless caller is refused even when the
    plan is fully approved. An agent's reach ends at the proposal — every path
    that actually moves the robot starts with a human clicking in the UI.
    """
    client = _client()
    plan = _propose(client)
    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]
    client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={"step_hash": plan["step_hash"]},
        headers={"X-Claim-Token": token},
    )
    assert client.get(f"/plans/{plan['plan_id']}").json()["executable"] is True

    # Approved and runnable — but not by anyone without the operator's token.
    assert client.post(f"/plans/{plan['plan_id']}/execute").status_code == 423

    # The operator's browser, holding the token, may run it.
    done = client.post(
        f"/plans/{plan['plan_id']}/execute", headers={"X-Claim-Token": token}
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "executed"


def test_the_operator_still_cannot_run_an_unapproved_plan():
    """Holding the claim is not the same as having reviewed the steps."""
    client = _client()
    plan = _propose(client)
    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]

    refused = client.post(
        f"/plans/{plan['plan_id']}/execute", headers={"X-Claim-Token": token}
    )
    assert refused.status_code == 409
    assert "not approved" in refused.json()["detail"]


def test_revising_over_http_voids_an_existing_approval():
    client = _client()
    plan = _propose(client)
    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]
    client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={"step_hash": plan["step_hash"]},
        headers={"X-Claim-Token": token},
    )

    revised = client.put(
        f"/plans/{plan['plan_id']}/steps",
        json={"steps": [{"action": "lights.set", "args": {"on": False}}]},
    )
    assert revised.status_code == 200
    assert revised.json()["status"] == "draft"
    assert revised.json()["executable"] is False

    # And the swapped-in step will not run — not even for the claim holder.
    refused = client.post(
        f"/plans/{plan['plan_id']}/execute", headers={"X-Claim-Token": token}
    )
    assert refused.status_code == 409


def test_plan_view_explains_why_it_cannot_run():
    """The review UI and an agent get the same reason string, so neither has to
    re-derive the rules."""
    client = _client()
    plan = _propose(client)
    body = client.get(f"/plans/{plan['plan_id']}").json()
    assert body["executable"] is False
    assert "not approved" in body["blocked_reason"]


def test_invalid_steps_are_rejected_by_the_http_surface():
    client = _client()
    assert client.post("/plans", json={"steps": [{"action": "nope", "args": {}}]}).status_code == 422
    assert (
        client.post(
            "/plans", json={"steps": [{"action": "aspirate", "args": {"pipette": "p300"}}]}
        ).status_code
        == 422
    )
    assert client.post("/plans", json={"steps": []}).status_code == 422


# ---------------------------------------------------------------------------
# Durable audit
#
# The approval itself lives in memory and dies with the process — right for a
# permission, wrong for a record. These two events are the only durable trace
# that a named human agreed to one exact step list. The per-step
# `control_action` rows say what ran; only these say who said yes, and to what.
# ---------------------------------------------------------------------------


def test_approval_and_execution_are_audited(monkeypatch):
    """Both halves of the pair, with the approver named on each."""
    from opentrons_server.gateway.events_exporter import EventsExporter

    emitted: list = []
    monkeypatch.setattr(
        EventsExporter, "emit", lambda self, event, **kw: emitted.append((event, kw)) or True
    )
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))

    plan = client.post(
        "/plans", json={"steps": [{"action": "lights.set", "args": {"on": True}}]}
    ).json()
    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]
    client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={"step_hash": plan["step_hash"]},
        headers={"X-Claim-Token": token},
    )

    approved = [e for e in emitted if e[0] == "plan_approved"]
    assert len(approved) == 1
    payload = approved[0][1]
    assert payload["owner"] == "ada@lab"
    assert payload["step_hash"] == plan["step_hash"]
    assert payload["steps"] == ["lights.set"]
    assert payload["proposed_by"]

    client.post(f"/plans/{plan['plan_id']}/execute", headers={"X-Claim-Token": token})

    executed = [e for e in emitted if e[0] == "plan_executed"]
    assert len(executed) == 1
    done = executed[0][1]
    assert done["status"] == "executed"
    assert done["outcomes"] == ["ok"]
    # The approval is spent by execute(), so the owner has to be captured
    # beforehand — otherwise the completion record loses the person who
    # authorised it.
    assert done["owner"] == "ada@lab"
    assert done["step_hash"] == plan["step_hash"]


def test_a_halted_plan_still_records_why(monkeypatch):
    """A refusal is exactly when someone will read the audit trail."""
    from opentrons_server.gateway.events_exporter import EventsExporter

    emitted: list = []
    monkeypatch.setattr(
        EventsExporter, "emit", lambda self, event, **kw: emitted.append((event, kw)) or True
    )
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))

    # `home` is not offered in dry run, so the pre-step re-check refuses it.
    plan = client.post("/plans", json={"steps": [{"action": "move_to", "args": {
        "pipette": "p300", "coordinates": {"x": 1, "y": 2, "z": 3}}}]}).json()
    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]
    client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={"step_hash": plan["step_hash"]},
        headers={"X-Claim-Token": token},
    )
    client.post(f"/plans/{plan['plan_id']}/execute", headers={"X-Claim-Token": token})

    done = [e for e in emitted if e[0] == "plan_executed"][0][1]
    assert done["status"] == "failed"
    assert done["outcomes"] == ["skipped"]
    assert "move_to" in done["halt_reason"]
