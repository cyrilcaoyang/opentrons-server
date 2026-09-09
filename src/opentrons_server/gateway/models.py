"""Models for the OT-2 gateway.

The AC equipment-status wire types are **not** defined here: they come from
the shared ``sdl-lab-contract`` package (versioned in lockstep with
``ac-organic-lab/docs/STATUS_SPEC.md``) and are re-exported so every
``from .models import ...`` in this repo keeps working. What stays local is
this gateway's own domain vocabulary — protocol-command request bodies,
plate/tip tracking, and the normalized deck model.

Conformance: this gateway conforms to **lab status spec v1.2**. ``activity``
/ ``activity_since`` are observed from the control plane (a protocol command
in flight, or a robot-server run the gateway deferred to), and
``metrics["cycles_total"]`` counts completed protocol commands so a 60 s
poller can still account for work it slept through (§2.3.1). See the README
for what "primary operation" means for this device.

``PROTOCOL_VERSION`` below is the version THIS device speaks; it deliberately
overrides the package default ("1.0" — the honest reading of a device that
does not state one).
"""

from __future__ import annotations

import keyword

from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional, Union, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .robot_profile import IS_FLEX
from .limits import (
    MAX_PIPETTE_VOLUME_UL,
    MAX_WELL_OFFSET_MM,
    MAX_X_MM,
    MAX_Y_MM,
    MAX_Z_MM,
)

from sdl_lab_contract import (
    Activity,
    ClaimedBy,
    ClaimRejection,
    ClaimRequest,
    ClaimResponse,
    ComponentStatus,
    EquipmentKind,
    EquipmentState,
    EquipmentStatus,
    ErrorInfo,
    ErrorSeverity,
    HealthResponse,
    MetricValue,
    ProbeResponse,
)

PROTOCOL_VERSION = "1.2"

# This gateway's kind, passed on every /status envelope (the shared model
# requires it rather than defaulting, so a device can never mis-declare by
# omission).
EQUIPMENT_KIND: EquipmentKind = "liquid_handler"


# ---------------------------------------------------------------------------
# `last_error.code` taxonomy (STATUS_SPEC best practice #6)
#
# Closed, small, and defined once here so a client can branch on the code and
# offer a targeted recovery hint. It is deliberately NOT per-command: this
# gateway used to emit `f"{name}_failed"` for every one of its ~15 protocol
# commands, an open-ended space nobody could enumerate, let alone branch on —
# and `aspirate_failed` vs `dispense_failed` implies no difference in recovery.
# *Which* command failed stays where it belongs: in `last_error.message`, and
# in the `control_action` audit row the events exporter writes.
#
# Recovery, per code:
#   startup_failed           - the gateway could not reach or initialise the
#                              robot. Fix connectivity, then POST /control/startup.
#   snapshot_failed          - a deck/labware read failed. Non-blocking (severity
#                              `warning`); /status serves the last good snapshot,
#                              whose age is in `details.snapshot_age_s`.
#   command_failed           - the robot rejected or failed a protocol command.
#                              The command did not take effect; safe to retry.
#   command_transport_failed - the link dropped during an *idempotent* command.
#                              Effect is unknown but harmless to repeat.
#   command_unknown_outcome  - the link dropped during a NON-idempotent command
#                              (aspirate/dispense/tips). Whether it happened is
#                              genuinely unknowable; needs operator
#                              reconciliation, never an automatic retry.
# ---------------------------------------------------------------------------

ErrorCode = Literal[
    "startup_failed",
    "snapshot_failed",
    "command_failed",
    "command_transport_failed",
    "command_unknown_outcome",
]

ERROR_CODES: frozenset[str] = frozenset(get_args(ErrorCode))


class GatewayClaimRequest(ClaimRequest):
    """``POST /control/claim`` body — the spec's, plus one local field.

    ``takeover`` asks to supersede a claim **already held by the same owner**:
    the operator's other tab, or the one they just reloaded (a reload drops the
    token, stranding a live claim nobody can heartbeat or release until its TTL
    runs out). Never grants a claim held by a different owner — an agent
    mid-plan or the dashboard is refused as usual, with 409.

    Additive and defaulted, so a spec-shaped body from ``lab-skills`` or the
    dashboard validates unchanged and never takes over by accident. Not part of
    STATUS_SPEC §5; see the README's claim section.
    """

    takeover: bool = False


class CommandResponse(BaseModel):
    ok: bool = True
    message: Optional[str] = None
    state: Optional[str] = None


# References remain compatible with engine UUIDs and deck slots, while
# excluding Python expressions from inputs shared with the SSH transport.
RobotReference = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$")]
WellName = Annotated[str, Field(pattern=r"^[A-Z]+[1-9][0-9]*$")]


class StrictRequest(BaseModel):
    """Base for control request bodies: unknown keys are rejected, not ignored.

    Pydantic's default silently drops unrecognized fields, which turned an
    agent-proposed ``pick_up_tip {"slot": 9, "well": "A1"}`` into a *bare* pick
    — the misnamed arguments simply vanished and something else ran instead of
    a validation error. On a device, an argument that silently disappears is
    the same failure class as a swallowed exception; refuse loudly at the
    boundary so the caller (human or agent) corrects the step.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class StartupRequest(StrictRequest):
    host_alias: Optional[str] = None
    password: str = ""
    simulation: bool = False


class ProtocolSetupRequest(StrictRequest):
    labware: List[Dict[str, Any]] = Field(default_factory=list)
    instruments: List[Dict[str, Any]] = Field(default_factory=list)
    modules: List[Dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def safe_setup_names(self):
        for item in [*self.labware, *self.instruments, *self.modules]:
            name = item.get("nickname")
            if (not isinstance(name, str) or not name.isascii() or not name.isidentifier()
                    or keyword.iskeyword(name) or name in {"protocol", "json", "Point", "Location", "location"}):
                raise ValueError("setup nickname must be a non-reserved Python identifier")
        return self


class WellLocation(StrictRequest):
    """A well, plus an optional offset from its top or bottom in mm.

    With no offset named, the reference is the one the action means: an
    aspirate is 1 mm above the well *bottom* (it has to be in the liquid), a
    dispense is at the well *top*, and a plain move is at the well top. See
    ``service._ASPIRATE_DEFAULT_BOTTOM_MM``.

    The offsets are bounded (see ``limits.MAX_WELL_OFFSET_MM``) because they
    were not, and an unbounded one is how a move ends up hundreds of millimetres
    above a plate — accepted here, refused by the robot mid-motion, with a tip
    on the head and a run stopped in the middle. Real protocols use single-digit
    offsets; the bound is a sanity check on intent, not the machine envelope.
    """

    labware_nickname: RobotReference
    position: WellName
    top: Optional[float] = Field(
        default=None,
        ge=-MAX_WELL_OFFSET_MM,
        le=MAX_WELL_OFFSET_MM,
        description=(
            f"mm above the well top (negative = into the well). Bounded to "
            f"+/-{MAX_WELL_OFFSET_MM:g} mm; typical values are single-digit."
        ),
    )
    bottom: Optional[float] = Field(
        default=None,
        ge=-MAX_WELL_OFFSET_MM,
        le=MAX_WELL_OFFSET_MM,
        description=(
            f"mm above the well bottom. Bounded to +/-{MAX_WELL_OFFSET_MM:g} mm; "
            "typical values are single-digit."
        ),
    )
    center: bool = False

    @model_validator(mode="after")
    def one_reference(self):
        if sum((self.top is not None, self.bottom is not None, self.center)) > 1:
            raise ValueError("provide only one of top, bottom or center")
        return self


class CoordinateLocation(StrictRequest):
    """Absolute deck coordinates in mm (the robot's deck reference frame).

    Bounded by the OT-2's own gantry travel. X/Y come from
    ``opentrons_shared_data``'s robot definition; Z is a conservative constant
    because that definition publishes no Z extent (see ``limits.py``), so
    passing this check is "not obviously impossible", not "reachable".
    """

    x: float = Field(
        ...,
        ge=0.0,
        le=MAX_X_MM,
        description=f"mm in the deck frame, 0 to {MAX_X_MM:g} (gantry X travel).",
    )
    y: float = Field(
        ...,
        ge=0.0,
        le=MAX_Y_MM,
        description=f"mm in the deck frame, 0 to {MAX_Y_MM:g} (gantry Y travel).",
    )
    z: float = Field(
        ...,
        ge=0.0,
        le=MAX_Z_MM,
        description=(
            f"mm above the deck, 0 to {MAX_Z_MM:g}. Conservative upper bound: "
            "the robot definition publishes no Z extent."
        ),
    )


class MoveToRequest(StrictRequest):
    """Move a pipette to a well or to absolute deck coordinates (no liquid).

    Exactly one of ``location`` (well-addressed, same shape as aspirate/dispense)
    or ``coordinates`` (absolute deck frame, mm) must be provided.
    """

    pipette: RobotReference
    location: Optional[WellLocation] = None
    coordinates: Optional[CoordinateLocation] = None
    # Straight-line speed for this move in mm/s; omit for the robot default.
    speed: Optional[float] = Field(default=None, gt=0.0)
    # Move in a straight line instead of the arced safe path. The caller owns
    # collision avoidance when set.
    force_direct: bool = Field(default=False, description="False uses the robot's arced path. True requests a straight move without a Z-retract waypoint. To hold Z, use the current Z as the destination.")
    # Minimum Z height (mm) for the arced path.
    minimum_z_height: Optional[float] = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "MoveToRequest":
        if (self.location is None) == (self.coordinates is None):
            raise ValueError("provide exactly one of 'location' or 'coordinates'")
        return self


class LiquidMoveRequest(StrictRequest):
    pipette: RobotReference
    # Bounded by the largest OT-2 pipette. The tighter, *live* bound — what THIS
    # pipette actually holds — cannot be a schema constant, so it is checked
    # pre-motion and refused with 412 (`limits.check_volume`). Unbounded, a
    # negative or 5000 uL aspirate reached the robot untouched.
    volume_ul: float = Field(
        ...,
        gt=0.0,
        le=MAX_PIPETTE_VOLUME_UL,
        description=(
            f"Volume in uL, up to {MAX_PIPETTE_VOLUME_UL:g} (largest OT-2 "
            "pipette). The attached pipette's own min/max is enforced "
            "separately and is published on details.pipette_volumes."
        ),
    )
    location: WellLocation
    # Flow rate in µL/s. Optional: omit to use the transport's default (the
    # pipette's protocol-API default on SSH; the OT2_HTTP_*_FLOW_UL_S env default
    # on the run-engine HTTP transport, which has no implicit pipette default).
    flow_rate: Optional[float] = Field(default=None, gt=0.0)


class DispenseRequest(LiquidMoveRequest):
    push_out: Optional[float] = Field(
        default=None, ge=0, le=MAX_PIPETTE_VOLUME_UL,
        description="Additional plunger air volume in uL; omit for robot default. Separate from blow-out.",
    )


class TipRequest(StrictRequest):
    pipette: RobotReference
    labware_nickname: Optional[RobotReference] = None
    position: Optional[WellName] = None
    # Tip-tracking fields. Omitting `position` on a tracked rack auto-picks the
    # next available tip; omitting `labware_nickname` too auto-selects a
    # tracked, tip-size-compatible rack (slot order). An untracked rack still
    # needs an explicit `position`. `sample_id` allows same-sample tip reuse;
    # `force` overrides the contamination guard (never an empty well).
    sample_id: Optional[str] = None
    force: bool = False


class TipsResetRequest(StrictRequest):
    """(Re)register a tip rack with every tip fresh — a physical rack swap.

    Addressed by deck ``slot``, which is a tip rack's identity: a rack carries
    no sample and no history worth naming, and what an operator refills is "the
    rack in slot 4". ``nickname`` is accepted as a legacy alias and resolved
    through the session recipe when one exists.
    """

    slot: Optional[str] = Field(default=None, min_length=1)
    nickname: Optional[str] = Field(default=None, min_length=1)
    wells: Optional[List[str]] = None  # defaults to the 96-tip column-major grid

    @property
    def target(self) -> str:
        ref = self.slot or self.nickname
        if not ref:
            raise ValueError("tips/reset needs a slot")
        return ref

    @model_validator(mode="after")
    def _needs_a_target(self) -> "TipsResetRequest":
        if not (self.slot or self.nickname):
            raise ValueError("provide slot (preferred) or nickname")
        return self


class TipsMarkRequest(StrictRequest):
    """Correct *part* of a tracked rack: assert which tips are actually there.

    ``tips.reset`` is all-or-nothing, so the only way to fix a wrong count used
    to be to claim a full rack. That is a lie whenever the rack is partly used,
    and a lie that sends the head onto bare holes. This sets the named wells (or
    whole ``columns``, which is how a rack is consumed and how the panel
    addresses it) and leaves every other well untouched.

    **This is the repair tool for a tracker that has drifted from the bench**,
    and it is plannable: an assistant should propose it whenever the recorded
    state and the physical rack disagree. Proposing is not asserting — a plan is
    a draft until a human approves it, and approving is the operator making the
    claim about what they can see. Refusing to propose a correction only leaves
    the drift in place.

    ``status`` is deliberately only ``new`` or ``empty``: presence is something
    a human can look at and confirm. A *touched* tip carries the sample id it
    contacted — evidence the gateway recorded during a real aspirate — which
    nobody can confirm by eye, so it is not offered here.
    """

    slot: Optional[str] = Field(default=None, min_length=1)
    nickname: Optional[str] = Field(default=None, min_length=1)
    wells: Optional[List[str]] = None
    columns: Optional[List[int]] = None
    status: Literal["new", "empty"]

    @property
    def target(self) -> str:
        ref = self.slot or self.nickname
        if not ref:
            raise ValueError("tips/mark needs a slot")
        return ref

    @model_validator(mode="after")
    def _needs_a_target_and_wells(self) -> "TipsMarkRequest":
        if not (self.slot or self.nickname):
            raise ValueError("provide slot (preferred) or nickname")
        if bool(self.wells) == bool(self.columns):
            # Both would be ambiguous about precedence; neither would silently
            # mark nothing and report success.
            raise ValueError("provide exactly one of wells or columns")
        for column in self.columns or []:
            if not 1 <= column <= 12:
                raise ValueError(f"column {column} is outside 1..12")
        return self


class TipRackState(BaseModel):
    """Tracked tip statuses for one rack.

    well -> ``"new"`` | ``"empty"`` | ``"on_pipette"`` | sample id. ``on_pipette``
    is a *hole* like ``empty`` — the tip left the rack on a head — but a hole the
    tip can still come back to, so the two are not interchangeable. The tip's own
    history while it is off the rack lives on the :class:`TipMount`.
    """

    nickname: str
    tips: Dict[str, str]
    registered_at: datetime


class TipMount(BaseModel):
    """The tips a pipette is holding right now — custody, origin, and exposure.

    The counterpart to :class:`TipRackState`: a rack answers "what is in this
    well", a mount answers "where did the tip on this head come from, and what
    has it touched". Persisted alongside the racks because a tip physically
    stays on the head across a gateway restart; losing this record used to
    strand the tip, since the origin well no longer read as free and nothing
    recalled the tip's exposure.

    ``contacted_liquid`` is the coarse question an operator actually asks — may
    this tip be re-seated in a rack, or has it been in a sample? It is set by a
    real aspirate/dispense, never inferred, and never cleared while the tip is
    on the head.
    """

    pipette: str
    # None when the tip's origin is not tracked (picked from an unregistered
    # rack, or already on the head when the gateway first saw it).
    rack: Optional[str] = None
    well: Optional[str] = None  # the addressed origin well
    wells: List[str] = Field(default_factory=list)  # the covered span
    channels: int = 1
    # What the origin well read before the pick, so a rolled-back pick or a
    # return-to-origin restores the tip's history instead of inventing "new".
    origin_status: Optional[str] = None
    last_sample: Optional[str] = None
    contacted_liquid: bool = False
    picked_at: datetime
    # The pick or drop ended without a definite answer (transport loss
    # mid-command). The tip is *assumed* on the head, because assuming
    # otherwise sends the next pick onto a well that may be a bare hole.
    uncertain: bool = False


class GripperOffset(StrictRequest):
    x: float = Field(default=0, ge=-MAX_WELL_OFFSET_MM, le=MAX_WELL_OFFSET_MM)
    y: float = Field(default=0, ge=-MAX_WELL_OFFSET_MM, le=MAX_WELL_OFFSET_MM)
    z: float = Field(default=0, ge=-MAX_WELL_OFFSET_MM, le=MAX_WELL_OFFSET_MM)


class MoveLabwareRequest(StrictRequest):
    labware_nickname: RobotReference
    new_location: RobotReference = Field(min_length=1, description="Deck slot, OFF_DECK, or a loaded module/adapter nickname on Flex.")
    use_gripper: bool = False
    pick_up_offset: Optional[GripperOffset] = None
    drop_offset: Optional[GripperOffset] = None

    @model_validator(mode="after")
    def gripper_options(self):
        if self.use_gripper and not IS_FLEX:
            raise ValueError("gripper moves require a Flex gateway profile")
        if not self.use_gripper and (self.pick_up_offset is not None or self.drop_offset is not None):
            raise ValueError("gripper offsets require use_gripper=true")
        return self


class LightsRequest(StrictRequest):
    on: bool


class TempmodSetRequest(StrictRequest):
    """Set the temperature-module target and return; the block ramps after.

    Waiting for the target would hold the gateway BUSY for the whole ramp
    (minutes, to 4 °C) and blow the HTTP command timeout. ``/status`` already
    reports current vs target; the operator watches that.

    ``module`` is the setup-recipe nickname or the deck slot. Omit it when
    exactly one temperature module is declared or loaded.
    """

    celsius: float = Field(
        ...,
        ge=4.0,
        le=95.0,
        description="Target in °C. Temperature module GEN2 range is 4–95.",
    )
    module: Optional[RobotReference] = Field(
        default=None,
        description=(
            'Recipe nickname or deck slot (e.g. "7"). Omit when exactly one '
            "temperature module is on the deck."
        ),
    )


class TempmodDeactivateRequest(StrictRequest):
    """Turn the temperature module off."""

    module: Optional[RobotReference] = Field(
        default=None,
        description=(
            'Recipe nickname or deck slot (e.g. "7"). Omit when exactly one '
            "temperature module is on the deck."
        ),
    )


# ---------------------------------------------------------------------------
# Per-well sample / plate tracking
#
# Mirrors the agilent-cytation-server contract (WellSample / LoadedPlate) so a
# plate's orchestrator-owned state round-trips cleanly across devices: the
# workflow hydrates each device on plate.load with the previous device's
# `wells`, and reads `details.loaded_plate` back from /status. Ownership split:
# the orchestrator owns `plate_id` / `sample_id` / `notes`; the device owns
# `volume_ul` and `loaded_at`. See agilent-cytation-server/docs/PLATE_STATE.md.
# ---------------------------------------------------------------------------


WellId = str  # "A1" .. "H12"


class WellSample(BaseModel):
    """One well of the currently-loaded plate."""

    well: WellId
    sample_id: Optional[str] = None
    volume_ul: Optional[float] = Field(default=None, ge=0.0)
    notes: Optional[str] = None


class LoadedPlate(BaseModel):
    """The plate the orchestrator currently considers loaded on the deck.

    ``model`` is a free-form labware identifier (typically an Opentrons
    ``load_name`` such as ``corning_96_wellplate_360ul_flat``). ``plate_id``
    is an orchestrator-assigned identifier (typically a barcode or
    run-prefixed UUID). Surfaced under ``EquipmentStatus.details.loaded_plate``.
    """

    plate_id: str
    model: str
    loaded_at: datetime
    wells: List[WellSample] = Field(default_factory=list)


class PlateLoadRequest(StrictRequest):
    plate_id: str = Field(..., min_length=1, max_length=128)
    model: str = Field(..., min_length=1)
    wells: Optional[List[WellSample]] = None  # defaults to 96 empty wells


# ---------------------------------------------------------------------------
# Deck / labware state (Phase 0: model only — normalizers + merge live in
# gateway/deck.py; wiring into /status is Phase 1+). See docs/DECK_STATE.md.
#
# The normalized deck is the single, provenance-tagged view of "what is on each
# of the 12 OT-2 slots", merged from up to three sources (run > repl > declared).
# `kind` is derived, never hand-set; the tile renders any grid off rows/columns.
# ---------------------------------------------------------------------------


LabwareKind = Literal[
    "96-well",
    "384-well",
    "48-well",
    "24-well",
    "12-well",
    "6-well",
    "well_plate",  # a wellPlate whose grid isn't one of the named sizes
    "tiprack",
    "reservoir",
    "tuberack",
    "trash",
    "adapter",
    "unknown",
]

# Per-slot lifecycle. See docs/DECK_STATE.md (slot-state decision table) for the decision table.
SlotState = Literal["empty", "declared", "occupied", "in_use", "mismatch"]

# Which source won a slot / the deck as a whole.
DeckSource = Literal["run", "repl", "declared", "empty"]


class SlotLabware(BaseModel):
    """Normalized labware on one deck slot.

    ``plate_id`` / ``wells`` are populated only for the orchestrator-tracked
    plate (unified from :class:`PlateStateStore`), never stored twice.

    ``nickname`` is the name the setup recipe gave this slot's labware — the key
    every nickname-addressed surface uses (``details.tip_racks``, the
    ``/control/*`` ``labware_nickname`` argument). It is a property of the
    *slot*, so :func:`~opentrons_server.gateway.deck.build_deck` stamps it
    regardless of which source won the slot; ``display_name`` cannot stand in
    for it, because a run/REPL source overwrites that with the robot's own
    display name.
    """

    kind: LabwareKind
    load_name: str
    display_name: Optional[str] = None
    is_tiprack: bool = False
    rows: Optional[int] = None
    columns: Optional[int] = None
    plate_id: Optional[str] = None
    wells: Optional[List[WellSample]] = None
    nickname: Optional[str] = None
    # Full Opentrons schema-2 definition, when the declaration carried one
    # (e.g. a custom labware built in the dashboard store). The gateway has
    # no local copy of custom definitions, so auto-loading a declared custom
    # slot must send this to `load_labware` as `ot_default: False` — the
    # default path looks it up in the opentrons namespace and 404s.
    definition: Optional[Dict[str, Any]] = None


class SlotModule(BaseModel):
    """A hardware module occupying a slot (temperature, magnetic, heater-shaker)."""

    module_name: str
    status: Optional[str] = None
    serial_number: Optional[str] = None


class DeckSlot(BaseModel):
    labware: Optional[SlotLabware] = None
    module: Optional[SlotModule] = None
    slot_state: SlotState = "empty"
    source: DeckSource = "empty"
    # The operator/recipe declaration standing on this slot, whatever won the
    # merge. Populated for *every* declared slot, not just a mismatch: once a
    # run/REPL source occupies a declared slot, `slot_state` stops saying
    # "declared", and a caller that reconstructs the layout from `slot_state`
    # alone would silently drop the declaration on its next full-layout
    # replace. The orchestrator-tracked plate is deliberately excluded — it is
    # folded onto its slot for its wells, not operator intent.
    declared: Optional[SlotLabware] = None
    # As `declared`, for a slot whose declaration is a sticky module rather than
    # labware. Movable modules are never declared, so this is what tells a
    # declared module apart from one that merely showed up in the live deck.
    declared_module: Optional[SlotModule] = None


class DeckState(BaseModel):
    source: DeckSource
    slots: Dict[str, DeckSlot]  # always all of "1".."12"
    timestamp: datetime


class DeckDeclareRequest(StrictRequest):
    """Operator/recipe-declared layout (Phase 2 endpoint payload).

    Each slot value is a labware ``load_name`` (preferred, full fidelity), a
    bare ``kind`` string (legacy dashboard compat), a **module** (a module-kind
    string like ``"temperature_module"`` or a dict with ``module_name``), or
    ``null`` to clear that slot. An empty ``slots`` map clears the whole
    declaration. Declared modules are sticky fixtures; movable modules flow
    through the live run instead.

    A labware slot may also be given as ``{"load_name": ..., "definition":
    {...}}``, where ``definition`` is a full Opentrons schema-2 labware
    definition. When present, the gateway derives ``kind``/``rows``/
    ``columns``/``is_tiprack`` from the real definition (via ``ordering`` and
    ``parameters``) instead of guessing from ``load_name`` alone — this is how
    a custom labware the gateway has no local copy of (e.g. one built in the
    dashboard's labware library) still resolves to real geometry rather than
    ``kind: "unknown"``.
    """

    slots: Dict[str, Optional[Union[str, Dict[str, Any]]]] = Field(default_factory=dict)


class WellUpdateRequest(StrictRequest):
    well: WellId = Field(..., min_length=2, max_length=3, description="e.g. A1, H12")
    sample_id: Optional[str] = None
    volume_ul: Optional[float] = Field(default=None, ge=0.0)
    notes: Optional[str] = None
    clear_sample_id: bool = False
    clear_notes: bool = False
