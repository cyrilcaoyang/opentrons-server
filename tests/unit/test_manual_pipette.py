"""Manual panel contract: fresh controller coordinates, bounded single steps, no retries."""

import ast
import socket
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opentrons_server.control.http_control import OT2HttpControl
from opentrons_server.control.http_run import CommandFailed, RunEngineClient, RunEngineCommands
from opentrons_server.control.ot2_control import OT2Control
from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.limits import MAX_X_MM, OutOfEnvelope
from opentrons_server.gateway.models import PipetteJogRequest, PipettePositionRequest
from opentrons_server.gateway.service import (
    JogReferenceRefused,
    OT2Service,
    OT2ServiceState,
    UnknownOutcomeError,
)


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


# --- HTTP critical-point mismatch (centerMultichannelOnWells) -----------------
#
# After a well of quirked labware the engine's savePosition reports a centered
# critical point, but moveToCoordinates targets the primary nozzle. A jog that
# adds a step to the centered read would shift a multi-channel pipette by up
# to half its nozzle span with no Z retract, so it must refuse before motion.

_RESERVOIR = {
    "namespace": "opentrons",
    "version": 1,
    "parameters": {"loadName": "nest_12_reservoir_15ml", "quirks": ["centerMultichannelOnWells"]},
}
_PLATE = {
    "namespace": "opentrons",
    "version": 2,
    "parameters": {"loadName": "corning_96_wellplate_360ul_flat"},
}


def _mirror_client(*, engine_well=None, known=True):
    client = Mock()
    client.engine_well = engine_well
    client.engine_well_known = known
    client.get_run.return_value = {
        "labware": [
            {"id": "reservoir", "definitionUri": "opentrons/nest_12_reservoir_15ml/1"},
            {"id": "plate", "definitionUri": "opentrons/corning_96_wellplate_360ul_flat/2"},
        ],
        "pipettes": [],
    }
    client.get_loaded_labware_definitions.return_value = [_RESERVOIR, _PLATE]
    client.execute.return_value = {"result": {"position": {"x": 100, "y": 200, "z": 50}}}
    return client


def _http_ready(client, model="p300_multi_gen2"):
    service, _ = ready()
    control = OT2HttpControl(client)
    control._pipette_ids.update({"p300": "p300", "other": "other"})
    control._pipette_models["p300"] = model
    service.control = control
    return service


@pytest.mark.parametrize(
    "engine_well, known",
    [(("p300", "reservoir"), True), (None, False)],
    ids=["centered-on-reservoir", "location-unconfirmed"],
)
def test_http_multichannel_jog_refuses_before_any_command_when_reference_may_be_centered(engine_well, known):
    client = _mirror_client(engine_well=engine_well, known=known)
    service = _http_ready(client)
    with pytest.raises(JogReferenceRefused, match="no move issued.*deck coordinates"):
        service.jog(PipetteJogRequest(pipette="p300", axis="z", distance_mm=1))
    client.execute.assert_not_called()  # no savePosition, no move
    assert service.state == OT2ServiceState.READY
    assert service.last_error is None


@pytest.mark.parametrize(
    "engine_well, model",
    [
        (("p300", "reservoir"), "p300_single_gen2"),  # one nozzle: centers coincide
        (("p300", "plate"), "p300_multi_gen2"),  # no quirk
        (("other", "reservoir"), "p300_multi_gen2"),  # location belongs to another pipette
        (None, "p300_multi_gen2"),  # coordinates / addressable area / homed
    ],
)
def test_http_jog_proceeds_when_read_and_move_share_the_reference(engine_well, model):
    client = _mirror_client(engine_well=engine_well)
    service = _http_ready(client, model=model)
    result = service.jog(PipetteJogRequest(pipette="p300", axis="z", distance_mm=1, speed=5))
    types = [call.args[0][0] for call in client.execute.call_args_list]
    assert types == ["savePosition", "moveToCoordinates", "savePosition"]
    assert client.execute.call_args_list[1].args[0][1]["coordinates"] == {"x": 100.0, "y": 200.0, "z": 51.0}
    assert result.coordinates.z == 50


def test_http_unconfirmed_location_does_not_refuse_single_channel():
    service = _http_ready(_mirror_client(known=False), model="p20_single_gen2")
    service.jog(PipetteJogRequest(pipette="p300", axis="x", distance_mm=1))


def test_http_model_falls_back_to_the_run_and_unknown_models_count_as_multichannel():
    client = _mirror_client(engine_well=("p300", "reservoir"))
    client.get_run.return_value["pipettes"] = [{"id": "p300", "pipetteName": "p20_single_gen2"}]
    control = OT2HttpControl(client)
    control._pipette_ids["p300"] = "p300"
    assert control.jog_reference_refusal("p300") is None
    client.get_run.return_value["pipettes"] = []
    control._pipette_models.clear()
    assert "nest_12_reservoir_15ml" in control.jog_reference_refusal("p300")


def test_jog_reference_refusal_is_a_412_precondition():
    app = create_app(dry_run=False, auto_reconnect=False, enforce_claims=False)
    service = app.state.service
    client = _mirror_client(engine_well=("p300", "reservoir"))
    control = OT2HttpControl(client)
    control._pipette_ids["p300"] = "p300"
    control._pipette_models["p300"] = "p300_multi_gen2"
    service.control = control
    service.state = OT2ServiceState.READY
    service.refresh_snapshot = Mock()
    with TestClient(app) as http:
        reply = http.post("/control/jog", json={"pipette": "p300", "axis": "y", "distance_mm": 1})
    assert reply.status_code == 412
    assert "nest_12_reservoir_15ml" in reply.json()["detail"]
    client.execute.assert_not_called()


class _RunEngineFake:
    """``RunEngineClient._request`` stand-in: answers commands by status."""

    def __init__(self):
        self.status = "succeeded"
        self.raises = None

    def __call__(self, method, path, **_kwargs):
        if path == "/runs":
            return {"id": "run-1"}
        if self.raises is not None:
            raise self.raises
        return {"status": self.status, "result": {}}


def _tracked_client():
    client = RunEngineClient("http://robot:31950")
    fake = _RunEngineFake()
    client._request = fake
    return client, fake


def test_run_client_mirrors_the_engine_current_well():
    client, fake = _tracked_client()
    assert client.engine_well_known is False  # no run, no history
    client.create_run()
    assert (client.engine_well, client.engine_well_known) == (None, True)

    client.execute(RunEngineCommands.move_to_well("p300", "reservoir", "A1"))
    assert client.engine_well == ("p300", "reservoir")
    # Non-well commands cannot put a pipette at a well; they leave it.
    client.execute(RunEngineCommands.save_position("p300"))
    client.execute(RunEngineCommands.aspirate_in_place("p300", 10, 50))
    client.execute(RunEngineCommands.move_labware("plate", {"slotName": "3"}))
    assert client.engine_well == ("p300", "reservoir")
    # Moving the labware under the pipette clears it, as the engine does.
    client.execute(RunEngineCommands.move_labware("reservoir", {"slotName": "3"}))
    assert client.engine_well is None

    client.execute(RunEngineCommands.aspirate("p300", "reservoir", "A2", 10, 50))
    assert client.engine_well == ("p300", "reservoir")
    client.execute(RunEngineCommands.move_to_coordinates("p300", {"x": 1, "y": 2, "z": 3}))
    assert client.engine_well is None
    client.execute(RunEngineCommands.pick_up_tip("other", "tips", "A1"))
    assert client.engine_well == ("other", "tips")
    client.execute(RunEngineCommands.home())
    assert (client.engine_well, client.engine_well_known) == (None, True)


@pytest.mark.parametrize("outcome", ["failed", "queued", "transport"])
def test_run_client_marks_location_unknown_after_an_unconfirmed_well_command(outcome):
    client, fake = _tracked_client()
    client.create_run()
    if outcome == "transport":
        fake.raises = OSError("connection reset")
    else:
        fake.status = outcome
    with pytest.raises((CommandFailed, OSError, RuntimeError)):
        client.execute(RunEngineCommands.move_to_well("p300", "reservoir", "A1"))
    assert client.engine_well_known is False
    # A failed reset proves nothing; a succeeded one re-establishes the location.
    fake.raises, fake.status = None, "failed"
    with pytest.raises(CommandFailed):
        client.execute(RunEngineCommands.home())
    assert client.engine_well_known is False
    fake.status = "succeeded"
    client.execute(RunEngineCommands.home())
    assert (client.engine_well, client.engine_well_known) == (None, True)
