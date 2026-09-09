"""Offline endpoint, state, geometry-addressing and interruption regressions."""
import threading
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opentrons_server.gateway.advanced import ADVANCED_ACTIONS, BlowOutRequest, AirGapRequest
from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.plans import PLAN_ACTIONS, PlanStep
from opentrons_server.gateway.service import OT2ServiceState, UnknownOutcomeError
from opentrons_server.gateway.limits import OutOfEnvelope


@pytest.fixture
def app(tmp_path, monkeypatch):
    for kind in ("PLATE", "TIP", "DECK"):
        monkeypatch.setenv(f"OT2_{kind}_STATE_PATH", str(tmp_path / f"{kind}.json"))
    app = create_app(dry_run=True, auto_reconnect=False)
    svc = app.state.service
    svc.control = Mock()
    svc.refresh_snapshot = Mock(return_value={})
    svc._ensure_session_pipette = Mock(return_value="p300")
    svc._resolve_session_labware = Mock(return_value="plate")
    svc._ensure_session_module = Mock(return_value="module1")
    svc._mark_tip_used = Mock()
    svc.dry_run = False
    svc.transport = "http"
    svc.state = OT2ServiceState.READY
    return app


def run(svc, name, **args):
    svc.advanced_action(name, ADVANCED_ACTIONS[name].model(**args))


WELL = {"labware_nickname": "2", "position": "A1"}


@pytest.mark.parametrize("kwargs", [{}, {"location": WELL, "in_place": True}])
def test_blowout_requires_unambiguous_destination(kwargs):
    with pytest.raises(ValidationError):
        BlowOutRequest(pipette="left", **kwargs)


def test_touch_tip_resolves_objects_and_preserves_contact_tracking(app):
    svc = app.state.service
    run(svc, "touch_tip", pipette="left", labware_nickname="2", position="A1", radius=0.9)
    svc.control.touch_tip.assert_called_once_with("p300", "plate", "A1", radius=0.9, v_offset=-1, speed=60)
    svc._mark_tip_used.assert_called_once_with("left", "2", "A1")


def test_blowout_in_place_does_not_reuse_pending_location(app):
    svc = app.state.service
    run(svc, "blow_out", pipette="left", in_place=True)
    svc.control.blow_out_in_place.assert_called_once_with("p300")
    svc.control.get_location_from_labware.assert_not_called()
    svc._mark_tip_used.assert_not_called()


def test_blowout_well_and_mix_default_origins(app):
    svc = app.state.service
    run(svc, "blow_out", pipette="left", location=WELL)
    assert svc.control.get_location_from_labware.call_args.kwargs["default_origin"] == "top"
    run(svc, "mix", pipette="left", location=WELL, volume_ul=50, repetitions=3)
    svc.control.mix.assert_called_once_with("p300", 3, 50, rate=1)
    assert svc.control.get_location_from_labware.call_args.kwargs["default_origin"] == "bottom"


def test_airgap_checks_combined_volume_before_motion(app):
    svc = app.state.service
    svc._volume_limits_for = Mock(return_value=(20, 300))
    svc.control.current_volume.return_value = 290
    with pytest.raises(OutOfEnvelope):
        run(svc, "air_gap", pipette="left", location=WELL, volume_ul=30)
    svc.control.move_to_pip.assert_not_called()


def test_airgap_requires_explicit_well_and_rejects_ignored_offsets():
    with pytest.raises(ValidationError):
        AirGapRequest(pipette="left", volume_ul=20)
    with pytest.raises(ValidationError):
        AirGapRequest(pipette="left", volume_ul=20, location={**WELL, "bottom": 2})


@pytest.mark.parametrize("state", [OT2ServiceState.BUSY, OT2ServiceState.ERROR, OT2ServiceState.PAUSED,
                                   OT2ServiceState.UNKNOWN_OUTCOME, OT2ServiceState.EXTERNAL_CONTROL])
def test_new_motion_refused_without_calling_transport(app, state):
    svc = app.state.service
    svc.state = state
    with pytest.raises(RuntimeError):
        run(svc, "blow_out", pipette="left", in_place=True)
    svc.control.blow_out_in_place.assert_not_called()
    assert "blow_out" not in svc.allowed_actions()


def test_transport_loss_during_touch_is_unknown_and_not_retried(app):
    svc = app.state.service
    svc.control.touch_tip.side_effect = OSError("disconnected")
    with pytest.raises(UnknownOutcomeError):
        run(svc, "touch_tip", pipette="left", labware_nickname="2", position="A1")
    assert svc.state == OT2ServiceState.UNKNOWN_OUTCOME
    assert svc.control.touch_tip.call_count == 1
    svc._mark_tip_used.assert_not_called()


def test_routes_and_plans_share_validation_and_claim_gate(app):
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]
        for name, spec in ADVANCED_ACTIONS.items():
            assert spec.path in paths
            assert PLAN_ACTIONS[name].model is spec.model
        body = {"pipette": "left", "in_place": True}
        assert client.post("/control/blow-out", json=body).status_code == 423
        claim = client.post("/control/claim", json={"owner": "operator", "session_id": "test", "ttl_s": 60}).json()
        headers = {"X-Claim-Token": claim["claim_token"]}
        assert client.post("/control/blow-out", json=body, headers=headers).status_code == 200
        assert client.post("/control/blow-out", json={**body, "volume": 5}, headers=headers).status_code == 422
        assert client.post("/control/touch-tip", json={"pipette":"left", "labware_nickname":"2", "position":"A1", "radius":1.1}, headers=headers).status_code == 422


def test_every_module_action_passes_typed_arguments(app):
    svc = app.state.service
    values = {"celsius": 40, "temperature": 60, "rpm": 500, "height_from_base": 5}
    for name, spec in ADVANCED_ACTIONS.items():
        if not spec.family:
            continue
        args = {"module": "module1"}
        for key, field in spec.model.model_fields.items():
            if field.is_required() and key != "module":
                args[key] = values[key]
        run(svc, name, **args)
        svc._ensure_session_module.assert_called_with("module1", family=spec.family)
        getattr(svc.control, spec.method).assert_called_once()


def test_stop_remains_available_while_command_runs_and_late_success_cannot_clear_it(app):
    svc = app.state.service
    entered, finish = threading.Event(), threading.Event()
    errors = []
    def blocking():
        entered.set()
        assert finish.wait(5)
    def worker():
        try:
            svc._run_action("mix", blocking, idempotent=False)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        assert "stop" in svc.allowed_actions()
        svc.stop()
        assert svc._stop_confirmed
        assert svc._operator_shutdown
        with pytest.raises(RuntimeError):
            svc.reconcile()
        with pytest.raises(RuntimeError):
            svc.startup()
        finish.set()
        thread.join(5)
        assert not thread.is_alive()
        assert isinstance(errors[0], UnknownOutcomeError)
        assert svc.state == OT2ServiceState.ERROR
        assert svc._cycles_total == 0
        with pytest.raises(RuntimeError):
            svc.home()
    finally:
        finish.set()
        thread.join(5)


def test_stop_failure_is_unknown_never_ready(app):
    svc = app.state.service
    svc.control.client.stop_and_confirm.side_effect = TimeoutError("no reply")
    with pytest.raises(TimeoutError):
        svc.stop()
    assert svc.state == OT2ServiceState.UNKNOWN_OUTCOME
    assert svc._observed_activity() == "unknown"
    assert svc._stop_latched and not svc._stop_confirmed


def test_ssh_stop_is_not_falsely_advertised(app):
    svc = app.state.service
    svc.transport = "ssh"
    assert "stop" not in svc.allowed_actions()
    with pytest.raises(RuntimeError, match="HTTP"):
        svc.stop()
    svc.control.client.stop_and_confirm.assert_not_called()


def test_stop_uses_separate_connection_and_requires_readback(monkeypatch):
    import opentrons_server.control.http_run as module
    client = module.RunEngineClient("http://synthetic-robot.invalid", session=Mock())
    client.run_id = "run-owned"
    connection = Mock()
    connection.get_run.return_value = {"status": "stopped"}
    factory = Mock(return_value=connection)
    monkeypatch.setattr(module, "RunEngineClient", factory)
    client.stop_and_confirm()
    assert connection.run_id == "run-owned"
    connection._request.assert_called_once_with(
        "POST", "/runs/run-owned/actions", json_body={"data":{"actionType":"stop"}}, timeout=connection.request_timeout_s)
    connection.get_run.assert_called_once()
    connection.close.assert_called_once()
    client._session.request.assert_not_called()
    with pytest.raises(OSError, match="stopped"):
        client.execute(("home", {}))


def test_stop_http_acknowledgment_alone_is_not_success(monkeypatch):
    import opentrons_server.control.http_run as module
    client = module.RunEngineClient("http://synthetic-robot.invalid", session=Mock())
    client.run_id = "run-owned"
    connection = Mock()
    connection.get_run.return_value = {"status": "running"}
    monkeypatch.setattr(module, "RunEngineClient", Mock(return_value=connection))
    with pytest.raises(TimeoutError, match="did not confirm"):
        client.stop_and_confirm(timeout_s=0.001)
    assert client._stop_requested.is_set()
    connection.close.assert_called_once()


@pytest.mark.parametrize("direct", [False, True])
def test_gateway_preserves_direct_motion_option(app, direct):
    from opentrons_server.gateway.models import MoveToRequest
    svc = app.state.service
    svc.move_to(MoveToRequest(pipette="left", coordinates={"x":100,"y":200,"z":30}, force_direct=direct))
    svc.control.move_to_pip.assert_called_once_with("p300", speed=None, force_direct=direct or None, minimum_z_height=None)
    svc.control.home.assert_not_called()


def test_stop_latch_survives_gateway_restart(app, monkeypatch):
    from opentrons_server.gateway.service import OT2Service
    svc = app.state.service
    svc.stop()
    assert svc._stop_latch_path.exists()
    restarted = OT2Service(dry_run=False, transport="http", tips=svc.tips, decks=svc.decks, plates=svc.plates)
    restarted.probe_robot = Mock(side_effect=AssertionError("must not reconnect"))
    restarted.boot_reconnect()
    assert restarted._stop_latched and restarted._operator_shutdown
    assert "startup" in restarted.allowed_actions()
    assert "home" not in restarted.allowed_actions()


def test_zero_well_offset_is_not_discarded(app):
    svc = app.state.service
    run(svc, "blow_out", pipette="left", location={**WELL, "bottom":0})
    kwargs = svc.control.get_location_from_labware.call_args.kwargs
    assert (kwargs["default_origin"], kwargs["default_offset"]) == ("bottom", 0)


def test_failed_mix_does_not_leave_tip_record_fresh(app):
    from datetime import datetime, timezone
    from opentrons_server.gateway.tip_state import TipMount
    svc = app.state.service
    svc.tips.set_mount(TipMount(pipette="left", rack="1", well="A1", picked_at=datetime.now(timezone.utc)))
    svc.control.mix.side_effect = OSError("lost reply after a partial mix")
    with pytest.raises(UnknownOutcomeError):
        run(svc, "mix", pipette="left", location=WELL, volume_ul=50, repetitions=3)
    mount = svc.tips.get_mount("left")
    assert mount.uncertain and mount.last_sample == "unknown"
    svc._mark_tip_used.assert_not_called()
