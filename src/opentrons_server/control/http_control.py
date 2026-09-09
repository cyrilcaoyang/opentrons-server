"""OT2Control-compatible adapter backed by the run-engine HTTP client.

``OT2HttpControl`` mirrors the **full public method surface** of
:class:`OT2Control` (the SSH REPL transport), so the gateway — and any direct
caller — can treat the two transports interchangeably. It translates the REPL's
stateful, immediate-execution model into run-engine commands via
:class:`RunEngineClient`. Wired into ``gateway/service.py`` behind
``OT2_TRANSPORT=http`` and validated on real hardware (2026-07-14,
``ot2cytation``); the parity surface added afterwards is bench-unverified where
noted in ``docs/HTTP_SSH_PARITY.md``.

Parity model (full method-by-method table in ``docs/HTTP_SSH_PARITY.md``):

- **Native** — one (or two) run-engine commands with the same effect
  (``aspirate``, ``touch_tip``, module verbs, ``comment``, ``delay``, …).
- **Emulated** — composed client-side from run-engine primitives (``mix``,
  ``air_gap``, ``return_tip``, absolute-coordinate liquid handling via
  ``moveToCoordinates`` + ``*InPlace``).
- **Client-tracked** — protocol-API session state the run engine does not hold;
  kept in this adapter (``has_tip``, ``current_volume``, ``set/get_flow_rate``,
  well-bottom clearances, ``set_speed``). Readbacks reflect what *this adapter*
  did, not independent hardware state.
- **Unsupported** — REPL-only or no run-engine equivalent; raises
  ``NotImplementedError`` with the reason (``invoke``, ``set_max_speed``,
  ``set_starting_tip`` / ``reset_tipracks`` — the gateway's own tip tracking
  replaces those two).

Semantic differences vs. the SSH REPL that remain by design:

- **Flow rates.** The run engine *requires* ``flowRate``; the SSH path inherits
  protocol-API defaults. Precedence per call: explicit ``flow_rate`` >
  ``set_flow_rate(pip, ...)`` override > constructor/env default
  (``OT2_HTTP_*_FLOW_UL_S``). The protocol-API ``rate`` multiplier is applied
  on top of whichever base wins.
- **Pending locations are consumed.** ``get_location_from_labware`` /
  ``get_location_absolute`` stash one pending location; the next consuming call
  (aspirate/dispense/blow_out/pick_up/drop/mix) takes it. The SSH REPL's
  ``location`` variable persists instead. ``move_to_pip`` deliberately *peeks*
  (does not consume) so move-then-aspirate works like SSH.
- **Tips.** ``pickUpTip`` needs an explicit labware+well (no protocol-API
  next-tip tracking); the gateway's tip store provides auto-pick above this
  layer. ``drop_tip`` without a location drops into the registered trash —
  ``setup_protocol`` registers the OT-2 fixed trash automatically when the
  recipe leaves slot 12 free (see :meth:`load_trash_bin`) — else in place.
- **Object ids** are client-supplied and equal to the caller's nickname, so
  nicknames must stay unique within a run.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .http_run import (
    OFF_DECK,
    RunEngineClient,
    RunEngineCommands,
    RunEngineError,
    deck_slot,
)

# Default flow rates (µL/s) used when a call omits `flow_rate`. Aspirate lowered
# 150 -> 90 after the 2026-07-14 ot2cytation validation judged 150 slightly fast
# (~90 also aligns with the p300 gen2 factory default). Override per-deployment
# via the env vars, per-pipette via set_flow_rate(), or per-call via
# LiquidMoveRequest.flow_rate.
_DEFAULT_ASPIRATE_FLOW = float(os.getenv("OT2_HTTP_ASPIRATE_FLOW_UL_S", "90"))
_DEFAULT_DISPENSE_FLOW = float(os.getenv("OT2_HTTP_DISPENSE_FLOW_UL_S", "300"))
_DEFAULT_BLOWOUT_FLOW = float(os.getenv("OT2_HTTP_BLOWOUT_FLOW_UL_S", "100"))

_OFF_DECK_ALIASES = {"OFF_DECK", "offDeck", "off_deck", OFF_DECK}

# The OT-2's fixed trash. Older robot-servers model it as a normal labware
# definition in slot 12; modern ones model it as an addressable AREA — slot 12
# is then not loadable at all (AreaNotInDeckConfigurationError, observed live
# on ot2_complexation 2026-08-11, robot-server leaving live runs empty).
_OT2_FIXED_TRASH_LOADNAME = "opentrons_1_trash_1100ml_fixed"
_OT2_FIXED_TRASH_SLOT = "12"
_OT2_FIXED_TRASH_AREA = "fixedTrash"

# Motor axes for the run-engine `home` command, keyed by mount.
_MOUNT_Z_AXIS = {"left": "leftZ", "right": "rightZ"}
_MOUNT_PLUNGER_AXIS = {"left": "leftPlunger", "right": "rightPlunger"}

# Protocol-API air_gap default: 5 mm above the well top.
_AIR_GAP_DEFAULT_HEIGHT_MM = 5.0


class OT2HttpControl:
    """Run-engine-backed drop-in for the full ``OT2Control`` method surface."""

    def __init__(
        self,
        client: RunEngineClient,
        *,
        aspirate_flow_rate: float = _DEFAULT_ASPIRATE_FLOW,
        dispense_flow_rate: float = _DEFAULT_DISPENSE_FLOW,
        blow_out_flow_rate: float = _DEFAULT_BLOWOUT_FLOW,
    ) -> None:
        self.client = client
        self.aspirate_flow_rate = aspirate_flow_rate
        self.dispense_flow_rate = dispense_flow_rate
        self.blow_out_flow_rate = blow_out_flow_rate
        # nickname -> engine id (client-supplied == nickname; kept explicit so a
        # future switch to engine-assigned ids is a one-place change).
        self._labware_ids: Dict[str, str] = {}
        self._pipette_ids: Dict[str, str] = {}
        self._pipette_mounts: Dict[str, str] = {}
        self._module_ids: Dict[str, str] = {}
        # nickname -> hardware serial, captured from the loadModule result when
        # present; backs the live get_rpm/get_temp readbacks via GET /modules.
        self._module_serials: Dict[str, Optional[str]] = {}
        # Stateful "current location" set by get_location_from_labware /
        # get_location_absolute, consumed by the next liquid/tip action —
        # mirrors the REPL's `location` variable (but single-shot; see module
        # docstring). kind: "well" | "coordinates".
        self._pending: Optional[Dict[str, Any]] = None
        # pip -> last well-location acted on (feeds air_gap's "current well").
        self._last_well: Dict[str, Dict[str, Any]] = {}
        # pip -> tip-origin well-location (set on pick_up_tip; feeds return_tip
        # and the client-tracked has_tip readback).
        self._tip_origin: Dict[str, Dict[str, Any]] = {}
        # pip -> aspirated-volume ledger (client-tracked current_volume).
        self._volumes: Dict[str, float] = {}
        # pip -> {"aspirate","dispense","blow_out"} overrides (set_flow_rate).
        self._flow_rates: Dict[str, Dict[str, float]] = {}
        # pip -> {"aspirate","dispense"} stored clearances. NOTE: inert through
        # this wrapper (as on the SSH path — see HTTP_SSH_PARITY.md).
        self._clearances: Dict[str, Dict[str, float]] = {}
        # pip -> default gantry speed (mm/s); applied to move_to_pip only.
        self._default_speeds: Dict[str, float] = {}
        # Registered trash: a labware nickname (older servers / preloaded runs)
        # or an addressable-area name (modern servers, where the fixed trash is
        # not labware). Default drop_tip target when either is set.
        self._trash_nickname: Optional[str] = None
        self._trash_area: Optional[str] = None

    # -- session lifecycle -------------------------------------------------

    def initialize_protocol(self, simulation: bool = False) -> None:
        """Create the run. ``simulation`` is decided by the target robot-server,
        not here, so the flag is accepted for signature parity and ignored."""
        self.client.create_run()

    def shutdown(self) -> None:
        self.client.stop_run()
        self.client.close()

    def close_session(self) -> None:
        """Home the gantry then end the run — mirrors ``OT2Control.close_session``."""
        self.home()
        self.shutdown()

    def invoke(self, code: str) -> str:
        raise NotImplementedError(
            "invoke() executes Python in the robot-side REPL and has no HTTP "
            "equivalent; the run engine only accepts typed commands"
        )

    # -- setup / loading ---------------------------------------------------

    def setup_protocol(
        self,
        *,
        labware: Optional[List[Dict[str, Any]]] = None,
        instruments: Optional[List[Dict[str, Any]]] = None,
        modules: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        trash_from_recipe = False
        for labware_config in labware or []:
            if self._is_fixed_trash_labware(labware_config):
                # The recipe names the OT-2 fixed trash in slot 12. On a modern
                # robot-server slot 12 is an addressable AREA, not loadable
                # labware, so sending this through load_labware dies with
                # AreaNotInDeckConfigurationError and takes the whole setup down
                # (observed live 2026-08-11, and the reason setup #1 half-loaded
                # the run). load_trash_bin handles both the preloaded-labware and
                # addressable-area routes; route the entry there instead.
                self.load_trash_bin()
                trash_from_recipe = True
            else:
                self.load_labware(labware_config)
        for instrument_config in instruments or []:
            self.load_instrument(instrument_config)
        for module_config in modules or []:
            self.load_module(module_config)
        # The OT-2's fixed trash is always physically present, and on the SSH
        # path drop_tip auto-routes to it. The run engine has no implicit
        # trash, so without this a bare drop_tip fell through to
        # dropTipInPlace — the tip landed wherever the pipette happened to be.
        # Registered here (not per-drop) so drop_tip's own precedence
        # (pending location > trash labware > trash area > in place) stays a
        # pure lookup. Skipped if a recipe deliberately occupies slot 12 with
        # *non-trash* labware, if the recipe's own fixed-trash entry was already
        # routed above, or when a registration (startup-time or an earlier
        # setup) already happened.
        occupied = {
            str(cfg.get("location"))
            for cfg in [*(labware or []), *(modules or [])]
            if not self._is_fixed_trash_labware(cfg)
        }
        if (
            not trash_from_recipe
            and self._trash_nickname is None
            and self._trash_area is None
            and _OT2_FIXED_TRASH_SLOT not in occupied
        ):
            self.load_trash_bin()
        # Success path: the local id maps now match the run. Adopt it explicitly
        # so the invariant "maps == run" holds even if a load registered an id
        # under a name the cache did not record (see adopt_run_state).
        self.adopt_run_state()

    @staticmethod
    def _is_fixed_trash_labware(cfg: Dict[str, Any]) -> bool:
        """True for a recipe entry naming the OT-2 fixed trash in slot 12.

        Identified by slot *and* load name so a genuine non-trash labware in
        slot 12 (e.g. a reservoir) still loads normally: only the fixed-trash
        family (``opentrons_1_trash_*ml_fixed``, any capacity) reroutes."""
        if str(cfg.get("location")) != _OT2_FIXED_TRASH_SLOT:
            return False
        name = str(cfg.get("loadname") or cfg.get("load_name") or "")
        return "trash" in name and "fixed" in name

    def load_labware(self, labware: Dict[str, Any]) -> str:
        nickname = labware["nickname"]
        location = self._location(labware["location"])
        if labware["ot_default"]:
            load_name = labware["loadname"]
            namespace = labware.get("namespace", "opentrons")
            version = int(labware.get("version", 1))
        else:
            definition = labware["config"]
            self.client.add_labware_definition(definition)
            params = definition["parameters"]
            load_name = params["loadName"]
            namespace = definition["namespace"]
            version = int(definition["version"])
        self.client.execute(
            RunEngineCommands.load_labware(
                load_name, namespace, version, location, labware_id=nickname
            )
        )
        self._labware_ids[nickname] = nickname
        return nickname

    def load_instrument(self, instrument: Dict[str, Any]) -> str:
        if not instrument.get("ot_default", True):
            raise NotImplementedError("custom instrument not implemented")
        nickname = instrument["nickname"]
        mount = instrument["mount"]
        self.client.execute(
            RunEngineCommands.load_pipette(
                instrument["instrument_name"], mount, pipette_id=nickname
            )
        )
        self._pipette_ids[nickname] = nickname
        self._pipette_mounts[nickname] = mount
        return nickname

    def load_module(self, module: Dict[str, Any]) -> str:
        """Load a module and (matching ``OT2Control``) its adapter when the
        config carries one. Adapters are labware in the run engine: they load
        via ``loadLabware`` onto the module and register as
        ``<nickname>_adapter`` for later on-adapter placements."""
        nickname = module["nickname"]
        result = self.client.execute(
            RunEngineCommands.load_module(
                module["module_name"], self._location(module["location"]), module_id=nickname
            )
        )
        self._module_ids[nickname] = nickname
        serial = None
        if isinstance(result, dict):
            serial = (result.get("result") or {}).get("serialNumber")
        self._module_serials[nickname] = serial
        adapter = module.get("adapter")
        if adapter:
            adapter_id = f"{nickname}_adapter"
            self.client.execute(
                RunEngineCommands.load_labware(
                    adapter, "opentrons", 1, {"moduleId": nickname}, labware_id=adapter_id
                )
            )
            self._labware_ids[adapter_id] = adapter_id
        return nickname

    def load_trash_bin(
        self,
        nickname: str = "default_trash",
        location: str = _OT2_FIXED_TRASH_SLOT,
    ) -> str:
        """Register the OT-2 fixed trash as the default ``drop_tip`` target.

        On the SSH path this method is Flex-only (``protocol.load_trash_bin``);
        the OT-2's fixed trash is implicit in the protocol API. Here, one of
        two routes, decided by what the robot-server actually offers:

        * A run that **preloads** the fixed trash as labware (some server
          versions do) — adopt that labware.
        * Otherwise the trash is the ``fixedTrash`` **addressable area** —
          slot 12 is not loadable at all on such servers
          (``AreaNotInDeckConfigurationError``, observed live 2026-08-11, and
          the reason the first shipped version of this method took the whole
          session down). Drops then use ``moveToAddressableAreaForDropTip`` +
          ``dropTipInPlace``, exactly as protocol API ≥ 2.16 does. No labware
          is loaded, so nothing can fail at registration time.

        ``location`` is kept for signature parity; the area route ignores it.
        """
        run = self.client.get_run() or {}
        for lw in run.get("labware") or []:
            if lw.get("id") == "fixedTrash" or lw.get("loadName") == _OT2_FIXED_TRASH_LOADNAME:
                self._labware_ids[nickname] = lw.get("id")
                self._trash_nickname = nickname
                return nickname
        self._trash_area = _OT2_FIXED_TRASH_AREA
        return nickname

    def remove_labware(self, labware_nickname: str) -> None:
        """Clear a deck slot by moving the labware off-deck (the run engine's
        equivalent of the REPL's ``del protocol.deck[slot]``). The labware id
        stays registered, so it can be moved back on-deck later."""
        self.client.execute(
            RunEngineCommands.move_labware(self._labware_id(labware_nickname), OFF_DECK)
        )

    # -- protocol-level controls --------------------------------------------

    def home(self) -> None:
        self.client.execute(RunEngineCommands.home())

    def comment(self, message: str) -> None:
        self.client.execute(RunEngineCommands.comment(message))

    def delay(self, seconds: float = 0, minutes: float = 0) -> None:
        self.client.execute(
            RunEngineCommands.wait_for_duration(float(seconds) + float(minutes) * 60.0)
        )

    def set_rail_lights(self, on: bool = True) -> None:
        self.client.set_lights(bool(on))

    def get_rail_lights(self) -> bool:
        return self.client.get_lights()

    def set_max_speed(self, axis: str, speed: float) -> None:
        raise NotImplementedError(
            "protocol.max_speeds (per-axis caps) has no run-engine equivalent; "
            "use set_speed()/move_to_pip(speed=...) for per-move speeds"
        )

    def clear_max_speed(self, axis: str) -> None:
        raise NotImplementedError(
            "protocol.max_speeds (per-axis caps) has no run-engine equivalent"
        )

    def pause(self) -> None:
        """No-op: in the never-played setup model each command already blocks to
        completion, so there is no run queue to pause (see HTTP_TRANSPORT.md)."""

    def resume(self) -> None:
        """No-op counterpart to :meth:`pause`."""

    # -- labware geometry readbacks -------------------------------------------

    def well_diameter(self, labware_nickname: str, position: str) -> float:
        well = self._well_definition(labware_nickname, position)
        diameter = well.get("diameter")
        if diameter is None:
            raise RunEngineError(
                f"well {position!r} of {labware_nickname!r} has no diameter "
                "(rectangular wells define xDimension/yDimension instead)"
            )
        return float(diameter)

    def well_depth(self, labware_nickname: str, position: str) -> float:
        return float(self._well_definition(labware_nickname, position)["depth"])

    def tip_length(self, labware_nickname: str, position: str) -> Optional[float]:
        """Tip length for tip racks, ``None`` for anything else — mirrors the
        SSH readback of ``well.length``. Uniform per rack in the definition."""
        definition = self._labware_definition(labware_nickname)
        parameters = definition.get("parameters") or {}
        if not parameters.get("isTiprack"):
            return None
        return float(parameters["tipLength"])

    # -- locations -----------------------------------------------------------

    def get_location_from_labware(
        self,
        labware_nickname: str,
        position: str,
        top: float = 0,
        bottom: float = 0,
        center: float = 0,
        default_origin: str = "top",
        default_offset: float = 0,
    ) -> None:
        """Stash the pending well location, mirroring OT2Control's precedence
        (top, then bottom, then center, else ``default_origin`` at
        ``default_offset`` — which the calling action chooses)."""
        if top:
            origin, z = "top", top
        elif bottom:
            origin, z = "bottom", bottom
        elif center:
            origin, z = "center", 0.0
        elif default_origin == "center":
            origin, z = "center", 0.0
        else:
            origin, z = default_origin, float(default_offset)
        self._pending = {
            "kind": "well",
            "labware_id": self._labware_id(labware_nickname),
            "well_name": position,
            "origin": origin,
            "offset": {"x": 0, "y": 0, "z": z},
        }

    def get_location_absolute(
        self, x: float, y: float, z: float, reference: str = None
    ) -> None:
        """Stash a pending absolute deck coordinate. ``reference`` is accepted
        for signature parity; like the SSH path's ``Location`` label it does
        not affect motion targeting."""
        self._pending = {
            "kind": "coordinates",
            "coordinates": {"x": float(x), "y": float(y), "z": float(z)},
        }

    def move_to_pip(
        self,
        pip_name: str,
        *,
        speed: Optional[float] = None,
        force_direct: Optional[bool] = None,
        minimum_z_height: Optional[float] = None,
    ) -> None:
        """Move the pipette to the pending location. Peeks (does not consume)
        the pending location, so a following aspirate/dispense targets the same
        spot — matching the REPL's persistent ``location`` variable."""
        if self._pending is None:
            raise RuntimeError(
                "move_to_pip needs a location; call get_location_from_labware or "
                "get_location_absolute first"
            )
        loc = self._pending
        effective_speed = speed if speed is not None else self._default_speeds.get(pip_name)
        pipette_id = self._pipette_id(pip_name)
        if loc["kind"] == "coordinates":
            self.client.execute(
                RunEngineCommands.move_to_coordinates(
                    pipette_id,
                    loc["coordinates"],
                    speed=effective_speed,
                    force_direct=force_direct,
                    minimum_z_height=minimum_z_height,
                )
            )
        else:
            self.client.execute(
                RunEngineCommands.move_to_well(
                    pipette_id,
                    loc["labware_id"],
                    loc["well_name"],
                    origin=loc["origin"],
                    offset=loc["offset"],
                    speed=effective_speed,
                    force_direct=force_direct,
                    minimum_z_height=minimum_z_height,
                )
            )
            self._last_well[pip_name] = loc

    # -- tips --------------------------------------------------------------

    def pick_up_tip(
        self,
        pip_name: str,
        *,
        presses: Optional[int] = None,
        increment: Optional[float] = None,
        prep_after: Optional[bool] = None,
    ) -> None:
        if presses is not None or increment is not None or prep_after is not None:
            raise NotImplementedError(
                "the run engine's pickUpTip has no presses/increment/prep_after "
                "parameters (they are deprecated in the protocol API as well)"
            )
        if self._pending is None:
            raise RuntimeError(
                "pick_up_tip over HTTP needs an explicit tip location; call "
                "get_location_from_labware(tiprack, well) first (the run engine has "
                "no implicit next-tip tracking)"
            )
        loc = self._take_pending("pick_up_tip")
        if loc["kind"] != "well":
            raise RuntimeError("pick_up_tip needs a well location, not coordinates")
        self.client.execute(
            RunEngineCommands.pick_up_tip(
                self._pipette_id(pip_name),
                loc["labware_id"],
                loc["well_name"],
                origin=loc["origin"],
                offset=loc["offset"],
            )
        )
        self._tip_origin[pip_name] = loc
        self._last_well[pip_name] = loc
        self._volumes[pip_name] = 0.0

    def drop_tip(self, pip_name: str, *, home_after: Optional[bool] = None) -> None:
        """Drop the tip. Precedence: pending location > registered trash
        labware > registered trash *area* (see :meth:`load_trash_bin`) > in
        place. The SSH path always auto-routes to the OT-2 fixed trash;
        registering the trash gives the same behavior here."""
        pipette_id = self._pipette_id(pip_name)
        if self._pending is not None:
            loc = self._take_pending("drop_tip")
            if loc["kind"] != "well":
                raise RuntimeError("drop_tip needs a well location, not coordinates")
            self.client.execute(
                RunEngineCommands.drop_tip(
                    pipette_id,
                    loc["labware_id"],
                    loc["well_name"],
                    origin=loc["origin"],
                    offset=loc["offset"],
                    home_after=home_after,
                )
            )
        elif self._trash_nickname is not None:
            self.client.execute(
                RunEngineCommands.drop_tip(
                    pipette_id,
                    self._labware_id(self._trash_nickname),
                    "A1",
                    home_after=home_after,
                )
            )
        elif self._trash_area is not None:
            # Modern servers: the fixed trash is an area, not labware. Two
            # commands — position over the area, then drop in place there.
            self.client.execute(
                RunEngineCommands.move_to_addressable_area_for_drop_tip(
                    pipette_id, self._trash_area
                )
            )
            self.client.execute(
                RunEngineCommands.drop_tip_in_place(pipette_id, home_after=home_after)
            )
        else:
            # Fallback: drop where the pipette is (NOT the trash). Flagged.
            self.client.execute(
                RunEngineCommands.drop_tip_in_place(pipette_id, home_after=home_after)
            )
        self._tip_origin.pop(pip_name, None)
        self._volumes[pip_name] = 0.0

    def return_tip(self, pip_name: str, *, home_after: Optional[bool] = None) -> None:
        """Return the tip to the well it was picked from. Emulated: the run
        engine has no returnTip command, so this drops into the tracked origin
        well (tracked client-side since the pick)."""
        origin = self._tip_origin.get(pip_name)
        if origin is None:
            raise RuntimeError(
                f"return_tip: no tracked tip origin for {pip_name!r} "
                "(no pick_up_tip through this adapter in this session)"
            )
        self.client.execute(
            RunEngineCommands.drop_tip(
                self._pipette_id(pip_name),
                origin["labware_id"],
                origin["well_name"],
                home_after=home_after,
            )
        )
        self._tip_origin.pop(pip_name, None)
        self._volumes[pip_name] = 0.0

    def has_tip(self, pip_name: str) -> bool:
        """Client-tracked: True between a pick_up_tip and a drop/return through
        this adapter. Unlike the SSH readback it cannot see tips acquired by
        other clients or before a gateway restart."""
        self._pipette_id(pip_name)  # raise on unknown pipette, like SSH would
        return pip_name in self._tip_origin

    def set_starting_tip(self, pip_name: str, tiprack_nickname: str, position: str) -> None:
        raise NotImplementedError(
            "the run engine has no protocol-API tip tracking; the gateway's own "
            "tip store handles next-tip selection (see /control/tips/reset and "
            "gateway/tip_state.py)"
        )

    def reset_tipracks(self, pip_name: str) -> None:
        raise NotImplementedError(
            "the run engine has no protocol-API tip tracking; reset racks via "
            "the gateway's POST /control/tips/reset instead"
        )

    # -- liquid handling ---------------------------------------------------

    def prepare_aspirate(self, pip_name: str) -> None:
        self.client.execute(
            RunEngineCommands.prepare_to_aspirate(self._pipette_id(pip_name))
        )

    def aspirate(
        self,
        pip_name: str,
        volume: float,
        *,
        rate: Optional[float] = None,
        flow_rate: Optional[float] = None,
    ) -> None:
        loc = self._take_pending("aspirate")
        flow = self._flow(pip_name, "aspirate", flow_rate, rate)
        pipette_id = self._pipette_id(pip_name)
        if loc["kind"] == "coordinates":
            # Emulated absolute-position aspirate: explicit move, then in-place.
            self.client.execute(
                RunEngineCommands.move_to_coordinates(pipette_id, loc["coordinates"])
            )
            self.client.execute(
                RunEngineCommands.aspirate_in_place(pipette_id, volume, flow)
            )
        else:
            self.client.execute(
                RunEngineCommands.aspirate(
                    pipette_id,
                    loc["labware_id"],
                    loc["well_name"],
                    volume,
                    flow,
                    origin=loc["origin"],
                    offset=loc["offset"],
                )
            )
            self._last_well[pip_name] = loc
        self._volumes[pip_name] = self._volumes.get(pip_name, 0.0) + float(volume)

    def dispense(
        self,
        pip_name: str,
        volume: float,
        push_out: Optional[float] = None,
        *,
        rate: Optional[float] = None,
        flow_rate: Optional[float] = None,
    ) -> None:
        loc = self._take_pending("dispense")
        flow = self._flow(pip_name, "dispense", flow_rate, rate)
        pipette_id = self._pipette_id(pip_name)
        if loc["kind"] == "coordinates":
            self.client.execute(
                RunEngineCommands.move_to_coordinates(pipette_id, loc["coordinates"])
            )
            self.client.execute(
                RunEngineCommands.dispense_in_place(
                    pipette_id, volume, flow, push_out=push_out
                )
            )
        else:
            self.client.execute(
                RunEngineCommands.dispense(
                    pipette_id,
                    loc["labware_id"],
                    loc["well_name"],
                    volume,
                    flow,
                    origin=loc["origin"],
                    offset=loc["offset"],
                    push_out=push_out,
                )
            )
            self._last_well[pip_name] = loc
        self._volumes[pip_name] = max(
            0.0, self._volumes.get(pip_name, 0.0) - float(volume)
        )

    def mix(
        self,
        pip_name: str,
        repetitions: int,
        volume: Optional[float] = None,
        rate: Optional[float] = None,
    ) -> None:
        """Emulated: N × (aspirate + dispense) at the pending well. ``volume``
        is required here — the run engine cannot default to the pipette's max
        working volume the way the protocol API does."""
        if volume is None:
            raise ValueError(
                "mix over HTTP requires an explicit volume (the run engine cannot "
                "default to the pipette's max volume)"
            )
        loc = self._take_pending("mix")
        if loc["kind"] != "well":
            raise RuntimeError("mix needs a well location, not coordinates")
        pipette_id = self._pipette_id(pip_name)
        aspirate_flow = self._flow(pip_name, "aspirate", None, rate)
        dispense_flow = self._flow(pip_name, "dispense", None, rate)
        for _ in range(int(repetitions)):
            self.client.execute(
                RunEngineCommands.aspirate(
                    pipette_id,
                    loc["labware_id"],
                    loc["well_name"],
                    volume,
                    aspirate_flow,
                    origin=loc["origin"],
                    offset=loc["offset"],
                )
            )
            self.client.execute(
                RunEngineCommands.dispense(
                    pipette_id,
                    loc["labware_id"],
                    loc["well_name"],
                    volume,
                    dispense_flow,
                    origin=loc["origin"],
                    offset=loc["offset"],
                )
            )
        self._last_well[pip_name] = loc

    def air_gap(
        self, pip_name: str, volume: float, height: Optional[float] = None
    ) -> None:
        """Emulated: move above the last-touched well's top (protocol-API
        default 5 mm), then aspirate in place."""
        well = self._pending or self._last_well.get(pip_name)
        if well is None:
            raise RuntimeError(
                f"air_gap: no current well for {pip_name!r}; aspirate/dispense at "
                "a well first (mirrors the protocol API's location requirement)"
            )
        if well["kind"] != "well":
            raise ValueError("air_gap requires a well location")
        z = float(height) if height is not None else _AIR_GAP_DEFAULT_HEIGHT_MM
        pipette_id = self._pipette_id(pip_name)
        self.client.execute(
            RunEngineCommands.move_to_well(
                pipette_id,
                well["labware_id"],
                well["well_name"],
                origin="top",
                offset={"x": 0, "y": 0, "z": z},
            )
        )
        self.client.execute(
            RunEngineCommands.aspirate_in_place(
                pipette_id, volume, self._flow(pip_name, "aspirate", None, None)
            )
        )
        self._volumes[pip_name] = self._volumes.get(pip_name, 0.0) + float(volume)
        self._last_well[pip_name] = well
        self._pending = None

    def touch_tip(
        self,
        pip_name: str,
        labware_nickname: str,
        position: str,
        radius: float = 1.0,
        v_offset: float = -1.0,
        speed: float = 60.0,
    ) -> None:
        self.client.execute(
            RunEngineCommands.touch_tip(
                self._pipette_id(pip_name),
                self._labware_id(labware_nickname),
                position,
                radius=radius,
                v_offset=v_offset,
                speed=speed,
            )
        )

    def blow_out(self, pip_name: str) -> None:
        loc = self._take_pending("blow_out")
        pipette_id = self._pipette_id(pip_name)
        flow = self._flow(pip_name, "blow_out", None, None)
        if loc["kind"] == "coordinates":
            self.client.execute(
                RunEngineCommands.move_to_coordinates(pipette_id, loc["coordinates"])
            )
            self.client.execute(
                RunEngineCommands.blow_out_in_place(pipette_id, flow)
            )
        else:
            self.client.execute(
                RunEngineCommands.blow_out(
                    pipette_id,
                    flow,
                    loc["labware_id"],
                    loc["well_name"],
                    origin=loc["origin"],
                    offset=loc["offset"],
                )
            )
            self._last_well[pip_name] = loc
        self._volumes[pip_name] = 0.0

    def blow_out_in_place(self, pip_name: str) -> None:
        self.client.execute(
            RunEngineCommands.blow_out_in_place(
                self._pipette_id(pip_name), self._flow(pip_name, "blow_out", None, None)
            )
        )
        self._volumes[pip_name] = 0.0

    # -- pipette configuration ------------------------------------------------

    def set_speed(self, pip_name: str, speed: float) -> None:
        """Client-tracked default gantry speed. The run engine only honors a
        speed on explicit moves, so this applies to :meth:`move_to_pip`; the
        implicit moves inside aspirate/dispense keep robot defaults (unlike the
        SSH path's ``default_speed``, which affects all moves)."""
        self._pipette_id(pip_name)
        self._default_speeds[pip_name] = float(speed)

    def set_flow_rate(
        self,
        pip_name: str,
        aspirate: Optional[float] = None,
        dispense: Optional[float] = None,
        blow_out: Optional[float] = None,
    ) -> None:
        """Per-pipette flow-rate overrides, consulted whenever a call omits its
        explicit ``flow_rate`` — functional parity with the protocol API's
        ``pipette.flow_rate.*`` session state."""
        self._pipette_id(pip_name)
        rates = self._flow_rates.setdefault(pip_name, {})
        if aspirate is not None:
            rates["aspirate"] = float(aspirate)
        if dispense is not None:
            rates["dispense"] = float(dispense)
        if blow_out is not None:
            rates["blow_out"] = float(blow_out)

    def get_flow_rate(self, pip_name: str) -> Dict[str, float]:
        self._pipette_id(pip_name)
        rates = self._flow_rates.get(pip_name, {})
        return {
            "aspirate": rates.get("aspirate", self.aspirate_flow_rate),
            "dispense": rates.get("dispense", self.dispense_flow_rate),
            "blow_out": rates.get("blow_out", self.blow_out_flow_rate),
        }

    def set_well_bottom_clearance(
        self,
        pip_name: str,
        aspirate: Optional[float] = None,
        dispense: Optional[float] = None,
    ) -> None:
        """Stored for readback parity only. Inert through this wrapper — as on
        the SSH path, where every aspirate/dispense passes an explicit
        ``Location`` and the protocol API therefore never applies clearances."""
        self._pipette_id(pip_name)
        clearances = self._clearances.setdefault(
            pip_name, {"aspirate": 1.0, "dispense": 1.0}
        )
        if aspirate is not None:
            clearances["aspirate"] = float(aspirate)
        if dispense is not None:
            clearances["dispense"] = float(dispense)

    def get_well_bottom_clearance(self, pip_name: str) -> Dict[str, float]:
        self._pipette_id(pip_name)
        return dict(self._clearances.get(pip_name, {"aspirate": 1.0, "dispense": 1.0}))

    def current_volume(self, pip_name: str) -> float:
        """Client-tracked ledger (aspirates add, dispenses subtract, blow-outs
        zero). Reflects only actions issued through this adapter."""
        self._pipette_id(pip_name)
        return self._volumes.get(pip_name, 0.0)

    def home_pipette(self, pip_name: str) -> None:
        """Home the pipette's mount (Z + plunger), like ``pipette.home()``."""
        mount = self._pipette_mount(pip_name)
        self.client.execute(
            RunEngineCommands.home(
                axes=[_MOUNT_Z_AXIS[mount], _MOUNT_PLUNGER_AXIS[mount]]
            )
        )

    def home_plunger(self, pip_name: str) -> None:
        mount = self._pipette_mount(pip_name)
        self.client.execute(RunEngineCommands.home(axes=[_MOUNT_PLUNGER_AXIS[mount]]))

    # -- labware movement -------------------------------------------------------

    def move_labware(self, labware_nickname: str, new_location: str) -> None:
        """Move a loaded labware. Always ``manualMoveWithoutPause`` on the OT-2 —
        unlike OT2Control, which (incorrectly for an OT-2) uses the gripper."""
        self.client.execute(
            RunEngineCommands.move_labware(
                self._labware_id(labware_nickname), self._location(new_location)
            )
        )

    def move_labware_w_gripper(self, labware_nickname: str, new_location: str) -> None:
        """Signature-parity alias. The OT-2 has no gripper; the move is the same
        ``manualMoveWithoutPause`` record-keeping move as :meth:`move_labware`."""
        self.move_labware(labware_nickname, new_location)

    # -- heater-shaker module ----------------------------------------------------

    def hs_latch_open(self, nickname: str) -> None:
        self.client.execute(
            RunEngineCommands.hs_open_labware_latch(self._module_id(nickname))
        )

    def hs_latch_close(self, nickname: str) -> None:
        self.client.execute(
            RunEngineCommands.hs_close_labware_latch(self._module_id(nickname))
        )

    def hs_set_and_wait_shake_speed(self, nickname: str, rpm: int) -> None:
        self.client.execute(
            RunEngineCommands.hs_set_and_wait_shake_speed(self._module_id(nickname), rpm)
        )

    def hs_deactivate_shaker(self, nickname: str) -> None:
        self.client.execute(
            RunEngineCommands.hs_deactivate_shaker(self._module_id(nickname))
        )

    def hs_set_and_wait_temperature(self, nickname: str, celsius: float) -> None:
        module_id = self._module_id(nickname)
        self.client.execute(RunEngineCommands.hs_set_target_temperature(module_id, celsius))
        self.client.execute(RunEngineCommands.hs_wait_for_temperature(module_id))

    def hs_set_target_temperature(self, nickname: str, celsius: float) -> None:
        self.client.execute(
            RunEngineCommands.hs_set_target_temperature(self._module_id(nickname), celsius)
        )

    def hs_wait_for_temperature(self, nickname: str) -> None:
        self.client.execute(
            RunEngineCommands.hs_wait_for_temperature(self._module_id(nickname))
        )

    def hs_deactivate_heater(self, nickname: str) -> None:
        self.client.execute(
            RunEngineCommands.hs_deactivate_heater(self._module_id(nickname))
        )

    def hs_deactivate(self, nickname: str) -> None:
        """Emulated: the run engine has no combined deactivate; issues
        deactivateShaker then deactivateHeater."""
        self.hs_deactivate_shaker(nickname)
        self.hs_deactivate_heater(nickname)

    def set_rpm(self, nickname: str, rpm: int) -> None:
        """Set-and-wait shake speed; out-of-band values deactivate the shaker."""
        if 200 <= rpm <= 3000:
            self.hs_set_and_wait_shake_speed(nickname, rpm)
        else:
            self.hs_deactivate_shaker(nickname)

    def set_temp(self, nickname: str, temp: float) -> None:
        """Set-and-wait heater temperature; out-of-band values deactivate it."""
        if 27 <= temp <= 95:
            self.hs_set_and_wait_temperature(nickname, temp)
        else:
            self.hs_deactivate_heater(nickname)

    def get_rpm(self, nickname: str) -> float:
        return float(self._module_live_data(nickname).get("currentSpeed") or 0.0)

    def get_temp(self, nickname: str) -> float:
        return float(self._module_live_data(nickname).get("currentTemperature") or 0.0)

    # -- temperature module -------------------------------------------------------

    def tempmod_start_set_temperature(self, nickname: str, celsius: float) -> None:
        """Set the target and return; the block ramps in the background.

        The gateway's operator/assistant path uses this so a 4 °C set does not
        hold BUSY for the whole cool-down (and blow ``OT2_HTTP_COMMAND_TIMEOUT``).
        ``tempmod_set_temperature`` remains the protocol-API set-and-wait.
        """
        self.client.execute(
            RunEngineCommands.temp_set_target(self._module_id(nickname), celsius)
        )

    def tempmod_set_temperature(self, nickname: str, celsius: float) -> None:
        """Set-and-wait, like the protocol API's blocking ``set_temperature``."""
        module_id = self._module_id(nickname)
        self.client.execute(RunEngineCommands.temp_set_target(module_id, celsius))
        self.client.execute(RunEngineCommands.temp_wait(module_id))

    def tempmod_await_temperature(self, nickname: str) -> None:
        self.client.execute(RunEngineCommands.temp_wait(self._module_id(nickname)))

    def tempmod_deactivate(self, nickname: str) -> None:
        self.client.execute(RunEngineCommands.temp_deactivate(self._module_id(nickname)))

    # -- magnetic module ------------------------------------------------------------

    def magmod_engage(
        self,
        nickname: str,
        height_from_base: Optional[float] = None,
        offset: Optional[float] = None,
    ) -> None:
        """Engage at ``height_from_base`` (mm above labware base). The run
        engine's engage takes only that form: ``offset`` (relative to the
        labware's default engage height) is unsupported here."""
        if offset is not None:
            raise NotImplementedError(
                "magneticModule/engage takes an absolute height only; "
                "offset-from-default has no run-engine equivalent"
            )
        if height_from_base is None:
            raise ValueError(
                "magmod_engage over HTTP requires height_from_base (the run engine "
                "cannot default to the labware's engage height)"
            )
        self.client.execute(
            RunEngineCommands.mag_engage(self._module_id(nickname), height_from_base)
        )

    def magmod_disengage(self, nickname: str) -> None:
        self.client.execute(RunEngineCommands.mag_disengage(self._module_id(nickname)))

    # -- thermocycler module ----------------------------------------------------------

    def thermocycler_open_lid(self, nickname: str) -> None:
        self.client.execute(RunEngineCommands.tc_open_lid(self._module_id(nickname)))

    def thermocycler_close_lid(self, nickname: str) -> None:
        self.client.execute(RunEngineCommands.tc_close_lid(self._module_id(nickname)))

    def thermocycler_open_labware_latch(self, nickname: str) -> None:
        raise NotImplementedError(
            "the thermocycler has no labware latch (the SSH method would fail "
            "on-robot too; labware latches are a heater-shaker feature)"
        )

    def thermocycler_close_labware_latch(self, nickname: str) -> None:
        raise NotImplementedError(
            "the thermocycler has no labware latch (the SSH method would fail "
            "on-robot too; labware latches are a heater-shaker feature)"
        )

    def thermocycler_set_block_temperature(
        self,
        nickname: str,
        temperature: float,
        hold_time_seconds: Optional[float] = None,
        hold_time_minutes: Optional[float] = None,
        block_max_volume: Optional[float] = None,
        ramp_rate: Optional[float] = None,
    ) -> None:
        """Set-and-wait block temperature (+ optional hold), like the protocol
        API. ``ramp_rate`` has no run-engine equivalent."""
        if ramp_rate is not None:
            raise NotImplementedError(
                "thermocycler/setTargetBlockTemperature has no rampRate parameter"
            )
        hold: Optional[float] = None
        if hold_time_seconds is not None or hold_time_minutes is not None:
            hold = float(hold_time_seconds or 0) + float(hold_time_minutes or 0) * 60.0
        module_id = self._module_id(nickname)
        self.client.execute(
            RunEngineCommands.tc_set_target_block_temperature(
                module_id,
                temperature,
                hold_time_seconds=hold,
                block_max_volume_ul=block_max_volume,
            )
        )
        self.client.execute(RunEngineCommands.tc_wait_for_block_temperature(module_id))

    def thermocycler_set_lid_temperature(self, nickname: str, temperature: float) -> None:
        module_id = self._module_id(nickname)
        self.client.execute(
            RunEngineCommands.tc_set_target_lid_temperature(module_id, temperature)
        )
        self.client.execute(RunEngineCommands.tc_wait_for_lid_temperature(module_id))

    def thermocycler_deactivate_block(self, nickname: str) -> None:
        self.client.execute(
            RunEngineCommands.tc_deactivate_block(self._module_id(nickname))
        )

    def thermocycler_deactivate_lid(self, nickname: str) -> None:
        self.client.execute(RunEngineCommands.tc_deactivate_lid(self._module_id(nickname)))

    def thermocycler_deactivate(self, nickname: str) -> None:
        """Emulated: deactivateBlock then deactivateLid (no combined command)."""
        self.thermocycler_deactivate_block(nickname)
        self.thermocycler_deactivate_lid(nickname)

    # -- snapshots -----------------------------------------------------------

    def run_snapshot(self) -> Dict[str, Any]:
        """Raw run resource for deck-state parsing (replaces the REPL snapshot)."""
        return self.client.get_run()

    # -- internals ---------------------------------------------------------

    def _location(self, new_location: Any) -> Any:
        if isinstance(new_location, dict):
            return new_location  # caller-supplied module / on-labware location
        if new_location in _OFF_DECK_ALIASES:
            return OFF_DECK
        return deck_slot(new_location)

    def adopt_run_state(self) -> None:
        """Rehydrate the nickname->id maps from the run engine's own ids.

        The run is authoritative for what is loaded; ``_labware_ids`` /
        ``_pipette_ids`` / ``_module_ids`` are a local cache written *only* at
        load time (each ``load_*`` does ``ids[nickname] = nickname``). A
        partially-failed setup (some loads succeeded, one raised) or a control
        object built against a run a previous process loaded therefore leaves
        entries in the run that are absent from the cache, so ``_labware_id`` /
        ``_pipette_id`` would raise for a name the run actually knows. Reading
        the run back closes that gap without a service restart.

        Keyed by the run-engine id, which for anything this gateway loaded *is*
        the nickname (``load_*`` passes ``labware_id=nickname``), so adopting a
        run recovers those names too. Non-clobbering (``setdefault``): an
        existing mapping wins, so a trash nickname pointing at a differently
        named area id, or an adapter's synthetic id, is never overwritten — this
        only *fills* gaps. Best-effort: a failed run read must not break a caller
        that is merely resolving a name.
        """
        try:
            run = self.client.get_run() or {}
        except Exception:
            return
        for lw in run.get("labware") or []:
            rid = lw.get("id")
            if rid:
                self._labware_ids.setdefault(rid, rid)
        for pip in run.get("pipettes") or []:
            rid = pip.get("id")
            if rid:
                self._pipette_ids.setdefault(rid, rid)
                mount = pip.get("mount")
                if mount:
                    self._pipette_mounts.setdefault(rid, mount)
        for mod in run.get("modules") or []:
            rid = mod.get("id")
            if rid:
                self._module_ids.setdefault(rid, rid)

    def _labware_id(self, nickname: str) -> str:
        try:
            return self._labware_ids[nickname]
        except KeyError:
            pass
        # The name is not in the local cache — the run may still hold it (a
        # partial setup, an adopted run). Refresh once from the run, then retry.
        self.adopt_run_state()
        try:
            return self._labware_ids[nickname]
        except KeyError:
            loaded = ", ".join(sorted(self._labware_ids)) or "none"
            raise RuntimeError(
                f"labware {nickname!r} is not loaded in this run (loaded: {loaded})"
            ) from None

    def _pipette_id(self, nickname: str) -> str:
        try:
            return self._pipette_ids[nickname]
        except KeyError:
            pass
        self.adopt_run_state()
        try:
            return self._pipette_ids[nickname]
        except KeyError:
            loaded = ", ".join(sorted(self._pipette_ids)) or "none"
            raise RuntimeError(
                f"pipette {nickname!r} is not loaded in this run (loaded: {loaded})"
            ) from None

    def _pipette_mount(self, nickname: str) -> str:
        self._pipette_id(nickname)
        mount = self._pipette_mounts.get(nickname)
        if mount not in _MOUNT_Z_AXIS:
            raise RuntimeError(f"pipette {nickname!r} has no known mount")
        return mount

    def _module_id(self, nickname: str) -> str:
        try:
            return self._module_ids[nickname]
        except KeyError:
            pass
        self.adopt_run_state()
        try:
            return self._module_ids[nickname]
        except KeyError:
            loaded = ", ".join(sorted(self._module_ids)) or "none"
            raise RuntimeError(
                f"module {nickname!r} is not loaded in this run (loaded: {loaded})"
            ) from None

    def _module_live_data(self, nickname: str) -> Dict[str, Any]:
        """Live telemetry for a loaded module, matched by hardware serial
        (``GET /modules``). Backs get_rpm/get_temp."""
        self._module_id(nickname)
        serial = self._module_serials.get(nickname)
        if not serial:
            raise RunEngineError(
                f"module {nickname!r} has no recorded serial number; the loadModule "
                "result did not include one, so live readbacks are unavailable"
            )
        for module in self.client.get_modules():
            if module.get("serialNumber") == serial:
                return module.get("data") or {}
        raise RunEngineError(
            f"module {nickname!r} (serial {serial}) not reported by GET /modules"
        )

    def _flow(
        self,
        pip_name: str,
        kind: str,
        override: Optional[float],
        rate: Optional[float],
    ) -> float:
        """Effective flow rate: explicit per-call > per-pipette set_flow_rate >
        constructor default, times the protocol-API ``rate`` multiplier."""
        if override is not None:
            base = float(override)
        else:
            defaults = {
                "aspirate": self.aspirate_flow_rate,
                "dispense": self.dispense_flow_rate,
                "blow_out": self.blow_out_flow_rate,
            }
            base = self._flow_rates.get(pip_name, {}).get(kind, defaults[kind])
        return base * (float(rate) if rate is not None else 1.0)

    def _take_pending(self, action: str) -> Dict[str, Any]:
        if self._pending is None:
            raise RuntimeError(f"{action} needs a location; call get_location_from_labware first")
        pending = self._pending
        self._pending = None
        return pending

    def _labware_definition(self, nickname: str) -> Dict[str, Any]:
        """Schema-2 definition of a loaded labware, resolved by matching the
        run's ``definitionUri`` against ``GET .../loaded_labware_definitions``."""
        labware_id = self._labware_id(nickname)
        run = self.client.get_run()
        uri: Optional[str] = None
        for entry in run.get("labware") or []:
            if entry.get("id") == labware_id:
                uri = entry.get("definitionUri")
                break
        if not uri:
            raise RunEngineError(
                f"labware {nickname!r} has no definitionUri in the run resource"
            )
        for definition in self.client.get_loaded_labware_definitions():
            parameters = definition.get("parameters") or {}
            candidate = (
                f"{definition.get('namespace')}/{parameters.get('loadName')}/"
                f"{definition.get('version')}"
            )
            if candidate == uri:
                return definition
        raise RunEngineError(f"no loaded definition matches {uri!r}")

    def _well_definition(self, nickname: str, position: str) -> Dict[str, Any]:
        wells = self._labware_definition(nickname).get("wells") or {}
        try:
            return wells[position]
        except KeyError:
            raise RunEngineError(
                f"labware {nickname!r} has no well {position!r}"
            ) from None
