"""Operator pause lands at a command boundary: the running command finishes,
the gateway holds before the next one, and an executing plan waits there until
resume. Nothing here interrupts motion (that is `stop`)."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from opentrons_server.gateway.deck import DeckDeclarationStore
from opentrons_server.gateway.plans import PlanExecutor, PlanStep, PlanStore
from opentrons_server.gateway.plate_state import PlateStateStore
from opentrons_server.gateway.service import OT2Service, OT2ServiceState
from opentrons_server.gateway.tip_state import TipStateStore
from opentrons_server.gateway.models import ClaimedBy
from datetime import datetime, timedelta, timezone


@pytest.fixture
def service(tmp_path):
    svc = OT2Service(
        dry_run=False,
        plates=PlateStateStore(state_path=tmp_path / "plate.json"),
        decks=DeckDeclarationStore(state_path=tmp_path / "deck.json"),
        tips=TipStateStore(state_path=tmp_path / "tips.json"),
    )
    svc.control = Mock()
    svc.refresh_snapshot = Mock(return_value={})
    svc.state = OT2ServiceState.READY
    return svc


def test_pause_while_idle_pauses_at_once(service):
    service.pause()
    assert service.state == OT2ServiceState.PAUSED
    allowed = set(service.allowed_actions())
    assert {"resume", "shutdown"} <= allowed and "pause" not in allowed
    assert not allowed & {"pick_up_tip", "aspirate", "dispense", "move_to", "home"}
    service.resume()
    assert service.state == OT2ServiceState.READY


def test_pause_during_a_command_is_honoured_when_it_ends(service):
    """The command keeps running; the gateway becomes PAUSED instead of READY
    as it completes, and the request is visible in /status meanwhile."""
    started, release = threading.Event(), threading.Event()

    def slow_command():
        started.set()
        release.wait(5)

    worker = threading.Thread(
        target=lambda: service._run_action("home", slow_command, idempotent=True)
    )
    worker.start()
    assert started.wait(2)
    assert service.state == OT2ServiceState.BUSY

    service.pause()  # no error, no interruption
    assert service.state == OT2ServiceState.BUSY
    assert service.get_status().details["pause_requested"] is True
    # No protocol action is advertised while one is in flight (§2.3); the
    # pending pause is cancelled by `resume` once the gateway has paused.
    assert "resume" not in service.allowed_actions()

    release.set()
    worker.join(5)
    assert service.state == OT2ServiceState.PAUSED
    assert service.get_status().details["pause_requested"] is False
    assert service.last_error is None


def test_resume_during_a_command_cancels_a_pending_pause(service):
    started, release = threading.Event(), threading.Event()

    def slow_command():
        started.set()
        release.wait(5)

    worker = threading.Thread(
        target=lambda: service._run_action("home", slow_command, idempotent=True)
    )
    worker.start()
    assert started.wait(2)
    service.pause()
    service.resume()
    release.set()
    worker.join(5)
    assert service.state == OT2ServiceState.READY


def test_resume_during_a_command_without_a_pending_pause_is_refused(service):
    started, release = threading.Event(), threading.Event()

    def slow_command():
        started.set()
        release.wait(5)

    worker = threading.Thread(
        target=lambda: service._run_action("home", slow_command, idempotent=True)
    )
    worker.start()
    assert started.wait(2)
    with pytest.raises(RuntimeError, match="in flight"):
        service.resume()
    release.set()
    worker.join(5)


def test_a_failed_command_does_not_pause_an_error(service):
    def failing():
        raise RuntimeError("boom")

    service._pause_requested = True
    with pytest.raises(RuntimeError):
        service._run_action("home", failing, idempotent=True)
    assert service.state == OT2ServiceState.ERROR
    assert service._pause_requested is False


# --- plan executor ---------------------------------------------------------------

def _claimed_by() -> ClaimedBy:
    return ClaimedBy(
        session_id="sess-1", owner="ada@lab",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def _approved_plan(store: PlanStore, *actions: str):
    plan = store.create([PlanStep(action=a, args={"on": True} if a == "lights.set" else {})
                         for a in actions], created_by="agent")
    store.approve(plan.plan_id, step_hash=plan.step_hash, claimed_by=_claimed_by())
    return plan


def _executor_service(state: str):
    service = Mock()
    service.state = SimpleNamespace(value=state)
    service.claims.current.return_value = _claimed_by()
    service.allowed_actions.return_value = ["lights.set", "plate.unload"]
    return service


def test_executing_plan_waits_at_the_step_boundary_while_paused():
    store = PlanStore()
    service = _executor_service("ready")
    plan = _approved_plan(store, "lights.set", "plate.unload")
    gate = threading.Event()

    def first_step(_on):
        gate.set()
        service.state = SimpleNamespace(value="paused")  # operator paused mid-step

    service.set_lights.side_effect = first_step
    done = {}
    worker = threading.Thread(
        target=lambda: done.update(plan=PlanExecutor(service, store).execute(
            plan.plan_id, claimed_by=_claimed_by())))
    worker.start()
    assert gate.wait(2)
    time.sleep(0.6)
    service.unload_plate.assert_not_called()  # held before step 2
    assert store.get(plan.plan_id).status == "executing"

    service.state = SimpleNamespace(value="ready")  # operator pressed play
    worker.join(5)
    assert done["plan"].status == "executed"
    service.unload_plate.assert_called_once()


def test_abort_ends_a_paused_wait():
    store = PlanStore()
    service = _executor_service("paused")
    plan = _approved_plan(store, "lights.set")
    done = {}
    worker = threading.Thread(
        target=lambda: done.update(plan=PlanExecutor(service, store).execute(
            plan.plan_id, claimed_by=_claimed_by())))
    worker.start()
    time.sleep(0.5)
    service.set_lights.assert_not_called()
    store.abort(plan.plan_id, reason="operator aborted while paused")
    worker.join(5)
    assert done["plan"].status == "aborted"
    assert [r.outcome for r in done["plan"].results] == ["skipped"]
    service.set_lights.assert_not_called()
