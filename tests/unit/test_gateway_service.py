import json
import socket
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

from opentrons_server.gateway import service as service_module
from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.models import (
    ERROR_CODES,
    EquipmentStatus,
    LiquidMoveRequest,
    WellLocation,
)
from opentrons_server.gateway.service import OT2Service, OT2ServiceState, UnknownOutcomeError

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_dry_run_status_matches_equipment_contract():
    service = OT2Service(dry_run=True)

    status = service.get_status()

    assert status.equipment_id == "ot2"
    assert status.equipment_kind == "liquid_handler"
    assert status.equipment_status == "dry_run"
    assert "startup" in status.allowed_actions
    assert status.details["service_state"] == "dry_run"


def test_gateway_exposes_spec_endpoints_in_dry_run():
    client = TestClient(create_app(dry_run=True, enforce_claims=False))

    assert client.get("/").json()["equipment_id"] == "ot2"
    assert client.get("/health").json() == {"status": "healthy"}
    status = client.get("/status").json()
    assert status["equipment_status"] == "dry_run"
    assert status["equipment_kind"] == "liquid_handler"


def test_non_idempotent_transport_loss_sets_unknown_outcome():
    service = OT2Service(dry_run=False)
    control = Mock()
    control.get_location_from_labware.return_value = None
    control.aspirate.side_effect = socket.timeout("lost during aspirate")
    service.control = control
    service.state = OT2ServiceState.READY

    request = LiquidMoveRequest(
        pipette="p300",
        volume_ul=50,
        location=WellLocation(labware_nickname="plate", position="A1"),
    )

    with pytest.raises(UnknownOutcomeError):
        service.aspirate(request)

    assert service.state == OT2ServiceState.UNKNOWN_OUTCOME
    assert service.last_error is not None
    assert service.last_error.code == "command_unknown_outcome"
    # The code is the taxonomy's; which command it was lives in the message.
    assert service.last_error.message.startswith("aspirate: ")
    assert service.get_status().equipment_status == "unknown"


def test_reconcile_clears_unknown_outcome():
    service = OT2Service(dry_run=False)
    service.state = OT2ServiceState.UNKNOWN_OUTCOME

    service.reconcile({"deck": {"slots": {}}})

    assert service.state == OT2ServiceState.READY
    assert service.last_error is None
    assert service.last_snapshot == {"deck": {"slots": {}}}


def test_reconcile_clears_error_when_a_session_is_live():
    service = OT2Service(dry_run=False)
    service.control = Mock()
    service._set_error("command_failed", "pick_up_tip: boom", severity="error")
    assert service.state == OT2ServiceState.ERROR

    service.reconcile()

    assert service.state == OT2ServiceState.READY
    assert service.last_error is None


def test_reconcile_does_not_clear_error_without_a_session():
    # e.g. a failed startup: there is nothing usable to return "ready" to.
    service = OT2Service(dry_run=False)
    service._set_error("startup_failed", "no route to robot", severity="error")

    service.reconcile()

    assert service.state == OT2ServiceState.ERROR
    assert service.last_error is not None
    assert service._required_actions() == ["startup"]


def test_error_state_advertises_recovery_actions_when_a_session_is_live():
    service = OT2Service(dry_run=False)
    service.control = Mock()
    service._set_error("command_failed", "pick_up_tip: boom", severity="error")

    allowed = set(service.allowed_actions())

    # An operator with a mounted tip after a failed step must be able to get
    # out — the old ["startup"]-only list stranded exactly that operator.
    assert {"startup", "shutdown", "home", "move_to", "drop_tip"} <= allowed
    # §2.2: run-starting actions stay withheld while the fault is active.
    assert not {"setup", "pick_up_tip", "aspirate", "dispense"} & allowed
    assert service._required_actions() == ["reconcile"]


def test_error_state_without_a_session_offers_only_startup():
    service = OT2Service(dry_run=False)
    service._set_error("startup_failed", "no route to robot", severity="error")

    assert service._allowed_for_state() == ["startup"]


# ---------------------------------------------------------------------------
# /control/startup credential precedence: env-var default vs request body
# ---------------------------------------------------------------------------


def _service_with_fake_control(monkeypatch, **kwargs):
    """Build a service whose OT2Control() doesn't try to open SSH; capture
    the kwargs passed to OT2Control() so tests can assert what credentials
    actually reached the transport layer."""
    captured: dict = {}

    def fake_ot2control(**ot2_kwargs):
        captured.update(ot2_kwargs)
        return Mock()

    monkeypatch.setattr(
        "opentrons_server.gateway.service.OT2Control", fake_ot2control
    )
    service = OT2Service(dry_run=False, **kwargs)
    return service, captured


def test_empty_password_in_body_preserves_env_default(monkeypatch):
    """Regression: password='' in the request body must NOT clear the
    env-var-supplied default. The whole point of putting OT2_SSH_PASSWORD
    on the gateway host is that workflows can call /control/startup
    without sending the passphrase themselves."""
    service, captured = _service_with_fake_control(
        monkeypatch, host_alias="env-host", password="env-secret"
    )
    service.startup(host_alias="env-host", password="", simulation=True)
    assert service.password == "env-secret"
    assert captured["password"] == "env-secret"


def test_missing_password_in_body_preserves_env_default(monkeypatch):
    """password=None (field absent) also falls through to the env default."""
    service, captured = _service_with_fake_control(
        monkeypatch, host_alias="env-host", password="env-secret"
    )
    service.startup(host_alias="env-host", password=None, simulation=True)
    assert service.password == "env-secret"
    assert captured["password"] == "env-secret"


def test_truthy_password_in_body_overrides_env_default(monkeypatch):
    """Explicit non-empty password still overrides (the debug / local-dev
    path; production workflow code should not exercise this)."""
    service, captured = _service_with_fake_control(
        monkeypatch, host_alias="env-host", password="env-secret"
    )
    service.startup(host_alias="env-host", password="override", simulation=True)
    assert service.password == "override"
    assert captured["password"] == "override"


def test_empty_host_alias_in_body_preserves_env_default(monkeypatch):
    """host_alias was already correctly truthy-checked (line 87 of service.py);
    this test pins the behaviour so a future refactor can't regress it."""
    service, captured = _service_with_fake_control(
        monkeypatch, host_alias="env-host", password="env-secret"
    )
    service.startup(host_alias="", password=None, simulation=True)
    assert service.host_alias == "env-host"
    assert captured["host_alias"] == "env-host"


# ---------------------------------------------------------------------------
# Deck-light toggle: /control/lights endpoint + /status components wiring
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _ready_service_with_control() -> OT2Service:
    service = OT2Service(dry_run=False)
    control = Mock()
    control.client.hostname = "ot2.local"
    service.control = control
    service.state = OT2ServiceState.READY
    return service


def test_dry_run_lights_default_off_and_toggle():
    service = OT2Service(dry_run=True)

    status = service.get_status()
    assert status.components["lights"].connected is True
    assert status.components["lights"].state == "off"
    assert "lights.set" in status.allowed_actions

    assert service.set_lights(True) is True
    assert service.get_lights() is True
    assert service.get_status().components["lights"].state == "on"


def test_lights_endpoint_dry_run_toggles_and_reflects_in_status():
    client = TestClient(create_app(dry_run=True, enforce_claims=False))

    resp = client.post("/control/lights", json={"on": True})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    status = client.get("/status").json()
    assert status["components"]["lights"]["state"] == "on"
    assert "lights.set" in status["allowed_actions"]


def test_background_refresh_reads_robot_lights_http(monkeypatch):
    service = _ready_service_with_control()
    captured: dict = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _FakeResponse({"on": True})

    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", fake_get)

    # The off-request-path refresh does the HTTP read and populates the cache.
    service._refresh_lights()
    status = service.get_status()

    assert captured["url"] == "http://ot2.local:31950/robot/lights"
    assert captured["headers"]["Opentrons-Version"] == "3"
    assert status.components["lights"].connected is True
    assert status.components["lights"].state == "on"
    # Convenience control: available even though it isn't tied to ready state.
    assert "lights.set" in status.allowed_actions


def test_status_issues_no_http(monkeypatch):
    """/status must never issue a blocking HTTP read (the flap root cause).

    get_status serves the deck-light state from cache; any requests.get/post
    from within the handler is a regression that reintroduces the per-poll
    robot dependency that dropped the socket under contention.
    """

    service = _ready_service_with_control()
    service._last_lights = True  # seeded as the background refresh would

    def fail(*args, **kwargs):
        raise AssertionError("get_status issued a blocking HTTP request")

    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", fail)
    monkeypatch.setattr("opentrons_server.gateway.service.requests.post", fail)

    status = service.get_status()

    assert status.equipment_status == "ready"
    assert status.components["lights"].state == "on"


def test_lights_unreachable_reported_as_unknown(monkeypatch):
    service = _ready_service_with_control()

    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", boom)

    # A failed refresh resets the cache to None; /status then reports unknown
    # without itself touching the network.
    service._refresh_lights()
    status = service.get_status()

    assert status.components["lights"].connected is False
    assert status.components["lights"].state == "unknown"
    assert "lights.set" not in status.allowed_actions


def test_set_lights_posts_to_robot_http(monkeypatch):
    service = _ready_service_with_control()
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return _FakeResponse({"on": kwargs.get("json", {}).get("on")})

    monkeypatch.setattr("opentrons_server.gateway.service.requests.post", fake_post)

    assert service.set_lights(True) is True
    assert captured["url"] == "http://ot2.local:31950/robot/lights"
    assert captured["json"] == {"on": True}
    assert captured["headers"]["Opentrons-Version"] == "3"


def test_lights_endpoint_is_claim_gated_and_proxies(monkeypatch):
    app = create_app(dry_run=False, enforce_claims=True, auto_reconnect=False)
    service = app.state.service
    control = Mock()
    control.client.hostname = "ot2.local"
    service.control = control
    service.state = OT2ServiceState.READY

    def fake_post(url, **kwargs):
        return _FakeResponse({"on": kwargs.get("json", {}).get("on")})

    monkeypatch.setattr("opentrons_server.gateway.service.requests.post", fake_post)
    client = TestClient(app)

    # No claim token -> 423 Locked, same as the other /control/* actions.
    assert client.post("/control/lights", json={"on": True}).status_code == 423

    token = client.post(
        "/control/claim", json={"owner": "test", "session_id": "s1"}
    ).json()["claim_token"]
    resp = client.post(
        "/control/lights", json={"on": True}, headers={"X-Claim-Token": token}
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "Deck lights on"


def test_lights_endpoint_409_when_not_initialized():
    # No session and no OT2_HTTP_BASE_URL override -> set_lights raises, 409.
    client = TestClient(create_app(dry_run=False, enforce_claims=False, auto_reconnect=False))
    assert client.post("/control/lights", json={"on": True}).status_code == 409


def test_status_lights_on_fixture_matches_contract():
    payload = json.loads((_FIXTURES / "status_lights_on.json").read_text())
    status = EquipmentStatus(**payload)
    assert status.components["lights"].state == "on"
    assert status.components["lights"].connected is True
    assert "lights.set" in status.allowed_actions


def test_status_ready_claim_held_fixture_matches_contract():
    """STATUS_SPEC §9's v1.1 checklist asks for a `ready`-with-a-claim-held
    snapshot alongside the no-claim one. It is the only fixture that pins the
    serialized shape of `details.claimed_by` — the field a reader uses to show
    who holds the device, and the one thing the no-claim fixtures cannot show."""
    payload = json.loads((_FIXTURES / "status_ready_claim_held.json").read_text())
    status = EquipmentStatus(**payload)

    assert status.equipment_status == "ready"
    assert status.activity == "idle"  # §2.3 invariant: ready => idle
    claimed_by = status.details["claimed_by"]
    assert set(claimed_by) == {"session_id", "owner", "expires_at"}
    assert claimed_by["owner"] == "agent:solubility-screening"
    # A held claim gates control but does not narrow what the device would
    # honor for the holder, so allowed_actions is unchanged from the ready case.
    assert "aspirate" in status.allowed_actions


def test_no_claim_fixtures_omit_claimed_by():
    """The complement of the above: `details.claimed_by` is absent (not a
    stale object) whenever no claim is held."""
    for name in ("status_lights_on.json", "status_requires_init.json"):
        payload = json.loads((_FIXTURES / name).read_text())
        assert "claimed_by" not in EquipmentStatus(**payload).details


# ---------------------------------------------------------------------------
# Guarded auto-reconnect on process start (probe + re-derive state)
# ---------------------------------------------------------------------------


def _fake_probe_get(*, health=None, run_active=False, instruments=None, modules=None, fail=False):
    """Build a fake requests.get that dispatches by robot-server endpoint."""

    default_health = {
        "api_version": "8.7.0",
        "fw_version": "v1.1.0",
        "robot_model": "OT-2 Standard",
        "name": "ot2cytation",
    }
    default_instruments = [
        {
            "mount": "left",
            "instrumentModel": "p300_multi_v2.0",
            "instrumentName": "p300_multi_gen2",
            "data": {"channels": 8},
        }
    ]

    def fake_get(url, **kwargs):
        if fail:
            raise RuntimeError("connection refused")
        if url.endswith("/health"):
            return _FakeResponse(health or default_health)
        if url.endswith("/runs"):
            status = "running" if run_active else "succeeded"
            return _FakeResponse({"data": [{"current": run_active, "status": status}]})
        if url.endswith("/instruments"):
            data = default_instruments if instruments is None else instruments
            return _FakeResponse({"data": data})
        if url.endswith("/modules"):
            return _FakeResponse({"data": modules or []})
        return _FakeResponse({})

    return fake_get


def test_probe_surfaces_attached_modules(monkeypatch):
    mods = [
        {
            "moduleModel": "temperatureModuleV2",
            "moduleType": "temperatureModuleType",
            "serialNumber": "TMV2123",
            "id": "mod-1",
            "data": {"status": "heating", "currentTemperature": 26.0, "targetTemperature": 37.0},
        }
    ]
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(modules=mods)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9")

    probe = service.probe_robot()
    assert probe["modules"] == [
        {
            "model": "temperatureModuleV2",
            "type": "temperatureModuleType",
            "serial": "TMV2123",
            "id": "mod-1",
            "status": "heating",
            "current_temperature": 26.0,
            "target_temperature": 37.0,
        }
    ]
    # flows through to /status as details.robot.modules (live reading + all)
    service._last_probe = probe
    status = service.get_status()
    mod0 = status.details["robot"]["modules"][0]
    assert mod0["model"] == "temperatureModuleV2"
    assert mod0["current_temperature"] == 26.0


def test_probe_module_without_temperature_reads_none(monkeypatch):
    # Idle modules may omit currentTemperature/targetTemperature -> None, not KeyError.
    mods = [{"moduleModel": "temperatureModuleV2", "moduleType": "temperatureModuleType",
             "serialNumber": "S", "id": "m", "data": {"status": "idle"}}]
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(modules=mods)
    )
    probe = OT2Service(dry_run=False, host_alias="192.168.0.9").probe_robot()
    assert probe["modules"][0]["current_temperature"] is None
    assert probe["modules"][0]["target_temperature"] is None


def test_probe_no_modules_is_empty_list(monkeypatch):
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get()
    )
    probe = OT2Service(dry_run=False, host_alias="192.168.0.9").probe_robot()
    assert probe["modules"] == []


def test_boot_reconnect_idle_establishes_session(monkeypatch):
    captured: dict = {}

    def fake_ot2control(**kwargs):
        captured.update(kwargs)
        return Mock()

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", fake_ot2control)
    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", _fake_probe_get())

    service = OT2Service(dry_run=False, host_alias="192.168.0.9", password="pw")
    service.boot_reconnect()

    assert service.state == OT2ServiceState.READY
    status = service.get_status()
    assert status.equipment_status == "ready"
    assert status.components["pipette_left"].state == "p300_multi_gen2"
    assert status.details["robot"]["robot_name"] == "ot2cytation"


def test_never_started_run_is_not_an_active_run():
    """A created-but-never-started run has executed nothing, and on
    OT2_TRANSPORT=http it is very likely the gateway's OWN open run — counting
    it made every restart stand off as if an app were driving the robot."""

    from opentrons_server.gateway.service import _run_counts_as_active

    # The exact shape observed live on ot2_complexation, 2026-08-05.
    assert not _run_counts_as_active(
        {"current": True, "status": "idle", "startedAt": None, "protocolId": None}
    )
    assert not _run_counts_as_active({"current": True, "status": "idle"})  # key absent

    # A real session still counts the moment it has actually started...
    assert _run_counts_as_active(
        {"current": True, "status": "idle", "startedAt": "2026-08-05T23:13:22Z"}
    )
    # ... and any non-idle live status counts regardless.
    for status in ("running", "paused", "blocked-by-open-door", "finishing"):
        assert _run_counts_as_active({"current": True, "status": status})

    # Terminal and non-current runs never count.
    for status in ("succeeded", "failed", "stopped"):
        assert not _run_counts_as_active({"current": True, "status": status})
    assert not _run_counts_as_active({"current": False, "status": "running"})


def test_boot_reconnect_ignores_the_gateways_own_idle_run(monkeypatch):
    """The regression this guards: an http-transport gateway restarting with its
    own leftover run open must come up ready, not stand off against itself."""

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", lambda **kwargs: Mock())

    def probe_with_idle_run(url, **kwargs):
        if url.endswith("/health"):
            return _FakeResponse({"api_version": "8.7.0", "name": "ot2training"})
        if url.endswith("/runs"):
            return _FakeResponse(
                {"data": [{"current": True, "status": "idle", "startedAt": None}]}
            )
        return _FakeResponse({"data": []})

    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", probe_with_idle_run)

    service = OT2Service(dry_run=False, host_alias="192.168.0.9", password="pw")
    service.boot_reconnect()

    assert service.state != OT2ServiceState.EXTERNAL_CONTROL
    assert service.get_status().details["robot"]["run_active"] is False


def test_boot_reconnect_active_run_stands_off(monkeypatch):
    def must_not_connect(**kwargs):
        raise AssertionError("must not open a session while a run is active")

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", must_not_connect)
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=True)
    )

    service = OT2Service(dry_run=False, host_alias="192.168.0.9")
    service.boot_reconnect()

    assert service.state == OT2ServiceState.EXTERNAL_CONTROL
    assert service.control is None
    status = service.get_status()
    assert status.equipment_status == "busy"
    assert status.allowed_actions == []
    assert "external" in status.message.lower()


def test_external_control_self_heals_when_run_finishes(monkeypatch):
    """Once a boot-time external run completes, the background refresh reclaims
    the control plane and the gateway returns to ready without a restart."""
    monkeypatch.setattr(
        "opentrons_server.gateway.service.OT2Control", lambda **kwargs: Mock()
    )

    # Boot while an app-driven run is active: the gateway stands off.
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=True)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9", password="pw")
    service.boot_reconnect()
    assert service.state == OT2ServiceState.EXTERNAL_CONTROL

    # Run finishes: the next background refresh sees a reachable, idle robot.
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=False)
    )
    service._refresh_identity()

    assert service.state == OT2ServiceState.READY
    assert service.control is not None
    status = service.get_status()
    assert status.equipment_status == "ready"
    assert "home" in status.allowed_actions


def test_external_control_holds_while_run_still_active(monkeypatch):
    """The refresh must not seize the robot while the external run is ongoing."""

    def must_not_connect(**kwargs):
        raise AssertionError("must not open a session while a run is active")

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", must_not_connect)
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=True)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9")
    service.boot_reconnect()
    assert service.state == OT2ServiceState.EXTERNAL_CONTROL

    service._refresh_identity()  # run still active

    assert service.state == OT2ServiceState.EXTERNAL_CONTROL
    assert service.control is None


def test_external_control_does_not_reclaim_when_unreachable(monkeypatch):
    """A robot that has gone unreachable must not trigger a reclaim (which would
    fail and flip to error) — the gateway holds its boot-time stand-off."""

    def must_not_connect(**kwargs):
        raise AssertionError("must not open a session against an unreachable robot")

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", must_not_connect)
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=True)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9")
    service.boot_reconnect()
    assert service.state == OT2ServiceState.EXTERNAL_CONTROL

    # Robot becomes unreachable before the run's completion is ever observed.
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(fail=True)
    )
    service._refresh_identity()

    assert service.state == OT2ServiceState.EXTERNAL_CONTROL
    assert service.control is None


def test_boot_reconnect_unreachable_stays_requires_init(monkeypatch):
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(fail=True)
    )

    service = OT2Service(dry_run=False, host_alias="192.168.0.9")
    service.boot_reconnect()

    assert service.state == OT2ServiceState.REQUIRES_INIT
    assert service.control is None
    assert "unreachable" in service.get_status().message.lower()


def test_self_heals_when_robot_returns_after_unreachable_boot(monkeypatch):
    """A gateway that starts while its OT-2 is off must not stay down forever.

    Before this, nothing retried the unreachable-at-boot stand-off, so the
    gateway sat in `requires_init` until an operator noticed the tile.
    """

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", lambda **kwargs: Mock())
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(fail=True)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9", password="pw")
    service.boot_reconnect()
    assert service.state == OT2ServiceState.REQUIRES_INIT

    # Robot powers back on: the next background refresh takes the control plane.
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=False)
    )
    service._refresh_identity()

    assert service.state == OT2ServiceState.READY
    assert service.control is not None


def test_returning_robot_that_is_busy_is_deferred_to(monkeypatch):
    """If the robot comes back mid-run, stand off exactly as boot would have —
    the external-control self-heal picks it up when that run ends."""

    def must_not_connect(**kwargs):
        raise AssertionError("must not seize a robot with an active run")

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", must_not_connect)
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(fail=True)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9")
    service.boot_reconnect()

    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=True)
    )
    service._refresh_identity()

    assert service.state == OT2ServiceState.EXTERNAL_CONTROL
    assert service.control is None
    status = service.get_status()
    assert status.equipment_status == "busy"
    assert status.activity == "running"


def test_self_heal_never_undoes_an_operator_shutdown(monkeypatch):
    """Otherwise /control/shutdown would be a no-op: the refresh loop would
    re-take the REPL seconds later."""

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", lambda **kwargs: Mock())
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=False)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9", password="pw")
    service.boot_reconnect()
    assert service.state == OT2ServiceState.READY

    service.shutdown()
    assert service.state == OT2ServiceState.REQUIRES_INIT

    service._refresh_identity()
    assert service.state == OT2ServiceState.REQUIRES_INIT
    assert service.control is None

    # An explicit startup re-arms it.
    service.startup()
    assert service.state == OT2ServiceState.READY


def test_self_heal_retry_is_rate_limited(monkeypatch):
    """A robot that answers HTTP but cannot finish a protocol init must not be
    hammered once per refresh tick."""

    attempts = []

    def flaky(**kwargs):
        attempts.append(1)
        raise RuntimeError("protocol init failed")

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", flaky)
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(fail=True)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9")
    service.boot_reconnect()

    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=False)
    )
    service._refresh_identity()
    assert len(attempts) == 1
    assert service.state == OT2ServiceState.ERROR

    # Even back in requires_init, the next tick is inside the retry window.
    service.state = OT2ServiceState.REQUIRES_INIT
    service._refresh_identity()
    assert len(attempts) == 1


def test_self_heals_after_a_failed_boot_startup(monkeypatch):
    """One transient failure during the boot reconnect must not strand the
    gateway in `error` until a human restarts it (live 2026-08-12: a single
    10 s read-timeout on POST /runs did exactly that). Mirrors the
    shaker/plateloc boot-retry: a failed *startup* retries while no session
    exists, with the error surfaced on /status until a retry succeeds."""

    calls = []

    def flaky_then_good(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("POST /runs: read timed out")
        return Mock()

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", flaky_then_good)
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=False)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9", password="pw")
    service.boot_reconnect()
    assert service.state == OT2ServiceState.ERROR
    assert service.last_error is not None
    assert service.last_error.code == "startup_failed"

    service._last_self_heal_at = 0.0  # outside the retry window
    service._refresh_identity()

    assert service.state == OT2ServiceState.READY
    assert service.control is not None
    # The startup failure is cleared (the Mock control leaves a cosmetic
    # snapshot_failed warning behind, which is not what this test pins).
    assert service.last_error is None or service.last_error.code != "startup_failed"


def test_a_host_key_refusal_is_never_self_healed(monkeypatch):
    """A startup refused for an unknown or changed SSH host key meets the same
    key on every retry, so the self-heal stands off; an explicit startup
    (after an operator fixes known_hosts) still runs and re-arms it."""

    from opentrons_server.transport import HostKeyVerificationError

    calls = []

    def refused_then_good(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise HostKeyVerificationError("SSH host key check failed: changed")
        return Mock()

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", refused_then_good)
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=False)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9", password="pw")
    service.boot_reconnect()
    assert service.state == OT2ServiceState.ERROR
    assert service.last_error.code == "startup_failed"
    assert "host key" in service.last_error.message

    service._last_self_heal_at = 0.0  # outside the retry window
    service._refresh_identity()
    assert len(calls) == 1
    assert service.state == OT2ServiceState.ERROR

    service.startup()
    assert len(calls) == 2
    assert service.state == OT2ServiceState.READY
    assert service._host_key_refused is False


def test_a_mid_session_error_is_never_self_healed(monkeypatch):
    """A session that exists and then faults is a human's call (recover via
    the panel or reconcile) — only the no-session startup failure loops."""

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", lambda **kwargs: Mock())
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=False)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9", password="pw")
    service.boot_reconnect()
    assert service.state == OT2ServiceState.READY
    control = service.control

    service._set_error("command_failed", "aspirate: boom", severity="error")
    service._last_self_heal_at = 0.0
    service._refresh_identity()

    assert service.state == OT2ServiceState.ERROR  # surfaced, not looped
    assert service.control is control  # no new session was taken


def test_stale_unreachable_message_clears_when_robot_returns(monkeypatch):
    """/status must stop claiming 'Robot unreachable' once it isn't — the note
    was set at boot and previously nothing ever cleared it."""

    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(fail=True)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9")
    service.boot_reconnect()
    assert "unreachable" in service.get_status().message.lower()

    # Reachable again, but the operator has asked for it to stay down, so no
    # session is taken — the message must still tell the truth.
    service._operator_shutdown = True
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=False)
    )
    service._refresh_identity()

    status = service.get_status()
    assert status.equipment_status == "requires_init"
    assert "unreachable" not in (status.message or "").lower()
    assert status.details["robot"]["reachable"] is True


def test_boot_reconnect_is_noop_in_dry_run(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not probe the robot")

    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", boom)
    service = OT2Service(dry_run=True)
    service.boot_reconnect()
    assert service.state == OT2ServiceState.DRY_RUN


def test_connecting_reports_requires_init_not_busy():
    """STATUS_SPEC §2.2: `connecting` is "service up, hardware not initialized",
    not `busy`. Reporting busy made a slow-but-healthy boot (the REPL protocol
    init routinely takes minutes) look like the robot was doing work, and §2.3's
    invariant table pairs busy with an operation actually running."""

    service = OT2Service(dry_run=False)
    service.state = OT2ServiceState.CONNECTING

    status = service.get_status()

    assert status.equipment_status == "requires_init"
    assert status.details["service_state"] == "connecting"
    # startup is already in flight — don't tell an operator to POST it again.
    assert status.required_actions == []
    assert "startup" not in status.allowed_actions
    assert "connecting" in status.message.lower()


def test_ssh_connect_failure_raises_the_real_reason():
    """SSHClient.connect() returns False after exhausting retries instead of
    raising; discarding it let construction fall through to _get_protocol(),
    which failed with the misleading "SSH client is not connected"."""

    from unittest.mock import patch

    from opentrons_server.control.ot2_control import OT2Control

    with patch("opentrons_server.control.ot2_control.SSHClient") as ssh_cls:
        client = ssh_cls.return_value
        client.connect.return_value = False
        client.hostname = "ot2cytation"
        client.max_retries = 3
        client.connection_timeout = 10
        client.host_key_failure = False
        client.last_error = "TimeoutError: timed out"

        with pytest.raises(ConnectionError) as excinfo:
            OT2Control(host_alias="ot2cytation")

    message = str(excinfo.value)
    assert "ot2cytation" in message
    assert "3 attempts" in message
    assert "TimeoutError: timed out" in message
    # The old, misleading message must not be what surfaces.
    assert "SSH client is not connected" not in message


def test_same_owner_can_take_over_its_own_stale_claim():
    """A reloaded or second UI tab arrives with a new session_id and no token.

    Without takeover it is refused for the full TTL (nobody can heartbeat or
    release the stranded claim); with it, the same owner supersedes itself and
    the old token dies.
    """
    client = TestClient(create_app(dry_run=True, enforce_claims=True))

    first = client.post(
        "/control/claim", json={"owner": "ot2-gateway-ui", "session_id": "tab-1", "ttl_s": 60}
    ).json()["claim_token"]

    # Plain re-claim from the new tab: still a conflict, naming the holder.
    refused = client.post(
        "/control/claim", json={"owner": "ot2-gateway-ui", "session_id": "tab-2"}
    )
    assert refused.status_code == 409
    assert refused.json()["claimed_by"]["session_id"] == "tab-1"

    taken = client.post(
        "/control/claim",
        json={"owner": "ot2-gateway-ui", "session_id": "tab-2", "takeover": True},
    )
    assert taken.status_code == 200
    second = taken.json()["claim_token"]
    assert second != first

    # The superseded tab learns it lost the claim on its next heartbeat (§5).
    assert client.post("/control/heartbeat", headers={"X-Claim-Token": first}).status_code == 401
    assert client.post("/control/heartbeat", headers={"X-Claim-Token": second}).status_code == 200
    assert client.get("/status").json()["details"]["claimed_by"]["session_id"] == "tab-2"


def test_takeover_never_steals_another_owners_claim():
    """The whole point of scoping it to the owner: an agent mid-plan, or the
    dashboard's per-request claim, is not something a UI click may end."""
    client = TestClient(create_app(dry_run=True, enforce_claims=True))

    client.post(
        "/control/claim",
        json={"owner": "agent:solubility-screening", "session_id": "run-1", "ttl_s": 60},
    )

    resp = client.post(
        "/control/claim",
        json={"owner": "ot2-gateway-ui", "session_id": "tab-1", "takeover": True},
    )
    assert resp.status_code == 409
    assert resp.json()["claimed_by"]["owner"] == "agent:solubility-screening"


def test_reclaiming_the_same_session_keeps_its_token():
    """§5 idempotence must survive the takeover change: the same session
    re-claiming gets its existing token back, not a rotated one that would
    strand the token its own page is still heartbeating with."""
    client = TestClient(create_app(dry_run=True, enforce_claims=True))

    body = {"owner": "ot2-gateway-ui", "session_id": "tab-1", "ttl_s": 60}
    first = client.post("/control/claim", json=body).json()["claim_token"]
    again = client.post("/control/claim", json=body).json()["claim_token"]

    assert again == first
    assert client.post("/control/heartbeat", headers={"X-Claim-Token": first}).status_code == 200


def test_equipment_version_is_the_gateway_not_the_robot(monkeypatch):
    """`equipment_version` names the software answering /status.

    It used to be overwritten with the robot's `api_version` on every probe,
    so the dashboard's version column showed the robot's release and the
    gateway's own was invisible on the wire. The robot's versions are a
    property of the attached hardware and stay in `details.robot`.
    """
    from opentrons_server.version import __version__ as gateway_version

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", lambda **_: Mock())
    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", _fake_probe_get())

    service = OT2Service(dry_run=False, host_alias="192.168.0.9", password="pw")
    service.boot_reconnect()  # probes the robot; must not touch equipment_version
    status = service.get_status()

    assert status.equipment_version == gateway_version
    assert status.equipment_version != "8.7.0"
    # The robot's own versions are still reported, just not as the gateway's.
    assert status.details["robot"]["api_version"] == "8.7.0"


def test_equipment_version_is_set_before_any_probe():
    """A dry-run service never probes a robot, and previously reported
    `equipment_version: null` for its whole life."""
    from opentrons_server.version import __version__ as gateway_version

    assert OT2Service(dry_run=True).get_status().equipment_version == gateway_version


def test_package_version_matches_distribution_metadata():
    """One number: `opentrons_server.__version__` is the installed dist's, not
    a second hand-maintained copy that drifts from pyproject.toml."""
    from importlib.metadata import version as dist_version

    import opentrons_server

    assert opentrons_server.__version__ == dist_version("opentrons_server")


# ---------------------------------------------------------------------------
# last_error.code taxonomy (STATUS_SPEC best practice #6)
# ---------------------------------------------------------------------------


def test_set_error_rejects_a_code_outside_the_taxonomy():
    """The setter is the taxonomy's only enforcement point, so it must refuse
    rather than coerce — a substituted code ships a bug to every dashboard."""
    service = OT2Service(dry_run=True)

    with pytest.raises(ValueError, match="outside the taxonomy"):
        service._set_error("aspirate_failed", "boom", severity="error")

    assert service.last_error is None


def test_every_last_error_code_in_the_source_is_in_the_taxonomy():
    """Guards the drift this taxonomy exists to prevent: the gateway used to
    build codes as f-strings (`f"{name}_failed"`), so the code space grew a new
    member per protocol command and no client could enumerate it. Every call
    site must pass a bare literal drawn from ERROR_CODES."""
    import ast

    source = Path(service_module.__file__).read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_set_error"
    ]

    assert calls, "expected _set_error call sites in service.py"
    for call in calls:
        first = call.args[0]
        assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
            "last_error.code must be a literal, not a computed string — "
            f"got {ast.dump(first)}"
        )
        assert first.value in ERROR_CODES, f"{first.value!r} is not in ERROR_CODES"


def _liquid_service():
    """A READY service with a mocked transport, for liquid-handling calls."""
    service = OT2Service(dry_run=False)
    control = Mock()
    control.get_location_from_labware.return_value = None
    service.control = control
    service.state = OT2ServiceState.READY
    return service, control


def _unqualified(pipette: str = "p300") -> LiquidMoveRequest:
    """A move naming a well and no offset — the case the defaults decide."""
    return LiquidMoveRequest(
        pipette=pipette,
        volume_ul=50,
        location=WellLocation(labware_nickname="plate", position="A1"),
    )


def test_unqualified_aspirate_references_the_well_bottom():
    # An aspirate at the well top draws air. The default has to be in the
    # liquid, and off the glass (0 mm occludes the tip).
    service, control = _liquid_service()

    service.aspirate(_unqualified())

    kwargs = control.get_location_from_labware.call_args.kwargs
    assert kwargs["default_origin"] == "bottom"
    assert kwargs["default_offset"] == service_module._ASPIRATE_DEFAULT_BOTTOM_MM
    assert kwargs["default_offset"] > 0


def test_unqualified_dispense_references_the_well_top():
    service, control = _liquid_service()

    service.dispense(_unqualified())

    kwargs = control.get_location_from_labware.call_args.kwargs
    assert kwargs["default_origin"] == "top"
    assert kwargs["default_offset"] == service_module._DISPENSE_DEFAULT_TOP_MM


def test_explicit_offset_still_beats_the_action_default():
    service, control = _liquid_service()

    service.aspirate(
        LiquidMoveRequest(
            pipette="p300",
            volume_ul=50,
            location=WellLocation(labware_nickname="plate", position="A1", top=-3),
        )
    )

    kwargs = control.get_location_from_labware.call_args.kwargs
    assert kwargs["top"] == -3
    assert kwargs["bottom"] == 0


# --- a failed probe sub-request is unknown, never an empty answer -------------


class _HTTPErrorResponse:
    def raise_for_status(self) -> None:
        raise requests.HTTPError("500 Server Error")

    def json(self) -> dict:
        return {"errors": [{"detail": "internal"}]}


def _probe_failing(*names, how="raise", run_active=False):
    """``_fake_probe_get`` with the named robot-server reads failing."""

    healthy = _fake_probe_get(run_active=run_active)

    def fake_get(url, **kwargs):
        if any(url.endswith("/" + name) for name in names):
            if how == "raise":
                raise RuntimeError("read timeout")
            if how == "http_error":
                return _HTTPErrorResponse()
            return _FakeResponse({"errors": [{"detail": "no data key"}]})
        return healthy(url, **kwargs)

    return fake_get


@pytest.mark.parametrize("how", ["raise", "http_error", "no_data_list"])
def test_probe_unreadable_run_list_is_unknown_not_idle(monkeypatch, how):
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _probe_failing("runs", how=how)
    )
    probe = OT2Service(dry_run=False, host_alias="192.168.0.9").probe_robot()
    assert probe["reachable"] is True
    assert probe["run_active"] is None
    assert "runs" in probe["probe_errors"]
    assert "runs_read_at" not in probe
    # The other reads are independent of the failed one.
    assert probe["instruments"][0]["name"] == "p300_multi_gen2"
    assert probe["instruments_read_at"] is not None
    assert probe["modules"] == []


def test_boot_never_starts_a_session_without_reading_the_run_list(monkeypatch):
    """A reachable robot whose /runs read fails may be running someone else's
    protocol. Defaulting that to "no run" let boot_reconnect seize it."""

    def must_not_connect(**kwargs):
        raise AssertionError("must not open a session without reading the run list")

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", must_not_connect)
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _probe_failing("runs")
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9", password="pw")
    service.boot_reconnect()

    # Neither seized nor stood off as if a run had been seen.
    assert service.state == OT2ServiceState.REQUIRES_INIT
    assert service.control is None
    status = service.get_status()
    assert "run list" in status.message
    assert status.details["robot"]["run_active"] is None
    assert "runs" in status.details["robot"]["probe_errors"]

    # Still unreadable on the next refresh: still deferred, note kept.
    service._refresh_identity()
    assert service.state == OT2ServiceState.REQUIRES_INIT
    assert "run list" in service.get_status().message

    # Readable and idle: the unreachable-boot self-heal takes it from here.
    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", lambda **kwargs: Mock())
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=False)
    )
    service._refresh_identity()
    assert service.state == OT2ServiceState.READY


def test_external_control_holds_while_run_list_is_unreadable(monkeypatch):
    def must_not_connect(**kwargs):
        raise AssertionError("an unreadable run list is not evidence the run ended")

    monkeypatch.setattr("opentrons_server.gateway.service.OT2Control", must_not_connect)
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get(run_active=True)
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9")
    service.boot_reconnect()
    assert service.state == OT2ServiceState.EXTERNAL_CONTROL

    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _probe_failing("runs", run_active=True)
    )
    service._refresh_identity()

    assert service.state == OT2ServiceState.EXTERNAL_CONTROL
    assert service.control is None


def test_failed_instruments_read_keeps_last_list_and_its_read_time(monkeypatch):
    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get", _fake_probe_get()
    )
    service = OT2Service(dry_run=False, host_alias="192.168.0.9")
    service._record_probe(service.probe_robot())
    first_read_at = service._last_probe["instruments_read_at"]

    monkeypatch.setattr(
        "opentrons_server.gateway.service.requests.get",
        _probe_failing("instruments", "modules"),
    )
    service._record_probe(service.probe_robot())

    robot = service.get_status().details["robot"]
    # Pipettes are kept, stamped with when they were actually read...
    assert robot["instruments"][0]["name"] == "p300_multi_gen2"
    assert robot["instruments_read_at"] == first_read_at
    assert service.get_status().components["pipette_left"].state == "p300_multi_gen2"
    # ... while live module telemetry is unknown, not an empty deck.
    assert robot["modules"] is None
    assert set(robot["probe_errors"]) == {"instruments", "modules"}
