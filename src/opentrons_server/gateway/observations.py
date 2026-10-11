"""Side-effect-free observations for external followers (docs/OBSERVATIONS.md).

``ObservationCache`` holds what this gateway has observed about pipette
positions, from the commands it sends itself (two hooks in
``RunEngineClient.execute``) and its own explicit reads. ``GET /observations``
serves it with no robot I/O and without the command lock.

Every value carries where it came from, when, and whether it is still
current. A command target is never a position; a pickup or drop result (an
approach point) is never an endpoint; nothing is filled from a default.

Rules here follow Opentrons v8.7.0 (the version both robots run); the command
table is versioned with it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

OBSERVATION_SCHEMA_VERSION = 1
COMMAND_RULES_VERSION = "opentrons-8.7.0"
MOUNTS = ("left", "right")

# §2.4: commands whose result position is where the pipette ends up. Only the
# plunger moves after aspirate / dispense / blowout; touchTip returns its final
# point. Pickup and drop return the approach point and are deliberately absent.
TERMINAL_COMMANDS = frozenset(
    {
        "moveToWell",
        "moveToCoordinates",
        "moveToAddressableArea",
        "moveToAddressableAreaForDropTip",
        "aspirate",
        "dispense",
        "blowout",
        "touchTip",
    }
)
# §4.3: commands that cannot move the gantry. Short and explicit: anything not
# listed counts as motion.
NON_MOTION_COMMANDS = frozenset(
    {
        "savePosition",
        "loadPipette",
        "loadLabware",
        "loadModule",
        "comment",
        "waitForDuration",
        "temperatureModule/setTargetTemperature",
        "temperatureModule/waitForTemperature",
        "temperatureModule/deactivate",
    }
)
# Commands whose targets name a well; centering depends on that labware.
_WELL_TARGET_COMMANDS = frozenset(
    {"moveToWell", "aspirate", "dispense", "blowout", "touchTip"}
)
_COORDINATE_COMMANDS = frozenset(
    {"moveToCoordinates", "moveToAddressableArea", "moveToAddressableAreaForDropTip"}
)
# Succeeded, these leave the engine's current location off any well.
_LOCATION_RESETTING = frozenset(
    {"home", "retractAxis", "moveToCoordinates", "moveToAddressableArea", "moveToAddressableAreaForDropTip"}
)
_TIP_ON = frozenset({"pickUpTip"})
_TIP_OFF = frozenset({"dropTip", "dropTipInPlace"})

# GEN2 8-channel nozzle pitch; the primary nozzle (A1) sits (n - 1) * pitch / 2
# behind the centre of the full column (shared-data geometry nozzleOffset 31.5).
_NOZZLE_PITCH_MM = 9.0
# §4.7: robot clocks drift; widen each bound by this rate times its age.
_CLOCK_DRIFT_PPM = 50.0
_CLOCK_WINDOW_S = 600.0
_CLOCK_MAX_SAMPLES = 50

Outcome = Literal["succeeded", "failed", "not_completed", "transport_unknown", "interrupted"]
Validity = Literal["current", "superseded", "invalid", "unavailable"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def channels_from_model(model: Optional[str]) -> Optional[int]:
    """Channel count from an Opentrons pipette name; None when not stated."""
    if not model:
        return None
    name = model.lower()
    if "_single" in name:
        return 1
    if "_multi" in name:
        return 8
    if "_96" in name:
        return 96
    return None


def digest(value: Any) -> str:
    """Opaque, order-independent content revision."""
    blob = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Response models (documented in /openapi.json)
# ---------------------------------------------------------------------------


class Xyz(BaseModel):
    x: float
    y: float
    z: float


class ReferencePoint(BaseModel):
    horizontal: Literal["primary_nozzle", "nozzle_set_y_center", "nozzle_set_xy_center", "unknown"]
    vertical: Literal["nozzle_end", "tip_end", "unknown"]
    nozzle: Optional[str] = None
    centered_on_labware: Optional[str] = None
    # primary_xyz = reported_xyz + to_primary_nozzle_mm; null when not known.
    to_primary_nozzle_mm: Optional[Xyz] = None


class PositionObservation(BaseModel):
    valid: Validity
    reason: Optional[str] = None
    superseded_by_move_id: Optional[str] = None
    xyz_mm: Optional[Xyz] = None
    source: Optional[Literal["command_endpoint", "stored_endpoint", "driver_step_count"]] = None
    reference_point: Optional[ReferencePoint] = None
    acquired_at: Optional[datetime] = None
    effective_at: Optional[datetime] = None
    effective_at_clock: Optional[Literal["robot", "gateway"]] = None
    move_id: Optional[str] = None
    command_id: Optional[str] = None
    session_generation: Optional[int] = None
    validity_generation: Optional[int] = None
    epoch: Optional[int] = None
    geometry_revision: Optional[int] = None


class InstrumentObservation(BaseModel):
    valid: Literal["current", "last_known", "unknown"]
    model: Optional[str] = None
    name: Optional[str] = None
    serial: Optional[str] = None
    channels: Optional[int] = None
    source: Optional[Literal["robot_instruments"]] = None
    acquired_at: Optional[datetime] = None
    loaded_in_session: Optional[bool] = None


class TipLedgerObservation(BaseModel):
    has_tip: Optional[bool] = None
    rack: Optional[str] = None
    well: Optional[str] = None
    uncertain: Optional[bool] = None
    source: Literal["service_accounting"] = "service_accounting"
    effective_at: Optional[datetime] = None


class TipEngineObservation(BaseModel):
    has_tip: Optional[bool] = None
    source: Literal["run_engine_state"] = "run_engine_state"
    acquired_at: Optional[datetime] = None


class TipLengthObservation(BaseModel):
    value_mm: float
    source: Literal["engine_pickup_result"] = "engine_pickup_result"
    command_id: Optional[str] = None
    robot_run_id: Optional[str] = None
    instrument_serial: Optional[str] = None
    tip_rack_load_name: Optional[str] = None
    tip_rack_definition_uri: Optional[str] = None
    tip_rack_definition_sha256: Optional[str] = None
    acquired_at: datetime


class TipObservation(BaseModel):
    ledger: TipLedgerObservation
    engine: TipEngineObservation
    tip_length: Optional[TipLengthObservation] = None


class MountObservation(BaseModel):
    instrument: InstrumentObservation
    position: PositionObservation
    tip: TipObservation


class InFlightCommand(BaseModel):
    state: Literal["submitted"] = "submitted"
    move_id: str
    command_type: str
    mount: Optional[str] = None
    submitted_at: datetime
    plan: Optional[Dict[str, Any]] = None
    target: Optional[Dict[str, Any]] = None


class LastCommand(BaseModel):
    move_id: str
    command_id: Optional[str] = None
    command_type: str
    mount: Optional[str] = None
    outcome: Outcome
    motion: bool
    # Began under an earlier session or while degraded: history only, it
    # changed no current state.
    stale: bool = False
    submitted_at: datetime
    observed_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    completed_at_clock: Literal["robot"] = "robot"
    error_type: Optional[str] = None


class MotionObservation(BaseModel):
    state: Literal["idle", "in_flight", "paused", "external_run", "motion_unknown"]
    reason: Optional[str] = None
    epoch: int
    in_flight: Optional[InFlightCommand] = None
    last_command: Optional[LastCommand] = None


class ClockOffset(BaseModel):
    """offset = robot_time - gateway_time, known to lie in [min, max]."""

    min: float
    max: float
    width: float
    samples: int
    from_command: Optional[str] = None
    measured_at: datetime
    resolution_s: float
    # Widen [min, max] by drift_ppm * 1e-6 * seconds since measured_at.
    drift_ppm: float


class ClockObservation(BaseModel):
    gateway: Literal["utc_system"] = "utc_system"
    robot_offset_s: Optional[ClockOffset] = None
    robot_offset_reason: Optional[str] = None


class FrameObservation(BaseModel):
    id: Literal["ot2_deck"] = "ot2_deck"
    units: Literal["mm"] = "mm"
    axes: Dict[str, str] = Field(default_factory=lambda: {"x": "right", "y": "back", "z": "up"})
    origin: Literal["opentrons_deck_coordinates"] = "opentrons_deck_coordinates"
    applied: List[str] = Field(
        default_factory=lambda: [
            "deck_calibration",
            "mount_offset",
            "pipette_offset_calibration",
            "tip_length_when_attached",
        ]
    )
    not_applied: List[str] = Field(default_factory=lambda: ["labware_offsets"])


class CalibrationObservation(BaseModel):
    """Calibration on file on the robot; not proof of the geometry a sample used."""

    enabled: bool
    revision: Optional[str] = None
    read_at: Optional[datetime] = None
    errors: Dict[str, str] = Field(default_factory=dict)
    deck: Optional[Dict[str, Any]] = None
    pipette_offsets: Optional[List[Dict[str, Any]]] = None
    tip_lengths: Optional[List[Dict[str, Any]]] = None


class RobotObservation(BaseModel):
    reachable: Optional[bool] = None
    last_seen_at: Optional[datetime] = None
    transport: str
    session_generation: int
    robot_run_id: Optional[str] = None


class InventoryObservation(BaseModel):
    tip_racks_revision: str
    mounted_tips_revision: str
    deck_revision: str
    loaded_plate_revision: str
    details: Literal["/status"] = "/status"


class RunObservation(BaseModel):
    plan_id: Optional[str] = None
    plan_step: Optional[int] = None
    plan_action: Optional[str] = None
    robot_run_id: Optional[str] = None
    external_run_active: Optional[bool] = None
    external_run_ids: Optional[List[str]] = None


class ObservationResponse(BaseModel):
    schema_version: int = OBSERVATION_SCHEMA_VERSION
    command_rules: str = COMMAND_RULES_VERSION
    equipment_id: str
    boot_id: str
    observation_seq: int
    validity_generation: int
    response_generated_at: datetime
    qualification: Literal["hardware", "simulation", "dry_run"]
    # How qualification is known: "configured" (dry run, SSH simulation) or
    # "http_robot_server" (the robot-server cannot say whether it simulates).
    qualification_basis: str
    degraded: Optional[str] = None
    event_watermark: Optional[int] = None  # Phase 2
    clock: ClockObservation
    frame: FrameObservation
    calibration: CalibrationObservation
    robot: RobotObservation
    motion: MotionObservation
    # None only when a fresh, successful instruments read omits that mount.
    mounts: Dict[str, Optional[MountObservation]]
    inventory: InventoryObservation
    run: RunObservation


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass
class Ticket:
    """One command or read, with the tokens current when it began (§4.4)."""

    move_id: str
    command_type: str
    params: Dict[str, Any]
    motion: bool
    mount: Optional[str]
    submitted_at: datetime
    tokens: Tuple[int, int, int, int, int]  # session, validity, epoch, geom left, geom right
    plan: Optional[Dict[str, Any]] = None


@dataclass
class _MountState:
    position: Dict[str, Any] = field(
        default_factory=lambda: {"valid": "unavailable", "reason": "not_acquired"}
    )
    vertical: str = "unknown"
    tip_length: Optional[Dict[str, Any]] = None
    serial: Optional[str] = None


class ObservationCache:
    """Thread-safe store behind ``GET /observations``.

    Locking (docs/OBSERVATIONS.md §5): ``self._lock`` guards state and is held
    only for short in-memory updates; ``self._publish_lock`` serializes the
    content digest and sequence for readers, so hashing never blocks a command
    hook. Neither is held while waiting on anything else, and no method calls a
    transport. ``blocked`` reads plain service attributes and takes no lock.

    Trust: once anything in the observation path fails (``degrade``), every
    position is invalid and nothing is committed until the next session.
    """

    def __init__(
        self,
        *,
        qualification: str = "hardware",
        qualification_basis: str = "configured",
        boot_id: Optional[str] = None,
        clock: Callable[[], datetime] = _utcnow,
        blocked: Callable[[], Optional[str]] = lambda: None,
        plan_context: Callable[[], Optional[Dict[str, Any]]] = lambda: None,
        new_id: Callable[[str], str] = lambda prefix: f"{prefix}-{uuid.uuid4().hex[:12]}",
    ) -> None:
        self._lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._new_id = new_id
        self.boot_id = boot_id or str(uuid.uuid4())
        self.qualification = qualification
        self.qualification_basis = qualification_basis
        self._clock = clock
        self._blocked = blocked
        self._plan_context = plan_context
        self.session_generation = 0
        self.validity_generation = 0
        self.epoch = 0
        self._geometry = {m: 0 for m in MOUNTS}
        self._mounts = {m: _MountState() for m in MOUNTS}
        if qualification == "dry_run":
            for state in self._mounts.values():
                state.position = {"valid": "unavailable", "reason": "dry_run"}
        self._pipettes: Dict[str, Dict[str, Any]] = {}  # engine id -> mount, model, channels
        self._labware: Dict[str, Dict[str, Any]] = {}  # engine id -> centering facts
        # The engine's single current location, tracked strictly (§4.5): only
        # outcomes that settle it make it certain. Independent of the jog
        # guard's mirror in RunEngineClient, which is conservative the other way.
        self._location: Optional[Tuple[str, str]] = None
        self._location_certain = False
        self._in_flight: Optional[Ticket] = None
        self._last_command: Optional[Dict[str, Any]] = None
        self._last_motion_completed_at: Optional[datetime] = None
        self._last_motion_succeeded = False
        self._motion_unknown: Optional[str] = None
        self._clock_samples: Deque[Tuple[datetime, float, float, str]] = deque()
        self._clock_reason: Optional[str] = "no_command_observed"
        self._degraded: Optional[str] = None
        self._robot_run_id: Optional[str] = None
        self._seq = 0
        self._last_digest: Optional[str] = None

    # -- tokens --------------------------------------------------------------

    def _tokens(self) -> Tuple[int, int, int, int, int]:
        return (
            self.session_generation,
            self.validity_generation,
            self.epoch,
            self._geometry["left"],
            self._geometry["right"],
        )

    def _committable(self, ticket: Ticket) -> bool:
        return ticket.tokens == self._tokens() and self._degraded is None and self._blocked() is None

    # -- session and validity events (called by the service) -----------------

    def new_session(self, reason: str) -> None:
        """A session was created, lost, shut down, stopped or rebuilt."""
        with self._lock:
            self.session_generation += 1
            self._pipettes.clear()
            self._labware.clear()
            self._location, self._location_certain = None, True
            self._in_flight = None
            self._motion_unknown = None
            self._degraded = None
            self._last_motion_completed_at = None
            self._last_motion_succeeded = False
            self._clock_samples.clear()
            self._clock_reason = "no_command_observed"
            self._robot_run_id = None
            for mount, state in self._mounts.items():
                self._forget_tip(mount)
                self._invalidate(state, reason)

    def set_qualification(self, qualification: str, basis: str) -> None:
        with self._lock:
            self.qualification = qualification
            self.qualification_basis = basis

    def register_pipette(self, pipette_id: str, mount: str, model: Optional[str]) -> None:
        """A pipette already in the run (adopted, not loaded by a command we saw)."""
        if mount not in self._mounts:
            return
        with self._lock:
            self._pipettes.setdefault(
                pipette_id, {"mount": mount, "model": model, "channels": channels_from_model(model)}
            )

    def set_robot_run_id(self, run_id: Optional[str]) -> None:
        with self._lock:
            self._robot_run_id = run_id

    def invalidate_all(self, reason: str) -> None:
        """Positions and tips may have changed outside our view (robot unreachable)."""
        with self._lock:
            self.validity_generation += 1
            self._motion_unknown = reason
            self._location_certain = False
            for mount, state in self._mounts.items():
                self._forget_tip(mount)
                self._invalidate(state, reason)

    def supersede_all(self, reason: str) -> None:
        """Something else may be moving the gantry or changing tips (an external run)."""
        with self._lock:
            self.validity_generation += 1
            self._location_certain = False
            for mount, state in self._mounts.items():
                self._forget_tip(mount)
                self._supersede(state, reason, None)

    def instruments_reconciled(self, serials: Dict[str, Optional[str]]) -> None:
        """A fresh, successful instruments read: ``mount -> serial`` (None = absent).

        A removed or changed instrument invalidates that mount; reattaching the
        same serial later never revives its old position (§6).
        """
        with self._lock:
            for mount in MOUNTS:
                state = self._mounts[mount]
                serial = serials.get(mount)
                if serial is None:
                    reason = "instrument_removed" if state.serial or state.position.get("valid") != "unavailable" else None
                elif state.serial and serial != state.serial:
                    reason = "instrument_changed"
                else:
                    reason = None
                if reason:
                    self.validity_generation += 1
                    self._geometry[mount] += 1
                    self._forget_tip(mount)
                    self._invalidate(state, reason)
                state.serial = serial

    def degrade(self, reason: str) -> None:
        """Observation processing failed: trust nothing until the next session."""
        with self._lock:
            if self._degraded is None:
                self._degraded = reason
            self.validity_generation += 1
            self._motion_unknown = "observation_degraded"
            self._location_certain = False
            for mount, state in self._mounts.items():
                self._forget_tip(mount)
                self._invalidate(state, "observation_degraded")

    # Kept for RunEngineClient, which reports its own hook failures this way.
    mark_degraded = degrade

    def _forget_tip(self, mount: str) -> None:
        state = self._mounts[mount]
        state.vertical = "unknown"
        state.tip_length = None

    @staticmethod
    def _invalidate(state: _MountState, reason: str) -> None:
        if state.position.get("valid") == "unavailable":
            return  # never acquired (or dry run): nothing to invalidate
        state.position = {"valid": "invalid", "reason": reason}

    @staticmethod
    def _supersede(state: _MountState, reason: str, move_id: Optional[str]) -> None:
        if state.position.get("valid") in ("current", "superseded"):
            state.position = {
                **state.position,
                "valid": "superseded",
                "reason": reason,
                "superseded_by_move_id": move_id,
            }

    # -- HTTP command hooks ----------------------------------------------------

    def pre_send(self, command_type: str, params: Dict[str, Any]) -> Ticket:
        """Before any I/O: advance the epoch and supersede both mounts (§3.2)."""
        plan = self._plan_context()
        with self._lock:
            motion = command_type not in NON_MOTION_COMMANDS
            mount = self._mount_of(params.get("pipetteId"))
            if command_type == "loadPipette":
                mount = params.get("mount")
            move_id = self._new_id("gw")
            if motion:
                self.epoch += 1
                for state in self._mounts.values():
                    self._supersede(state, "move_in_flight", move_id)
            ticket = Ticket(
                move_id=move_id,
                command_type=command_type,
                params=copy.deepcopy(params),
                motion=motion,
                mount=mount,
                submitted_at=self._clock(),
                tokens=self._tokens(),
                plan=plan,
            )
            if motion:
                self._in_flight = ticket
            return ticket

    def outcome(self, ticket: Ticket, outcome: Outcome, command: Optional[Dict[str, Any]], **_: Any) -> None:
        """After the command, on every exit path. Never changes its result.

        A command that began under another session (or while degraded) is
        recorded as history only: it may not change any current state.
        """
        observed_at = self._clock()
        command = command or {}
        result = command.get("result") if isinstance(command.get("result"), dict) else {}
        started = _parse_time(command.get("startedAt"))
        completed = _parse_time(command.get("completedAt"))
        error = command.get("error") if isinstance(command.get("error"), dict) else {}
        with self._lock:
            if self._in_flight is ticket:
                self._in_flight = None
            # Anything that invalidated observations while this command was out
            # (session change, reachability loss, external run, instrument
            # change, degradation, a stop) makes its outcome history only.
            stale = (
                ticket.tokens != self._tokens()
                or self._degraded is not None
                or self._blocked() is not None
            )
            self._last_command = {
                "move_id": ticket.move_id,
                "command_id": command.get("id"),
                "command_type": ticket.command_type,
                "mount": ticket.mount,
                "outcome": outcome,
                "motion": ticket.motion,
                "stale": stale,
                "submitted_at": ticket.submitted_at,
                "observed_at": observed_at,
                "started_at": started,
                "completed_at": completed,
                "error_type": error.get("errorType"),
            }
            if stale:
                return
            same_validity = True
            succeeded = outcome == "succeeded"
            self._learn(ticket, succeeded, result, command)
            self._track_location(ticket, succeeded)
            if succeeded and started and completed:
                self._clock_sample(ticket.submitted_at, observed_at, started, completed, command.get("id"))
            if not ticket.motion:
                if ticket.command_type == "savePosition":
                    self._read_outcome(ticket, succeeded, same_validity, result, observed_at)
                return
            self._motion_outcome(ticket, outcome, same_validity, result, command, observed_at, completed)

    def _mounts_for(self, ticket: Ticket) -> List[str]:
        """The mount a command concerns, or both when it cannot be attributed."""
        return [ticket.mount] if ticket.mount in self._mounts else list(MOUNTS)

    def _learn(self, ticket: Ticket, succeeded: bool, result: Dict[str, Any], command: Dict[str, Any]) -> None:
        params = ticket.params
        if not succeeded:
            if ticket.command_type in _TIP_ON | _TIP_OFF:
                # The tip may or may not have changed.
                for mount in self._mounts_for(ticket):
                    self._geometry[mount] += 1
                    self._forget_tip(mount)
            return
        if ticket.command_type == "loadPipette":
            pipette_id = result.get("pipetteId") or params.get("pipetteId")
            if pipette_id:
                model = params.get("pipetteName")
                self._pipettes[pipette_id] = {
                    "mount": params.get("mount"),
                    "model": model,
                    "channels": channels_from_model(model),
                }
        elif ticket.command_type == "loadLabware":
            labware_id = result.get("labwareId") or params.get("labwareId")
            definition = result.get("definition") if isinstance(result.get("definition"), dict) else None
            if labware_id:
                self._labware[labware_id] = self._labware_facts(params, definition)
        elif ticket.command_type in _TIP_ON | _TIP_OFF:
            for mount in self._mounts_for(ticket):
                self._geometry[mount] += 1
                self._forget_tip(mount)
            if ticket.mount not in self._mounts:
                return
            state = self._mounts[ticket.mount]
            if ticket.command_type in _TIP_OFF:
                state.vertical = "nozzle_end"
                return
            state.vertical = "tip_end"
            length = result.get("tipLength")
            labware = self._labware.get(params.get("labwareId") or "", {})
            if isinstance(length, (int, float)) and not isinstance(length, bool):
                state.tip_length = {
                    "value_mm": float(length),
                    "command_id": command.get("id"),
                    "robot_run_id": self._robot_run_id,
                    "instrument_serial": state.serial,
                    "tip_rack_load_name": labware.get("load_name"),
                    "tip_rack_definition_uri": labware.get("uri"),
                    "tip_rack_definition_sha256": labware.get("sha256"),
                    "acquired_at": self._clock(),
                }

    def _track_location(self, ticket: Ticket, succeeded: bool) -> None:
        """The engine's current location, certain only when an outcome settles it."""
        params = ticket.params
        if not ticket.motion:
            return
        if not succeeded:
            self._location_certain = False
            return
        if all(params.get(key) for key in ("pipetteId", "labwareId", "wellName")):
            self._location, self._location_certain = (params["pipetteId"], params["labwareId"]), True
        elif ticket.command_type in _LOCATION_RESETTING:
            self._location, self._location_certain = None, True
        elif ticket.command_type == "moveLabware":
            if params.get("strategy") == "usingGripper" or (
                self._location is not None and self._location[1] == params.get("labwareId")
            ):
                self._location = None
        else:
            self._location_certain = False

    @staticmethod
    def _labware_facts(params: Dict[str, Any], definition: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if definition is None:
            return {"known": False, "load_name": params.get("loadName")}
        parameters = definition.get("parameters") or {}
        wells = definition.get("wells") or {}
        load_name = parameters.get("loadName") or params.get("loadName")
        return {
            "known": True,
            "load_name": load_name,
            "uri": f"{definition.get('namespace')}/{load_name}/{definition.get('version')}",
            "sha256": hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest(),
            "centering": "centerMultichannelOnWells" in (parameters.get("quirks") or []),
            "well_count": len(wells),
        }

    def _motion_outcome(
        self,
        ticket: Ticket,
        outcome: str,
        same_validity: bool,
        result: Dict[str, Any],
        command: Dict[str, Any],
        observed_at: datetime,
        completed: Optional[datetime],
    ) -> None:
        mount = ticket.mount
        if outcome != "succeeded":
            self._last_motion_succeeded = False
            if outcome == "failed":
                # The controller clears its stored position on a failed move.
                for state in self._mounts.values():
                    self._invalidate(state, "move_failed")
            else:
                self._motion_unknown = outcome
                for state in self._mounts.values():
                    self._invalidate(state, "pose_unknown_after_command")
            return
        if same_validity:
            self._motion_unknown = None
        self._last_motion_succeeded = True
        self._last_motion_completed_at = completed
        # The gantry moved; who moved it decides the superseded reason.
        moved_reason = "other_mount_moved" if mount in self._mounts else "unattributed_move"
        for other, state in self._mounts.items():
            if other != mount and state.position.get("valid") == "superseded":
                state.position = {**state.position, "reason": moved_reason}
        if mount not in self._mounts:
            return
        state = self._mounts[mount]
        position = result.get("position")
        if ticket.command_type not in TERMINAL_COMMANDS or not _is_xyz(position) or not self._committable(ticket):
            self._invalidate(state, "pose_unknown_after_command")
            return
        state.position = {
            "valid": "current",
            "reason": None,
            "xyz_mm": {k: float(position[k]) for k in ("x", "y", "z")},
            "source": "command_endpoint",
            "reference_point": self._endpoint_reference(ticket, mount),
            "acquired_at": observed_at,
            "effective_at": completed,
            "effective_at_clock": "robot" if completed else None,
            "move_id": ticket.move_id,
            "command_id": command.get("id"),
            **self._token_fields(mount),
        }

    def _read_outcome(
        self,
        ticket: Ticket,
        succeeded: bool,
        same_validity: bool,
        result: Dict[str, Any],
        observed_at: datetime,
    ) -> None:
        if not succeeded:
            # A read that failed (unhomed, transport) cannot confirm anything.
            for mount in self._mounts_for(ticket):
                self._invalidate(self._mounts[mount], "read_failed")
            return
        if same_validity:
            # The run engine queues commands, so a read that completed ran
            # after every earlier command: the gantry was at rest.
            self._motion_unknown = None
        mount = ticket.mount
        position = result.get("position")
        if mount not in self._mounts or not _is_xyz(position) or not self._committable(ticket):
            return
        state = self._mounts[mount]
        effective = self._last_motion_completed_at if self._last_motion_succeeded else None
        state.position = {
            "valid": "current",
            "reason": None,
            "xyz_mm": {k: float(position[k]) for k in ("x", "y", "z")},
            "source": "stored_endpoint",
            "reference_point": {**self._read_horizontal(ticket.params.get("pipetteId")), "vertical": state.vertical},
            "acquired_at": observed_at,
            "effective_at": effective,
            "effective_at_clock": "robot" if effective else None,
            "move_id": ticket.move_id,
            "command_id": None,
            **self._token_fields(mount),
        }

    def _token_fields(self, mount: str) -> Dict[str, Any]:
        return {
            "session_generation": self.session_generation,
            "validity_generation": self.validity_generation,
            "epoch": self.epoch,
            "geometry_revision": self._geometry[mount],
        }

    # -- reference points (§4.5) ---------------------------------------------

    def _mount_of(self, pipette_id: Optional[str]) -> Optional[str]:
        entry = self._pipettes.get(pipette_id or "")
        return entry.get("mount") if entry else None

    def _centered(self, pipette_id: Optional[str], labware_id: Optional[str]) -> Dict[str, Any]:
        """Horizontal reference for a pipette at a well of ``labware_id``."""
        pipette = self._pipettes.get(pipette_id or "")
        labware = self._labware.get(labware_id or "")
        channels = pipette.get("channels") if pipette else None
        if channels == 1:
            return {"horizontal": "primary_nozzle", "nozzle": "A1"}
        if not labware or not labware.get("known") or channels is None:
            return {"horizontal": "unknown"}
        if not labware.get("centering"):
            return {"horizontal": "primary_nozzle", "nozzle": "A1"}
        wells = labware.get("well_count") or 0
        kind = "nozzle_set_xy_center" if wells == 1 or wells >= 96 else "nozzle_set_y_center"
        offset = (
            {"x": 0.0, "y": (channels - 1) * _NOZZLE_PITCH_MM / 2, "z": 0.0}
            if channels == 8
            else None
        )
        return {"horizontal": kind, "centered_on_labware": labware_id, "to_primary_nozzle_mm": offset}

    def _endpoint_reference(self, ticket: Ticket, mount: str) -> Dict[str, Any]:
        params = ticket.params
        if ticket.command_type in _COORDINATE_COMMANDS:
            horizontal = {"horizontal": "primary_nozzle", "nozzle": "A1"}
        elif ticket.command_type in _WELL_TARGET_COMMANDS:
            horizontal = self._centered(params.get("pipetteId"), params.get("labwareId"))
        else:
            horizontal = {"horizontal": "unknown"}
        return {**horizontal, "vertical": self._mounts[mount].vertical}

    def _read_horizontal(self, pipette_id: Optional[str]) -> Dict[str, Any]:
        """savePosition's critical point follows the engine's current location."""
        if not self._location_certain:
            return {"horizontal": "unknown"}
        if self._location is None or self._location[0] != pipette_id:
            return {"horizontal": "primary_nozzle", "nozzle": "A1"}
        return self._centered(pipette_id, self._location[1])

    # -- SSH (§3.2): service-level motion hook and explicit reads --------------

    def ssh_action(self, name: str, *, motion: bool) -> Ticket:
        return self.pre_send(name if motion else "savePosition", {})

    def ssh_action_done(self, ticket: Ticket, *, succeeded: bool) -> None:
        with self._lock:
            if self._in_flight is ticket:
                self._in_flight = None
            if not ticket.motion or ticket.tokens[0] != self.session_generation:
                return
            if succeeded:
                if ticket.tokens[1] == self.validity_generation:
                    self._motion_unknown = None
                for state in self._mounts.values():
                    if state.position.get("valid") == "superseded":
                        state.position = {**state.position, "reason": "unattributed_move"}
            else:
                self._motion_unknown = "ssh_action_failed"
                for mount, state in self._mounts.items():
                    self._forget_tip(mount)
                    self._invalidate(state, "pose_unknown_after_command")

    def begin_read(self, mount: Optional[str]) -> Ticket:
        with self._lock:
            return Ticket(
                move_id=self._new_id("read"),
                command_type="read",
                params={},
                motion=False,
                mount=mount,
                submitted_at=self._clock(),
                tokens=self._tokens(),
            )

    def driver_read_failed(self, ticket: Ticket) -> None:
        """An SSH read that failed cannot confirm the earlier value."""
        with self._lock:
            for mount in self._mounts_for(ticket):
                self._invalidate(self._mounts[mount], "read_failed")

    def state_token(self) -> Tuple[Any, ...]:
        """Changes whenever cached state may have; snapshot() retries on it."""
        with self._lock:
            return (*self._tokens(), id(self._last_command), id(self._in_flight), self._degraded)

    def commit_driver_read(self, ticket: Ticket, xyz: Dict[str, float]) -> None:
        """An SSH ``M114.2`` read: default critical point, tip state unknown."""
        observed_at = self._clock()
        with self._lock:
            mount = ticket.mount
            if (
                mount not in self._mounts
                or not _is_xyz(xyz)
                or self._in_flight is not None
                or not self._committable(ticket)
            ):
                return
            self._motion_unknown = None  # the driver answered: the gantry was at rest
            self._mounts[mount].position = {
                "valid": "current",
                "reason": None,
                "xyz_mm": {k: float(xyz[k]) for k in ("x", "y", "z")},
                "source": "driver_step_count",
                "reference_point": {"horizontal": "primary_nozzle", "nozzle": "A1", "vertical": "unknown"},
                "acquired_at": observed_at,
                "effective_at": None,
                "effective_at_clock": None,
                "move_id": ticket.move_id,
                "command_id": None,
                **self._token_fields(mount),
            }

    # -- clock (§4.7) ------------------------------------------------------------

    def _clock_sample(
        self,
        sent: datetime,
        received: datetime,
        started: datetime,
        completed: datetime,
        command_id: Optional[str],
    ) -> None:
        low = (completed - received).total_seconds()
        high = (started - sent).total_seconds()
        self._clock_samples.append((received, low, high, command_id or ""))
        while len(self._clock_samples) > _CLOCK_MAX_SAMPLES:
            self._clock_samples.popleft()

    def _clock_offset(self, now: datetime) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        while self._clock_samples and (now - self._clock_samples[0][0]).total_seconds() > _CLOCK_WINDOW_S:
            self._clock_samples.popleft()
        if not self._clock_samples:
            return None, self._clock_reason
        resolution = 1e-6
        latest = self._clock_samples[-1]
        lows, highs = [], []
        # Widened to the newest sample's time, not to "now": the published
        # interval holds at measured_at, and drift_ppm lets a client widen it
        # for age itself, so repeated reads of unchanged state stay identical.
        for measured, low, high, _ in self._clock_samples:
            drift = (latest[0] - measured).total_seconds() * _CLOCK_DRIFT_PPM * 1e-6
            lows.append(low - drift - resolution)
            highs.append(high + drift + resolution)
        low, high = max(lows), min(highs)
        if low > high:
            # A clock jump: the bounds contradict each other. Restart from the
            # newest sample rather than publish an impossible interval.
            self._clock_samples.clear()
            self._clock_samples.append(latest)
            return None, "inconsistent_bounds_reset"
        return (
            {
                "min": round(low, 6),
                "max": round(high, 6),
                "width": round(high - low, 6),
                "samples": len(lows),
                "from_command": latest[3] or None,
                "measured_at": latest[0],
                "resolution_s": resolution,
                "drift_ppm": _CLOCK_DRIFT_PPM,
            },
            None,
        )

    # -- snapshot ------------------------------------------------------------------

    def snapshot(self, context: "ServiceContext", *, expect: Optional[Tuple[Any, ...]] = None) -> Optional[ObservationResponse]:
        """Build the response. ``context`` is gathered by the service beforehand,
        outside this lock. ``observation_seq`` advances only when the content
        (everything but ``response_generated_at``) differs from the last one."""
        now = self._clock()
        with self._lock:
            if expect is not None and expect != (
                *self._tokens(), id(self._last_command), id(self._in_flight), self._degraded
            ):
                return None  # changed while the service gathered context; retry
            offset, offset_reason = self._clock_offset(now)
            body: Dict[str, Any] = {
                "equipment_id": context.equipment_id,
                "boot_id": self.boot_id,
                "validity_generation": self.validity_generation,
                "qualification": self.qualification,
                "qualification_basis": self.qualification_basis,
                "degraded": self._degraded,
                "event_watermark": None,
                "clock": {"robot_offset_s": offset, "robot_offset_reason": offset_reason},
                "frame": {},
                "calibration": context.calibration,
                "robot": {
                    "reachable": context.reachable,
                    "last_seen_at": context.last_seen_at,
                    "transport": context.transport,
                    "session_generation": self.session_generation,
                    "robot_run_id": self._robot_run_id,
                },
                "motion": self._motion(context),
                "mounts": {m: self._mount_body(m, context) for m in MOUNTS},
                "inventory": context.inventory,
                "run": {**(context.run or {}), "robot_run_id": self._robot_run_id},
            }
        # State dicts are replaced, never mutated in place, so this copy is
        # stable; hashing happens off the state lock the command hooks use.
        with self._publish_lock:
            content = digest(body)
            if content != self._last_digest:
                self._seq += 1
                self._last_digest = content
            body["observation_seq"] = self._seq
        body["response_generated_at"] = now
        return ObservationResponse.model_validate(body)

    def _motion(self, context: "ServiceContext") -> Dict[str, Any]:
        in_flight = self._in_flight
        if self._motion_unknown or context.motion_unknown:
            state, reason = "motion_unknown", self._motion_unknown or context.motion_unknown
        elif in_flight is not None:
            state, reason = "in_flight", None
        elif context.external_run:
            state, reason = "external_run", None
        elif context.paused:
            state, reason = "paused", None
        else:
            state, reason = "idle", None
        flight = None
        if in_flight is not None:
            flight = {
                "move_id": in_flight.move_id,
                "command_type": in_flight.command_type,
                "mount": in_flight.mount,
                "submitted_at": in_flight.submitted_at,
                "plan": in_flight.plan,
                "target": _target(in_flight),
            }
        return {
            "state": state,
            "reason": reason,
            "epoch": self.epoch,
            "in_flight": flight,
            "last_command": self._last_command,
        }

    def _mount_body(self, mount: str, context: "ServiceContext") -> Optional[Dict[str, Any]]:
        instrument = context.instruments.get(mount)
        if instrument is None:
            return None
        state = self._mounts[mount]
        loaded = any(p.get("mount") == mount for p in self._pipettes.values())
        position = dict(state.position)
        if context.stopping and position.get("valid") in ("current", "superseded"):
            # A stop is under way; the session is invalidated once it finishes.
            position.update(valid="superseded", reason="stop_in_progress", superseded_by_move_id=None)
        return {
            "instrument": {**instrument, "loaded_in_session": loaded if self.session_generation else None},
            "position": position,
            "tip": {
                "ledger": context.tip_ledger.get(mount) or {"has_tip": None},
                "engine": context.tip_engine.get(mount) or {"has_tip": None},
                "tip_length": state.tip_length,
            },
        }


@dataclass
class ServiceContext:
    """What the service contributes to a snapshot, gathered outside the cache lock."""

    equipment_id: str
    transport: str
    reachable: Optional[bool]
    last_seen_at: Optional[datetime]
    # mount -> instrument body; a mount absent from this dict is reported null.
    instruments: Dict[str, Dict[str, Any]]
    tip_ledger: Dict[str, Dict[str, Any]]
    tip_engine: Dict[str, Dict[str, Any]]
    calibration: Dict[str, Any]
    inventory: Dict[str, Any]
    run: Dict[str, Any]
    paused: bool = False
    external_run: bool = False
    motion_unknown: Optional[str] = None
    stopping: bool = False


def _is_xyz(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and all(isinstance(value.get(k), (int, float)) and not isinstance(value.get(k), bool) for k in ("x", "y", "z"))
    )


def _target(ticket: Ticket) -> Optional[Dict[str, Any]]:
    """The requested destination, from the command's own params. Never a position."""
    params = ticket.params
    if isinstance(params.get("coordinates"), dict):
        return {"coordinates": params["coordinates"], "reference": "primary_nozzle"}
    if params.get("labwareId") and params.get("wellName"):
        return {"labware_id": params["labwareId"], "well": params["wellName"]}
    if params.get("addressableAreaName"):
        return {"addressable_area": params["addressableAreaName"]}
    return None
