"""GET /observations (docs/OBSERVATIONS.md): side-effect-free, honest observations.

Scenarios drive the real hooks: a ``RunEngineClient`` whose ``_request`` is a
fake robot, an ``OT2HttpControl``, and an ``OT2Service`` with a deterministic
observation clock. Each scenario is written to
``tests/fixtures/observations/<config>_<case>.json`` as the sequence of
observations it produces, for the two instrument configurations the twin cares
about (HTE, the twin's target; Complexation, where motion acceptance runs).
Run with ``OBSERVATION_FIXTURES_REGEN=1`` to rewrite them.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from opentrons_server.control.http_control import OT2HttpControl
from opentrons_server.control.http_run import (
    CommandFailed,
    RunEngineClient,
    RunEngineCommands as C,
)
from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.deck import DeckDeclarationStore
from opentrons_server.gateway.labware import standard_definition
from opentrons_server.gateway.models import PipetteJogRequest, PipettePositionRequest
from opentrons_server.gateway.observations import ObservationCache, ObservationResponse
from opentrons_server.gateway.plate_state import PlateStateStore
from opentrons_server.gateway.service import (
    _OT2_UNREACHABLE_AFTER,
    OT2Service,
    OT2ServiceState,
)
from opentrons_server.gateway.tip_state import TipStateStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "observations"
REGEN = os.getenv("OBSERVATION_FIXTURES_REGEN") == "1"

# Instrument configurations as each robot reports them on /status (2026-10-11).
# Serials are placeholders, not the robots' real serials.
CONFIGS: Dict[str, Dict[str, Any]] = {
    "hte": {
        "equipment_id": "ot2_hte",
        "left": {"name": "p300_multi_gen2", "model": "p300_multi_v2.0", "channels": 8, "serial": "EXAMPLE-LEFT-P300M"},
        "right": {"name": "p1000_single_gen2", "model": "p1000_single_v2.0", "channels": 1, "serial": "EXAMPLE-RIGHT-P1000S"},
        "tiprack": "opentrons_96_tiprack_300ul",
    },
    "complexation": {
        "equipment_id": "ot2_complexation",
        "left": {"name": "p300_single_gen2", "model": "p300_single_v2.1", "channels": 1, "serial": "EXAMPLE-LEFT-P300S"},
        "right": {"name": "p20_multi_gen2", "model": "p20_multi_v2.0", "channels": 8, "serial": "EXAMPLE-RIGHT-P20M"},
        "tiprack": "opentrons_96_tiprack_20ul",
    },
}
T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
ROBOT_CLOCK_AHEAD_S = 0.25  # the fake robot's clock runs this far ahead


class FakeTime:
    def __init__(self) -> None:
        self.now = T0

    def tick(self, seconds: float = 0.01) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now

    def __call__(self) -> datetime:
        return self.tick()


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _counter_ids() -> Callable[[str], str]:
    counter = iter(range(1, 1_000_000))
    return lambda prefix: f"{prefix}-{next(counter):04d}"


class FakeRobot:
    """``RunEngineClient._request`` stand-in. Each command takes 0.5 s of the
    shared clock; queued specs override the default answer for one command."""

    def __init__(self, time: FakeTime) -> None:
        self.time = time
        self.queue: List[Dict[str, Any]] = []
        self.during: Optional[Callable[[], None]] = None
        self.client: Optional[RunEngineClient] = None
        self.n = 0
        self.posted: List[str] = []

    def answer(self, **spec: Any) -> None:
        self.queue.append(spec)

    def __call__(self, method: str, path: str, *, json_body=None, params=None, timeout=None):
        if path == "/runs":
            return {"id": "run-fixture"}
        if method == "GET":
            raise AssertionError(f"unexpected robot read {path}")
        data = json_body["data"]
        command_type, cmd_params = data["commandType"], data["params"]
        self.posted.append(command_type)
        spec = self.queue.pop(0) if self.queue else {}
        started = self.time.now + timedelta(seconds=ROBOT_CLOCK_AHEAD_S + 0.001)
        if self.during is not None:
            during, self.during = self.during, None
            during()
        if spec.get("stop"):
            self.client._stop_requested.set()
        self.time.tick(0.5)
        if "raise" in spec:
            raise spec["raise"]
        completed = self.time.now + timedelta(seconds=ROBOT_CLOCK_AHEAD_S - 0.001)
        self.n += 1
        result = spec.get("result", _default_result(command_type, cmd_params))
        status = spec.get("status", "succeeded")
        return {
            "id": f"cmd-{self.n}",
            "commandType": command_type,
            "status": status,
            "startedAt": _iso(started),
            "completedAt": _iso(completed) if status in ("succeeded", "failed") else None,
            "result": result if status == "succeeded" else None,
            "error": spec.get("error"),
        }


def _default_result(command_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if command_type == "loadPipette":
        return {"pipetteId": params["pipetteId"]}
    if command_type == "loadLabware":
        return {"labwareId": params["labwareId"], "definition": standard_definition(params["loadName"])}
    if command_type == "moveToCoordinates":
        return {"position": dict(params["coordinates"])}
    return {}


class Harness:
    def __init__(self, tmp_path: Path, config_name: str, *, boot_id: str = "boot-fixture-1") -> None:
        self.config = CONFIGS[config_name]
        self.time = FakeTime()
        self.service = OT2Service(
            dry_run=False,
            transport="http",
            equipment_id=self.config["equipment_id"],
            host_alias="192.0.2.10",
            tips=TipStateStore(state_path=tmp_path / f"{boot_id}-tips.json"),
            plates=PlateStateStore(state_path=str(tmp_path / f"{boot_id}-plates.json")),
            decks=DeckDeclarationStore(state_path=str(tmp_path / f"{boot_id}-deck.json")),
        )
        self.service.refresh_snapshot = Mock()
        self.service.observations = ObservationCache(
            boot_id=boot_id,
            clock=self.time,
            blocked=self.service._observation_blocked,
            plan_context=self.service.claims.automation,
            new_id=_counter_ids(),
        )
        self.robot = FakeRobot(self.time)
        self.client = RunEngineClient("http://192.0.2.10:31950")
        self.client._request = self.robot
        self.robot.client = self.client
        self.client.observer = self.service.observations
        self.control = OT2HttpControl(self.client)
        self.service.observations.new_session("session_lost")
        self.client.create_run()
        self.service.observations.set_robot_run_id(self.client.run_id)
        self.service.control = self.control
        self.service.state = OT2ServiceState.READY
        self.service._robot_last_seen_at = T0
        self.service._last_probe = self.probe()
        self.steps: List[Dict[str, Any]] = []

    def probe(self, **overrides: Any) -> Dict[str, Any]:
        instruments = [
            {"mount": mount, **{k: self.config[mount][k] for k in ("model", "name", "serial", "channels")}}
            for mount in ("left", "right")
        ]
        return {
            "reachable": True,
            "run_active": False,
            "robot_model": "OT-2 Standard",
            "instruments": instruments,
            "instruments_read_at": T0,
            "runs_read_at": T0,
            "modules": [],
            "modules_read_at": T0,
            "probe_errors": {},
            **overrides,
        }

    def record_probe(self, **overrides: Any) -> None:
        """The real probe bookkeeping, with its wall-clock stamp pinned to the fake clock."""
        self.service._record_probe(self.probe(**overrides))
        self.service._robot_last_seen_at = self.time.now

    # -- commands --------------------------------------------------------------

    def run(self, command, **spec: Any) -> Any:
        if spec:
            self.robot.answer(**spec)
        return self.client.execute(command)

    def load(self) -> None:
        for mount in ("left", "right"):
            self.run(C.load_pipette(self.config[mount]["name"], mount, pipette_id=mount))
        self.run(C.load_labware(self.config["tiprack"], "opentrons", 1, {"slotName": "1"}, labware_id="tips"))
        self.run(C.load_labware("nest_12_reservoir_15ml", "opentrons", 1, {"slotName": "2"}, labware_id="reservoir"))
        self.run(C.load_labware("corning_96_wellplate_360ul_flat", "opentrons", 2, {"slotName": "3"}, labware_id="plate"))

    def multi(self) -> str:
        return "left" if self.config["left"]["channels"] == 8 else "right"

    def single(self) -> str:
        return "right" if self.multi() == "left" else "left"

    def observe(self, label: str) -> Dict[str, Any]:
        snapshot = self.service.observation_snapshot().model_dump(mode="json")
        self.steps.append({"after": label, "observation": snapshot})
        return snapshot


def _write_or_compare(config: str, case: str, description: str, steps: List[Dict[str, Any]]) -> None:
    path = FIXTURES / f"{config}_{case}.json"
    body = {"config": config, "case": case, "description": description, "steps": steps}
    for step in steps:
        ObservationResponse.model_validate(step["observation"])
    text = json.dumps(body, indent=2, sort_keys=True) + "\n"
    if REGEN:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"missing fixture {path.name}; regenerate with OBSERVATION_FIXTURES_REGEN=1"
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(text), (
        f"{path.name} is out of date; regenerate with OBSERVATION_FIXTURES_REGEN=1"
    )


def _well_pos(x: float, y: float, z: float) -> Dict[str, Any]:
    return {"position": {"x": x, "y": y, "z": z}}


# ---------------------------------------------------------------------------
# Scenarios (each produces one fixture per configuration)
# ---------------------------------------------------------------------------


def scenario_idle_after_session_start(h: Harness) -> str:
    h.observe("session started, nothing acquired")
    h.load()
    h.observe("pipettes and labware loaded; still nothing acquired")
    return "A fresh session: instruments known, every position unavailable until a move or read."


def scenario_shared_gantry_update(h: Harness) -> str:
    h.load()
    single, multi = h.single(), h.multi()
    h.run(C.move_to_coordinates(single, {"x": 200.0, "y": 150.0, "z": 100.0}))
    h.observe(f"{single} moved to coordinates: current, primary nozzle")
    h.run(C.move_to_coordinates(multi, {"x": 100.0, "y": 150.0, "z": 120.0}))
    h.observe(f"{multi} moved: it is current, {single} is superseded (other_mount_moved)")
    h.run(C.save_position(single), result={"positionId": "p", "position": {"x": 134.0, "y": 150.0, "z": 218.0}})
    h.observe(f"{single} read with savePosition: both current at the same epoch (coherent)")
    return "One gantry, two mounts: a move by either supersedes the other; equal epochs mean the same gantry state."


def scenario_reference_point_change(h: Harness) -> str:
    h.load()
    multi = h.multi()
    h.run(C.move_to_well(multi, "reservoir", "A1"), result=_well_pos(14.4, 42.6, 40.0))
    h.observe(f"{multi} moved into a 12-well reservoir: column Y centre, with conversion to A1")
    h.run(C.save_position(multi), result={"positionId": "p", "position": {"x": 14.4, "y": 42.6, "z": 40.0}})
    h.observe("savePosition there reports the same centred reference")
    h.run(C.move_to_coordinates(multi, {"x": 14.4, "y": 74.1, "z": 80.0}))
    h.observe("a coordinate move resets the reference to the primary nozzle (A1)")
    h.run(C.move_to_well(multi, "plate", "A1"), result=_well_pos(14.4, 74.1, 15.0))
    h.observe("a 96-well plate has no centring quirk: primary nozzle")
    return "Reservoir centring (centerMultichannelOnWells) changes the reference point; coordinate moves reset it."


def scenario_tip_pickup_and_drop(h: Harness) -> str:
    h.load()
    single = h.single()
    h.run(
        C.pick_up_tip(single, "tips", "A1"),
        result={"tipLength": 51.83, "tipVolume": 300, "tipDiameter": 5.2, "position": {"x": 14.4, "y": 74.1, "z": 99.0}},
    )
    h.observe(f"{single} picked up a tip: pose unknown (result is the approach point), tip length from the engine")
    h.run(C.move_to_coordinates(single, {"x": 150.0, "y": 100.0, "z": 120.0}))
    h.observe("after a move, the position is current and measured at the tip end")
    h.run(C.drop_tip_in_place(single))
    h.observe("tip dropped: pose unknown again, tip length cleared")
    return "Tip pickup and drop change the vertical reference and never publish an endpoint."


def scenario_move_in_flight(h: Harness) -> str:
    h.load()
    single, multi = h.single(), h.multi()
    h.run(C.move_to_coordinates(single, {"x": 200.0, "y": 150.0, "z": 100.0}))
    h.robot.during = lambda: h.observe("a move is in flight: every position superseded, target only in motion.in_flight")
    h.run(C.move_to_well(multi, "plate", "B2"), result=_well_pos(23.4, 65.1, 12.0))
    h.observe("the move completed")
    return "While a command is in flight, no position is current and its target never appears as one."


def scenario_failed_move(h: Harness) -> str:
    h.load()
    single = h.single()
    h.run(C.move_to_coordinates(single, {"x": 200.0, "y": 150.0, "z": 100.0}))
    with pytest.raises(CommandFailed):
        h.run(
            C.move_to_well(single, "plate", "H12"),
            status="failed",
            error={"errorType": "PipetteNotHomedError", "detail": "must home"},
        )
    h.observe("a move failed: the controller cleared its position, so both mounts are invalid")
    return "A failed move invalidates positions; the failure and its robot timestamps are kept."


def scenario_cancelled_by_stop(h: Harness) -> str:
    h.load()
    multi = h.multi()
    h.run(C.move_to_coordinates(multi, {"x": 100.0, "y": 150.0, "z": 120.0}))
    with pytest.raises(OSError):
        h.run(
            C.move_to_well(multi, "plate", "A1"),
            stop=True,
            status="failed",
            error={"errorType": "RunStoppedError", "detail": "run was stopped"},
        )
    h.observe("a software stop cut the move short: motion unknown, positions invalid")
    return "A move interrupted by a stop leaves motion and pose unknown."


def scenario_mirror_unconfirmed(h: Harness) -> str:
    h.load()
    multi = h.multi()
    with pytest.raises(OSError):
        h.run(C.move_to_well(multi, "reservoir", "A2"), **{"raise": OSError("connection reset")})
    h.observe("transport lost during a well move: motion unknown")
    h.run(C.save_position(multi), result={"positionId": "p", "position": {"x": 23.4, "y": 42.6, "z": 40.0}})
    h.observe("a later read succeeds, but whether it is centred is unknown, so no conversion is offered")
    return "When the engine's current location cannot be confirmed, the horizontal reference is unknown."


def scenario_unhomed_read(h: Harness) -> str:
    h.load()
    with pytest.raises(CommandFailed):
        h.run(C.save_position(h.single()), status="failed", error={"errorType": "MustHomeError", "detail": "home"})
    h.observe("savePosition refused (not homed): nothing acquired, the refusal is the last command")
    return "An unhomed robot refuses position reads; nothing is invented."


def scenario_robot_unreachable(h: Harness) -> str:
    h.load()
    h.run(C.move_to_coordinates(h.single(), {"x": 200.0, "y": 150.0, "z": 100.0}))
    h.observe("before the outage: current")
    for _ in range(_OT2_UNREACHABLE_AFTER):
        h.service._record_probe({"reachable": False})
    h.observe("robot unreachable: positions invalid, instruments last-known, motion unknown")
    return "Losing the robot invalidates positions; instruments stay as last known."


def scenario_external_run(h: Harness) -> str:
    h.load()
    h.run(C.move_to_coordinates(h.single(), {"x": 200.0, "y": 150.0, "z": 100.0}))
    h.record_probe(run_active=True, active_runs=[{"id": "run-app-1", "status": "running"}])
    h.observe("the probe finds an outside run: positions superseded, tips unknown, motion external_run")
    h.run(C.save_position(h.single()), result={"positionId": "p", "position": {"x": 1.0, "y": 2.0, "z": 3.0}})
    h.observe("a read while that run is active is never committed")
    return "An external run supersedes positions and blocks commits; Phase 1 does not observe its motion."


def scenario_lost_reply_after_reservoir(h: Harness) -> str:
    h.load()
    multi = h.multi()
    h.run(C.move_to_well(multi, "reservoir", "A1"), result=_well_pos(14.4, 42.6, 40.0))
    h.observe(f"{multi} in the reservoir: centred reference")
    with pytest.raises(OSError):
        h.run(C.move_to_coordinates(multi, {"x": 14.4, "y": 74.1, "z": 80.0}), **{"raise": OSError("reply lost")})
    h.observe("a coordinate move's reply was lost: it may have run, so the engine's location is unknown")
    h.run(C.save_position(multi), result={"positionId": "p", "position": {"x": 14.4, "y": 74.1, "z": 80.0}})
    h.observe("the next read is published with an unknown horizontal reference and no conversion")
    return "After an ambiguous move the reference point is unknown; a stale reservoir centring is never applied."


def scenario_read_failed_after_position(h: Harness) -> str:
    h.load()
    single = h.single()
    h.run(C.move_to_coordinates(single, {"x": 200.0, "y": 150.0, "z": 100.0}))
    h.observe("current")
    with pytest.raises(CommandFailed):
        h.run(C.save_position(single), status="failed", error={"errorType": "MustHomeError", "detail": "home"})
    h.observe("a read failed (not homed): the earlier position can no longer be confirmed")
    return "A failed read invalidates the position it could not confirm."


def scenario_paused(h: Harness) -> str:
    h.load()
    h.run(C.move_to_coordinates(h.single(), {"x": 200.0, "y": 150.0, "z": 100.0}))
    h.service.state = OT2ServiceState.PAUSED
    h.observe("plan paused: last position still current, motion paused")
    return "Paused between commands."


def scenario_probe_partial_failure(h: Harness) -> str:
    h.load()
    h.service._last_probe = h.probe(instruments=None, run_active=None, probe_errors={"instruments": "RuntimeError: read timeout", "runs": "RuntimeError: read timeout"})
    h.observe("instruments and runs unreadable: mounts present with unknown instruments, external run unknown")
    return "A failed probe read is unknown, never an absent instrument or an idle robot."


SCENARIOS = {
    "idle_after_session_start": scenario_idle_after_session_start,
    "shared_gantry_update": scenario_shared_gantry_update,
    "reference_point_change": scenario_reference_point_change,
    "tip_pickup_and_drop": scenario_tip_pickup_and_drop,
    "move_in_flight": scenario_move_in_flight,
    "failed_move": scenario_failed_move,
    "cancelled_by_stop": scenario_cancelled_by_stop,
    "mirror_unconfirmed": scenario_mirror_unconfirmed,
    "unhomed_read": scenario_unhomed_read,
    "robot_unreachable": scenario_robot_unreachable,
    "external_run": scenario_external_run,
    "lost_reply_after_reservoir": scenario_lost_reply_after_reservoir,
    "read_failed_after_position": scenario_read_failed_after_position,
    "paused": scenario_paused,
    "probe_partial_failure": scenario_probe_partial_failure,
}


@pytest.mark.parametrize("config", sorted(CONFIGS))
@pytest.mark.parametrize("case", sorted(SCENARIOS))
def test_fixture(tmp_path, config, case):
    h = Harness(tmp_path, config)
    description = SCENARIOS[case](h)
    _write_or_compare(config, case, description, h.steps)


@pytest.mark.parametrize("config", sorted(CONFIGS))
def test_fixture_gateway_restart(tmp_path, config):
    before = Harness(tmp_path, config, boot_id="boot-fixture-1")
    before.load()
    before.run(C.move_to_coordinates(before.single(), {"x": 200.0, "y": 150.0, "z": 100.0}))
    steps = [{"after": "before restart: current", "observation": before.service.observation_snapshot().model_dump(mode="json")}]
    after = Harness(tmp_path, config, boot_id="boot-fixture-2")
    steps.append({"after": "after a gateway restart: new boot_id, nothing acquired", "observation": after.service.observation_snapshot().model_dump(mode="json")})
    _write_or_compare(config, "gateway_restart", "A gateway restart changes boot_id; nothing carries over.", steps)


def test_fixture_dry_run(tmp_path):
    service = OT2Service(
        dry_run=True,
        equipment_id="ot2_hte",
        tips=TipStateStore(state_path=tmp_path / "tips.json"),
        plates=PlateStateStore(state_path=str(tmp_path / "plates.json")),
        decks=DeckDeclarationStore(state_path=str(tmp_path / "deck.json")),
    )
    service.observations = ObservationCache(qualification="dry_run", boot_id="boot-fixture-dry", clock=FakeTime())
    steps = [{"after": "dry run", "observation": service.observation_snapshot().model_dump(mode="json")}]
    _write_or_compare("hte", "dry_run", "Dry run: qualified dry_run, no coordinates, no calibration I/O.", steps)


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def _position(snapshot: Dict[str, Any], mount: str) -> Dict[str, Any]:
    return snapshot["mounts"][mount]["position"]


def test_route_makes_no_robot_request_and_takes_no_service_lock(tmp_path, monkeypatch):
    h = Harness(tmp_path, "hte")
    h.load()
    posted = len(h.robot.posted)
    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", Mock(side_effect=AssertionError("robot I/O")))
    app = create_app(dry_run=False, auto_reconnect=False, enforce_claims=True)
    app.state.service.__dict__.update(h.service.__dict__)
    assert h.service._command_lock.acquire(blocking=False)
    try:
        app.state.service._command_lock = h.service._command_lock
        with TestClient(app) as http:
            reply = http.get("/observations")  # no claim header, command lock held
        assert reply.status_code == 200
    finally:
        h.service._command_lock.release()
    assert len(h.robot.posted) == posted
    assert reply.json()["equipment_id"] == "ot2_hte"


def test_repeated_reads_keep_the_sequence_and_never_restamp(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.run(C.move_to_coordinates("right", {"x": 1.0, "y": 2.0, "z": 3.0}))
    first, second = h.observe("a"), h.observe("b")
    assert first["observation_seq"] == second["observation_seq"]
    assert first["response_generated_at"] != second["response_generated_at"]
    assert _position(first, "right")["acquired_at"] == _position(second, "right")["acquired_at"]
    h.run(C.move_to_coordinates("right", {"x": 1.0, "y": 2.0, "z": 4.0}))
    assert h.observe("c")["observation_seq"] == first["observation_seq"] + 1


def test_pickup_and_drop_never_publish_an_endpoint(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.run(C.pick_up_tip("right", "tips", "A1"), result={"tipLength": 50.0, "position": {"x": 1.0, "y": 2.0, "z": 3.0}})
    assert _position(h.observe("pick"), "right")["xyz_mm"] is None
    h.run(C.drop_tip_in_place("right"))
    assert _position(h.observe("drop"), "right")["xyz_mm"] is None


def test_unattributable_pipette_supersedes_but_publishes_nothing(tmp_path):
    """A pipette the cache did not see loaded (an adopted run) has no mount."""
    h = Harness(tmp_path, "hte")
    h.load()
    h.run(C.move_to_coordinates("right", {"x": 1.0, "y": 2.0, "z": 3.0}))
    h.run(C.move_to_coordinates("adopted-id", {"x": 5.0, "y": 5.0, "z": 5.0}))
    right = _position(h.observe("x"), "right")
    assert right["valid"] == "superseded" and right["reason"] == "unattributed_move"


def test_delayed_success_after_invalidation_is_not_resurrected(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.run(C.move_to_coordinates("right", {"x": 1.0, "y": 2.0, "z": 3.0}))
    h.robot.during = lambda: h.service.observations.invalidate_all("robot_unreachable")
    h.run(C.move_to_coordinates("right", {"x": 4.0, "y": 5.0, "z": 6.0}))
    position = _position(h.observe("x"), "right")
    assert position["valid"] == "invalid" and position["xyz_mm"] is None


def test_no_commit_while_an_external_run_holds_the_robot(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.service.state = OT2ServiceState.EXTERNAL_CONTROL
    h.run(C.save_position("right"), result={"positionId": "p", "position": {"x": 1.0, "y": 2.0, "z": 3.0}})
    assert _position(h.observe("x"), "right")["xyz_mm"] is None


def test_stop_starts_a_new_session_generation(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.run(C.move_to_coordinates("right", {"x": 1.0, "y": 2.0, "z": 3.0}))
    generation = h.service.observations.session_generation
    h.control.client.stop_and_confirm = Mock()
    h.service._stop_latch_path = tmp_path / "stop.json"
    h.service.stop()
    snapshot = h.observe("stopped")
    assert snapshot["robot"]["session_generation"] == generation + 1
    assert _position(snapshot, "right")["reason"] == "session_lost"


def test_a_failing_observer_never_changes_a_command_result(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.client.observer.outcome = Mock(side_effect=RuntimeError("bug"))
    result = h.run(C.move_to_coordinates("right", {"x": 1.0, "y": 2.0, "z": 3.0}))
    assert result["status"] == "succeeded"
    assert h.observe("x")["degraded"].startswith("observation hook failed")


def test_clock_offset_bounds_contain_the_true_offset(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    offset = h.observe("x")["clock"]["robot_offset_s"]
    assert offset["min"] <= ROBOT_CLOCK_AHEAD_S <= offset["max"]
    assert offset["width"] == pytest.approx(offset["max"] - offset["min"])
    assert offset["samples"] >= 1


def test_ssh_read_move_read_never_shares_an_epoch(tmp_path):
    service = OT2Service(
        dry_run=False,
        transport="ssh",
        tips=TipStateStore(state_path=tmp_path / "tips.json"),
        plates=PlateStateStore(state_path=str(tmp_path / "plates.json")),
        decks=DeckDeclarationStore(state_path=str(tmp_path / "deck.json")),
    )
    service.observations = ObservationCache(clock=FakeTime(), blocked=service._observation_blocked)
    service.observations.new_session("session_lost")
    service.control = Mock()
    service.control.get_pipette_position.return_value = {"x": 1.0, "y": 2.0, "z": 3.0}
    service.state = OT2ServiceState.READY
    service.refresh_snapshot = Mock()
    service._session_pipettes = {"left": "left_alias", "right": "right_alias"}
    service._last_probe = {"reachable": True, "instruments": [{"mount": "left"}, {"mount": "right"}], "probe_errors": {}}
    service.pipette_position(PipettePositionRequest(pipette="left"))
    service.pipette_position(PipettePositionRequest(pipette="right"))
    both = service.observation_snapshot().model_dump(mode="json")
    assert both["mounts"]["left"]["position"]["epoch"] == both["mounts"]["right"]["position"]["epoch"]
    assert both["mounts"]["left"]["position"]["source"] == "driver_step_count"
    service._run_action("home", lambda: None, idempotent=True)
    after = service.observation_snapshot().model_dump(mode="json")
    assert after["mounts"]["left"]["position"]["valid"] == "superseded"
    assert after["motion"]["epoch"] > both["motion"]["epoch"]


def test_status_and_observations_share_inventory_revisions(tmp_path):
    h = Harness(tmp_path, "hte")
    revisions = h.service.get_status().details["revisions"]
    assert h.observe("x")["inventory"] == {**revisions, "details": "/status"}


def test_instrument_serial_change_invalidates_that_mount(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.service.observations.instruments_reconciled({"left": "SERIAL-L", "right": "SERIAL-A"})
    h.run(C.move_to_coordinates("right", {"x": 1.0, "y": 2.0, "z": 3.0}))
    h.service.observations.instruments_reconciled({"left": "SERIAL-L", "right": "SERIAL-B"})
    position = _position(h.observe("x"), "right")
    assert position["valid"] == "invalid" and position["reason"] == "instrument_changed"


# --- regressions from the Codex implementation review ----------------------


def test_observation_failure_never_blocks_a_stop(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.control.client.stop_and_confirm = Mock()
    h.service._stop_latch_path = tmp_path / "stop.json"
    h.service.observations.new_session = Mock(side_effect=RuntimeError("bug"))
    h.service.stop()
    h.control.client.stop_and_confirm.assert_called_once()
    assert h.service._stop_confirmed is True


def test_failed_pre_send_degrades_instead_of_leaving_old_positions_current(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.run(C.move_to_coordinates("right", {"x": 1.0, "y": 2.0, "z": 3.0}))
    h.service.observations._plan_context = Mock(side_effect=RuntimeError("bug"))
    result = h.run(C.move_to_coordinates("right", {"x": 9.0, "y": 9.0, "z": 9.0}))
    assert result["status"] == "succeeded"
    snapshot = h.observe("x")
    assert snapshot["degraded"]
    assert _position(snapshot, "right")["valid"] == "invalid"
    assert snapshot["motion"]["state"] == "motion_unknown"


def test_ambiguous_drop_forgets_tip_geometry(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.run(C.pick_up_tip("right", "tips", "A1"), result={"tipLength": 50.0, "position": {"x": 1.0, "y": 2.0, "z": 3.0}})
    with pytest.raises(OSError):
        h.run(C.drop_tip_in_place("right"), **{"raise": OSError("reply lost")})
    h.run(C.save_position("right"), result={"positionId": "p", "position": {"x": 1.0, "y": 2.0, "z": 3.0}})
    mount = h.observe("x")["mounts"]["right"]
    assert mount["position"]["reference_point"]["vertical"] == "unknown"
    assert mount["tip"]["tip_length"] is None


def test_late_success_from_an_earlier_session_changes_nothing(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.robot.during = lambda: h.service.observations.new_session("session_lost")
    h.run(C.pick_up_tip("right", "tips", "A1"), result={"tipLength": 50.0, "position": {"x": 1.0, "y": 2.0, "z": 3.0}})
    snapshot = h.observe("x")
    assert snapshot["mounts"]["right"]["tip"]["tip_length"] is None
    assert snapshot["motion"]["last_command"]["stale"] is True
    assert snapshot["clock"]["robot_offset_s"] is None


def test_removed_instrument_is_never_revived_by_reattaching_it(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.service._record_probe(h.probe())
    h.run(C.move_to_coordinates("right", {"x": 1.0, "y": 2.0, "z": 3.0}))
    without_right = [i for i in h.probe()["instruments"] if i["mount"] != "right"]
    h.service._record_probe(h.probe(instruments=without_right))
    assert h.observe("removed")["mounts"]["right"] is None
    h.service._record_probe(h.probe())
    assert _position(h.observe("back"), "right")["reason"] == "instrument_removed"


def test_malformed_calibration_response_is_recorded_not_raised(tmp_path, monkeypatch):
    class Odd:
        def raise_for_status(self):
            return None

        def json(self):
            return {"unexpected": True}

    monkeypatch.setattr("opentrons_server.gateway.service.requests.get", lambda *a, **k: Odd())
    h = Harness(tmp_path, "hte")
    h.service._calibration_enabled = True
    h.service._refresh_calibration()
    calibration = h.observe("x")["calibration"]
    assert calibration["revision"] is None
    assert set(calibration["errors"]) == {"deck", "pipette_offsets", "tip_lengths"}


def test_ssh_jog_commits_its_final_readback_and_clears_motion_unknown(tmp_path):
    service = OT2Service(
        dry_run=False,
        transport="ssh",
        tips=TipStateStore(state_path=tmp_path / "tips.json"),
        plates=PlateStateStore(state_path=str(tmp_path / "plates.json")),
        decks=DeckDeclarationStore(state_path=str(tmp_path / "deck.json")),
    )
    service.observations = ObservationCache(clock=FakeTime(), blocked=service._observation_blocked)
    service.observations.new_session("session_lost")
    service.observations._motion_unknown = "ssh_action_failed"
    service.control = Mock()
    service.control.get_pipette_position.side_effect = [{"x": 1.0, "y": 2.0, "z": 3.0}, {"x": 1.0, "y": 2.0, "z": 4.0}]
    service.state = OT2ServiceState.READY
    service.refresh_snapshot = Mock()
    service._session_pipettes = {"left": "left_alias"}
    service._last_probe = {"reachable": True, "instruments": [{"mount": "left"}], "probe_errors": {}}
    service.jog(PipetteJogRequest(pipette="left", axis="z", distance_mm=1))
    snapshot = service.observation_snapshot().model_dump(mode="json")
    position = snapshot["mounts"]["left"]["position"]
    assert position["valid"] == "current" and position["xyz_mm"]["z"] == 4.0
    assert position["epoch"] == snapshot["motion"]["epoch"]
    assert snapshot["motion"]["state"] == "idle"


def test_late_success_after_reachability_loss_restores_nothing(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.robot.during = lambda: h.service.observations.invalidate_all("robot_unreachable")
    h.run(C.pick_up_tip("right", "tips", "A1"), result={"tipLength": 50.0, "position": {"x": 1.0, "y": 2.0, "z": 3.0}})
    h.run(C.save_position("right"), result={"positionId": "p", "position": {"x": 1.0, "y": 2.0, "z": 3.0}})
    mount = h.observe("x")["mounts"]["right"]
    assert mount["tip"]["tip_length"] is None
    assert mount["position"]["reference_point"]["vertical"] == "unknown"


def test_failed_ssh_read_invalidates_the_earlier_position(tmp_path):
    service = OT2Service(
        dry_run=False,
        transport="ssh",
        tips=TipStateStore(state_path=tmp_path / "tips.json"),
        plates=PlateStateStore(state_path=str(tmp_path / "plates.json")),
        decks=DeckDeclarationStore(state_path=str(tmp_path / "deck.json")),
    )
    service.observations = ObservationCache(clock=FakeTime(), blocked=service._observation_blocked)
    service.observations.new_session("session_lost")
    service.control = Mock()
    service.control.get_pipette_position.side_effect = [{"x": 1.0, "y": 2.0, "z": 3.0}, RuntimeError("not homed")]
    service.state = OT2ServiceState.READY
    service.refresh_snapshot = Mock()
    service._session_pipettes = {"left": "left_alias"}
    service._last_probe = {"reachable": True, "instruments": [{"mount": "left"}], "probe_errors": {}}
    service.pipette_position(PipettePositionRequest(pipette="left"))
    with pytest.raises(RuntimeError):
        service.pipette_position(PipettePositionRequest(pipette="left"))
    position = service.observation_snapshot().model_dump(mode="json")["mounts"]["left"]["position"]
    assert position["valid"] == "invalid" and position["reason"] == "read_failed"


def test_positions_are_not_current_while_a_stop_is_under_way(tmp_path):
    h = Harness(tmp_path, "hte")
    h.load()
    h.run(C.move_to_coordinates("right", {"x": 1.0, "y": 2.0, "z": 3.0}))
    seen = {}

    def stopping():
        seen["snapshot"] = h.service.observation_snapshot().model_dump(mode="json")

    h.control.client.stop_and_confirm = Mock(side_effect=stopping)
    h.service._stop_latch_path = tmp_path / "stop.json"
    h.service.stop()
    position = seen["snapshot"]["mounts"]["right"]["position"]
    assert position["valid"] == "superseded" and position["reason"] == "stop_in_progress"


def test_own_run_is_never_reported_as_an_external_run(tmp_path):
    h = Harness(tmp_path, "hte")
    h.service._last_probe = h.probe(active_runs=[{"id": h.client.run_id, "status": "running"}, {"id": "run-app", "status": "running"}])
    assert h.observe("x")["run"]["external_run_ids"] == ["run-app"]
