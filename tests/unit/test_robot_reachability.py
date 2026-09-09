"""The robot reachability monitor: /status must not say `ready` about a robot
nobody can reach.

Background: the gateway's session state machine only moves when a *command*
fails, and until 2026-09 a failed probe was discarded, so `details.robot.reachable`
froze at `true` and `equipment_status` stayed `ready` for hours after the robot
vanished. STATUS_SPEC §2.1 says a gateway that cannot reach its hardware
reports `unknown` (not `error`); §6.2 says `allowed_actions` and the endpoints'
refusals must agree; §6.3 says a refusal never touches `last_error`.
"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from opentrons_server.control.http_run import RunEngineHTTPError
from opentrons_server.gateway.models import EquipmentStatus
from opentrons_server.gateway.service import (
    _OFFLINE_SAFE_ACTIONS,
    _OT2_UNREACHABLE_AFTER,
    OT2Service,
    OT2ServiceState,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _probe(*, fail: bool):
    """A requests.get stand-in for the robot-server endpoints probe_robot reads."""

    def fake_get(url, **kwargs):
        if fail:
            raise ConnectionError("connect timeout")
        if url.endswith("/health"):
            return _Resp({"api_version": "8.7.0", "robot_model": "OT-2 Standard", "name": "ot2training"})
        if url.endswith("/runs"):
            return _Resp({"data": []})
        return _Resp({"data": []})

    return fake_get


def _ready_http_service(monkeypatch, *, run_current=True) -> OT2Service:
    """A gateway holding a live http session against a reachable robot."""

    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", _probe(fail=False))
    service = OT2Service(dry_run=False, host_alias="192.168.0.9", transport="http")
    service.control = Mock()
    service.control.client.hostname = None  # a Mock hostname would leak into the probe URL
    service.control.client.run_id = "run-1"
    service.control.client.get_run.return_value = {"id": "run-1", "current": run_current}
    service.state = OT2ServiceState.READY
    service._boot_started = True
    service._refresh_identity()
    assert service.get_status().equipment_status == "ready"
    return service


def _fail_probes(monkeypatch, service: OT2Service, n: int) -> None:
    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", _probe(fail=True))
    for _ in range(n):
        service._refresh_identity()


def test_probe_failures_below_threshold_do_not_declare_an_outage(monkeypatch):
    """One 2 s timeout on a robot busy loading a protocol is not an outage."""

    service = _ready_http_service(monkeypatch)
    _fail_probes(monkeypatch, service, _OT2_UNREACHABLE_AFTER - 1)

    status = service.get_status()
    assert status.equipment_status == "ready"
    assert service.robot_unreachable is False
    assert status.details["robot"]["probe_failures"] == _OT2_UNREACHABLE_AFTER - 1
    # The last *answered* probe is what reachable describes, with its age.
    assert status.details["robot"]["reachable"] is True
    assert status.details["robot"]["readback_age_s"] is not None


def test_threshold_reached_reports_unknown_not_ready_and_not_error(monkeypatch):
    service = _ready_http_service(monkeypatch)
    _fail_probes(monkeypatch, service, _OT2_UNREACHABLE_AFTER)

    status = service.get_status()
    assert status.equipment_status == "unknown"  # §2.1, never `error`
    assert status.activity == "unknown"
    assert status.last_error is None
    assert "unreachable" in status.message.lower()
    assert "192.168.0.9" in status.message
    assert status.required_actions == []
    robot = status.details["robot"]
    assert robot["reachable"] is False
    assert robot["unreachable_since"] is not None
    assert robot["probe_failures"] == _OT2_UNREACHABLE_AFTER
    assert robot["robot_name"] == "ot2training"  # identity kept, reachability not
    assert status.components["robot"].connected is False
    assert status.components["robot"].state == "unreachable"
    assert status.components["control"].connected is False  # http liveness follows the probe


def test_allowed_actions_shrink_to_offline_safe_set(monkeypatch):
    service = _ready_http_service(monkeypatch)
    assert "home" in service.allowed_actions()

    _fail_probes(monkeypatch, service, _OT2_UNREACHABLE_AFTER)

    actions = set(service.allowed_actions())
    assert actions <= _OFFLINE_SAFE_ACTIONS | {"stop"}
    # A software stop may still attempt its own connection during an outage.
    assert "stop" in actions
    assert "shutdown" in actions
    assert not actions & {"home", "setup", "aspirate", "lights.set", "startup"}


def test_command_is_refused_up_front_without_touching_last_error(monkeypatch):
    """§6.2 / §6.3: the endpoint refuses what allowed_actions withholds, and a
    refusal is not an operational failure -- no 20 s wait on a dead socket, no
    `error` state, no last_error."""

    service = _ready_http_service(monkeypatch)
    _fail_probes(monkeypatch, service, _OT2_UNREACHABLE_AFTER)

    with pytest.raises(RuntimeError, match="unreachable since"):
        service.home()

    service.control.home.assert_not_called()
    assert service.last_error is None
    assert service.state == OT2ServiceState.READY  # session untouched, only refused


def test_recovery_restores_ready_when_the_run_survived(monkeypatch):
    """A robot that merely lost its network keeps its run: nothing to rebuild."""

    service = _ready_http_service(monkeypatch)
    _fail_probes(monkeypatch, service, _OT2_UNREACHABLE_AFTER)
    assert service.get_status().equipment_status == "unknown"

    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", _probe(fail=False))
    service._refresh_identity()

    status = service.get_status()
    assert status.equipment_status == "ready"
    assert status.activity == "idle"
    assert status.details["robot"]["reachable"] is True
    assert status.details["robot"]["unreachable_since"] is None
    assert status.components["robot"].state == "reachable"
    assert service.control is not None  # session kept


def test_recovery_rebuilds_session_when_the_robot_forgot_the_run(monkeypatch):
    """A robot that rebooted during the outage has no memory of our run. The
    next command would 409 forever; instead the session is dropped and the
    existing requires_init self-heal re-initialises on the same tick."""

    service = _ready_http_service(monkeypatch)
    service.control.client.get_run.side_effect = RunEngineHTTPError(404, "not found", path="/runs/run-1")
    _fail_probes(monkeypatch, service, _OT2_UNREACHABLE_AFTER)

    reinit = Mock()

    def fake_startup(self, **kwargs):
        reinit()
        self.control = Mock()
        self.state = OT2ServiceState.READY

    monkeypatch.setattr(OT2Service, "startup", fake_startup)
    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", _probe(fail=False))
    service._refresh_identity()

    reinit.assert_called_once()
    assert service.state == OT2ServiceState.READY
    assert service.get_status().equipment_status == "ready"


def test_recovery_leaves_an_operator_shutdown_alone(monkeypatch):
    """`_operator_shutdown` is the standing guard: a robot coming back never
    undoes a deliberate shutdown, outage or not."""

    service = _ready_http_service(monkeypatch)
    service.shutdown()
    assert service.control is None
    _fail_probes(monkeypatch, service, _OT2_UNREACHABLE_AFTER)

    monkeypatch.setattr(OT2Service, "startup", Mock(side_effect=AssertionError("must not start")))
    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", _probe(fail=False))
    service._refresh_identity()

    assert service.state == OT2ServiceState.REQUIRES_INIT


def test_stale_run_409_drops_the_session_for_the_self_heal(monkeypatch):
    """ROADMAP 'self-heal the stale-run 409': a 409 "not the current run" is
    definitive -- the run died with the robot -- so the gateway forgets it
    instead of latching `error` until an operator cycles shutdown/startup."""

    service = _ready_http_service(monkeypatch)
    service.control.home.side_effect = RunEngineHTTPError(
        409, "Run run-1 is not the current run", path="/runs/run-1/commands"
    )

    with pytest.raises(RunEngineHTTPError):
        service.home()

    assert service.control is None
    assert service.state == OT2ServiceState.REQUIRES_INIT
    assert service.last_error is not None and service.last_error.code == "command_failed"
    assert "restarted" in (service._status_note or "").lower()


def test_other_409s_keep_the_session():
    service = OT2Service(dry_run=False, transport="http")
    service.control = Mock()
    service.state = OT2ServiceState.READY
    service.control.home.side_effect = RunEngineHTTPError(409, "robot is busy", path="/x")

    with pytest.raises(RunEngineHTTPError):
        service.home()

    assert service.control is not None
    assert service.state == OT2ServiceState.ERROR


def test_boot_with_robot_off_becomes_unknown_once_the_threshold_is_reached(monkeypatch):
    """boot_reconnect's failed probe is the first strike; the refresh loop
    supplies the rest. Before the threshold the boot note stands."""

    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", _probe(fail=True))
    service = OT2Service(dry_run=False, host_alias="192.168.0.9", transport="http")
    service.boot_reconnect()
    assert service.get_status().equipment_status == "requires_init"

    for _ in range(_OT2_UNREACHABLE_AFTER - 1):
        service._refresh_identity()

    status = service.get_status()
    assert status.equipment_status == "unknown"
    assert "never seen" in status.message
    assert status.details["robot"]["reachable"] is False
    assert status.details["robot"]["last_seen_at"] is None


def test_dry_run_is_never_unreachable():
    service = OT2Service(dry_run=True)
    status = service.get_status()
    assert status.equipment_status == "dry_run"
    assert status.components["robot"].state == "dry_run"


def test_robot_unreachable_fixture_matches_contract():
    payload = json.loads((_FIXTURES / "status_robot_unreachable.json").read_text())
    status = EquipmentStatus(**payload)

    assert status.equipment_status == "unknown"
    assert status.activity == "unknown"
    assert status.last_error is None
    assert status.components["robot"].state == "unreachable"
    assert status.details["robot"]["reachable"] is False
    assert set(status.allowed_actions) <= _OFFLINE_SAFE_ACTIONS
