"""Manual panel contract: fresh controller coordinates, bounded single steps, no retries."""

import ast
import socket
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opentrons_server.control.http_control import OT2HttpControl
from opentrons_server.control.http_run import RunEngineCommands
from opentrons_server.control.ot2_control import OT2Control
from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.limits import MAX_X_MM, OutOfEnvelope
from opentrons_server.gateway.models import PipetteJogRequest, PipettePositionRequest
from opentrons_server.gateway.service import OT2Service, OT2ServiceState, UnknownOutcomeError


def ready():
    service = OT2Service(dry_run=False)
    control = Mock()
    service.control = control
    service.state = OT2ServiceState.READY
    service.refresh_snapshot = Mock()
    return service, control


def test_position_is_explicit_fresh_read_never_status_side_effect():
    service, control = ready()
    control.get_pipette_position.return_value = {"x": 10, "y": 20, "z": 30}
    service.get_status()
    control.get_pipette_position.assert_not_called()
    result = service.pipette_position(PipettePositionRequest(pipette="p300"))
    assert result.coordinates.model_dump() == {"x": 10, "y": 20, "z": 30}
    assert result.source == "robot"
    control.get_location_absolute.assert_not_called()
    control.move_to_pip.assert_not_called()
    assert service.state == OT2ServiceState.READY


def test_simulated_position_is_labeled_as_simulation():
    service, control = ready()
    service.simulation = True
    control.get_pipette_position.return_value = {"x": 1, "y": 2, "z": 3}
    assert service.pipette_position(PipettePositionRequest(pipette="p300")).source == "simulation"


def test_jog_reads_moves_straight_at_selected_speed_then_reports_actual_readback():
    service, control = ready()
    control.get_pipette_position.side_effect = [
        {"x": 10, "y": 20, "z": 30},
        {"x": 10.095, "y": 20, "z": 30},
        {"x": 40, "y": 50, "z": 60},
        {"x": 40, "y": 50, "z": 59},
    ]
    first = service.jog(PipetteJogRequest(pipette="p300", axis="x", distance_mm=0.1, speed=5))
    control.get_location_absolute.assert_called_once_with(10.1, 20.0, 30.0)
    control.move_to_pip.assert_called_once_with("p300", speed=5, force_direct=True)
    assert first.coordinates.x == 10.095  # Never substitute the requested 10.1.
    service.jog(PipetteJogRequest(pipette="p300", axis="z", distance_mm=-1, speed=10))
    control.get_location_absolute.assert_called_with(40.0, 50.0, 59.0)


@pytest.mark.parametrize(
    "args",
    [
        {"distance_mm": 0},
        {"distance_mm": 11},
        {"distance_mm": -11},
        {"distance_mm": float("nan")},
        {"speed": 0},
        {"speed": 101},
        {"speed": float("inf")},
        {"axis": "a"},
        {"coordinates": {"x": 1}},
    ],
)
def test_jog_static_bounds_and_no_browser_position(args):
    with pytest.raises(ValidationError):
        PipetteJogRequest.model_validate({"pipette": "p300", "axis": "x", "distance_mm": 1, **args})


def test_jog_destination_bound_refusal_is_pre_motion_and_does_not_latch_fault():
    service, control = ready()
    control.get_pipette_position.return_value = {"x": MAX_X_MM, "y": 20, "z": 30}
    with pytest.raises(OutOfEnvelope):
        service.jog(PipetteJogRequest(pipette="p300", axis="x", distance_mm=1))
    control.move_to_pip.assert_not_called()
    assert service.state == OT2ServiceState.READY
    assert service.last_error is None


@pytest.mark.parametrize(
    "position",
    [None, {}, {"x": 1, "y": 2}, {"x": float("nan"), "y": 2, "z": 3}, {"x": True, "y": 2, "z": 3}],
)
def test_unknown_position_never_becomes_a_target(position):
    service, control = ready()
    control.get_pipette_position.return_value = position
    with pytest.raises(ValueError):
        service.jog(PipetteJogRequest(pipette="p300", axis="z", distance_mm=1))
    control.move_to_pip.assert_not_called()


def test_unhomed_read_does_not_home_or_move():
    service, control = ready()
    control.get_pipette_position.side_effect = RuntimeError("PositionUnknownError: home axes first")
    with pytest.raises(RuntimeError, match="PositionUnknownError"):
        service.pipette_position(PipettePositionRequest(pipette="p300"))
    control.home.assert_not_called()
    control.move_to_pip.assert_not_called()


def test_transport_loss_during_relative_motion_is_unknown_outcome_and_never_retried():
    service, control = ready()
    control.get_pipette_position.return_value = {"x": 10, "y": 20, "z": 30}
    control.move_to_pip.side_effect = socket.timeout("lost during step")
    with pytest.raises(UnknownOutcomeError):
        service.jog(PipetteJogRequest(pipette="p300", axis="x", distance_mm=1))
    control.move_to_pip.assert_called_once()
    assert service.state == OT2ServiceState.UNKNOWN_OUTCOME
    assert "jog" not in service.allowed_actions()


@pytest.mark.parametrize(
    "state",
    [
        OT2ServiceState.BUSY,
        OT2ServiceState.PAUSED,
        OT2ServiceState.ERROR,
        OT2ServiceState.UNKNOWN_OUTCOME,
        OT2ServiceState.EXTERNAL_CONTROL,
    ],
)
def test_manual_actions_refused_outside_ready(state):
    service, control = ready()
    service.state = state
    assert "jog" not in service.allowed_actions()
    assert "pipette_position" not in service.allowed_actions()
    with pytest.raises(RuntimeError):
        service.jog(PipetteJogRequest(pipette="p300", axis="x", distance_mm=1))
    control.get_pipette_position.assert_not_called()


def test_command_lock_and_stop_latch_prevent_jog():
    service, control = ready()
    service._command_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="in flight"):
            service.jog(PipetteJogRequest(pipette="p300", axis="x", distance_mm=1))
    finally:
        service._command_lock.release()
    service._stop_latched = True
    with pytest.raises(RuntimeError, match="stopped"):
        service.jog(PipetteJogRequest(pipette="p300", axis="x", distance_mm=1))
    control.get_pipette_position.assert_not_called()


def test_http_position_uses_homed_save_position_and_observed_mount_id():
    client = Mock()
    client.get_run.return_value = {"pipettes": [{"id": "existing_p300", "mount": "left"}]}
    client.execute.return_value = {"result": {"position": {"x": 1, "y": 2, "z": 3}}}
    control = OT2HttpControl(client)
    assert control.pipette_for_mount("left") == "existing_p300"
    assert control.get_pipette_position("existing_p300") == {"x": 1, "y": 2, "z": 3}
    client.execute.assert_called_once_with(
        ("savePosition", {"pipetteId": "existing_p300", "failOnNotHomed": True})
    )
    assert RunEngineCommands.save_position("p") == (
        "savePosition",
        {"pipetteId": "p", "failOnNotHomed": True},
    )


def test_http_missing_position_fails_loudly():
    client = Mock()
    client.execute.return_value = {"result": {}}
    control = OT2HttpControl(client)
    control._pipette_ids["p"] = "p"
    with pytest.raises(ValueError, match="complete XYZ"):
        control.get_pipette_position("p")


def test_ssh_position_is_one_expression_with_homing_guard_and_no_motion():
    control = object.__new__(OT2Control)
    control.invoke = Mock(return_value='echo\r\n{"x": 1, "y": 2, "z": 3}\r\n>>> ')
    assert control.get_pipette_position("p300") == {"x": 1, "y": 2, "z": 3}
    code = control.invoke.call_args.args[0]
    assert len(ast.parse(code).body) == 1
    assert "refresh=True, fail_on_not_homed=True" in code
    assert "p300._core.get_mount()" in code
    assert "move" not in code
    with pytest.raises(ValueError):
        control.get_pipette_position("p300); forbidden()")


def test_manual_mount_resolves_existing_http_pipette_without_duplicate_load():
    service, _ = ready()
    client = Mock()
    client.get_run.return_value = {"pipettes": [{"id": "actual_id", "mount": "right"}]}
    client.execute.return_value = {"result": {"position": {"x": 1, "y": 2, "z": 3}}}
    service.control = OT2HttpControl(client)
    result = service.pipette_position(PipettePositionRequest(pipette="right"))
    assert result.coordinates.z == 3
    assert all(call.args[0][0] != "loadPipette" for call in client.execute.call_args_list)


def test_endpoints_require_claim_and_dry_run_never_invents_position():
    app = create_app(dry_run=True, auto_reconnect=False, enforce_claims=True)
    with TestClient(app) as client:
        for path, body in (
            ("pipette-position", {"pipette": "left"}),
            ("jog", {"pipette": "left", "axis": "x", "distance_mm": 1}),
        ):
            assert client.post(f"/control/{path}", json=body).status_code == 423
    app = create_app(dry_run=True, auto_reconnect=False, enforce_claims=False)
    with TestClient(app) as client:
        read = client.post("/control/pipette-position", json={"pipette": "left"})
        assert read.status_code == 200
        assert read.json()["source"] == "dry_run"
        assert read.json()["coordinates"] is None
        jog = client.post(
            "/control/jog", json={"pipette": "left", "axis": "z", "distance_mm": -0.1, "speed": 1}
        )
        assert jog.status_code == 200
        assert jog.json()["coordinates"] is None
        assert (
            client.post(
                "/control/jog", json={"pipette": "left", "axis": "z", "distance_mm": 50}
            ).status_code
            == 422
        )


def test_ssh_home_z_targets_selected_mount_without_plunger():
    control = object.__new__(OT2Control)
    control.invoke = Mock()
    control.home_pipette_z("left_pipette")
    code = control.invoke.call_args.args[0]
    ast.parse(code, mode="eval")
    assert "Axis.by_mount(left_pipette._core.get_mount())" in code
    assert "get_hardware().home([" in code
    assert "plunger" not in code
    with pytest.raises(ValueError):
        control.home_pipette_z("left;bad()")
