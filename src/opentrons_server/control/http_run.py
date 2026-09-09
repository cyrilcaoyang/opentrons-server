"""Run-engine HTTP client for driving an OT-2 over the robot-server API.

This is the HTTP replacement for the SSH REPL transport (``OT2Control`` /
``SSHClient``). It speaks the Opentrons robot-server run engine on ``:31950``:
create a run, post commands one at a time, register custom labware, read run
state. See ``docs/HTTP_TRANSPORT.md`` for the full migration design.

Driving model (confirmed against Opentrons v8.7.0 source):

- We create a run with ``POST /runs`` and **never issue ``play``**. Every command
  is posted with ``intent="setup"`` and executes immediately while the run stays
  in the SETUP phase. Issuing ``play`` would move the run to RUNNING and cause the
  engine to reject all further setup commands (``SetupCommandNotAllowedError``).
- ``waitUntilComplete=true`` blocks the POST until the command reaches a terminal
  status; a terminal ``failed`` status is surfaced as :class:`CommandFailed`.
- ``manualMoveWithPause`` is deliberately unsupported (it pauses the run out of
  SETUP and cannot be resumed without ``play``); plate moves use
  ``manualMoveWithoutPause`` — see :class:`RunEngineCommands`.

Every request/response uses the ``{"data": {...}}`` envelope and carries the
``Opentrons-Version`` header.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

# Off-deck location literal (confirmed: bare JSON string, not an object).
OFF_DECK = "offDeck"

_DEFAULT_OPENTRONS_VERSION = os.getenv("OT2_OPENTRONS_VERSION", "3")
# Wall-clock ceiling for a single blocking command, mirrors OT2_SSH_COMMAND_TIMEOUT.
_DEFAULT_COMMAND_TIMEOUT_S = float(os.getenv("OT2_HTTP_COMMAND_TIMEOUT", "120"))
# Timeout for non-command control-plane calls (create run, get run, register def).
_DEFAULT_REQUEST_TIMEOUT_S = float(os.getenv("OT2_HTTP_TIMEOUT", "10"))


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RunEngineError(Exception):
    """Base for run-engine client failures."""


class RunEngineUnreachable(RunEngineError, OSError):
    """Transport-level failure reaching the robot-server (timeout / refused).

    Subclasses ``OSError`` so the gateway's existing transport-loss handling in
    ``OT2Service._run_action`` (which catches ``OSError``) treats a dropped HTTP
    call the same way it treats a dropped SSH call — i.e. a non-idempotent action
    lands in ``UNKNOWN_OUTCOME`` rather than a plain error.
    """


class RunEngineHTTPError(RunEngineError):
    """The robot-server returned a non-2xx response to a control-plane call."""

    def __init__(self, status_code: int, detail: str, *, path: str) -> None:
        self.status_code = status_code
        self.detail = detail
        self.path = path
        super().__init__(f"{status_code} from {path}: {detail}")


class CommandNotCompleted(RunEngineError, OSError):
    """A blocking command did not reach a terminal status within the wait window.

    When ``waitUntilComplete=true``'s server-side ``timeout`` elapses, robot-server
    returns HTTP 200 with the command still ``queued`` / ``running`` (NOT ``failed``).
    Surfacing that as an error instead of a false success is the whole point: the
    command may still be executing on the robot, so the outcome is genuinely
    *unknown*. Subclasses ``OSError`` for the same reason as
    :class:`RunEngineUnreachable` — a non-idempotent action lands in
    ``UNKNOWN_OUTCOME`` (via ``OT2Service._run_action``) rather than a plain error.
    """

    def __init__(self, command: Dict[str, Any], *, timeout_ms: Optional[int] = None) -> None:
        self.command = command
        self.status: Optional[str] = command.get("status")
        self.timeout_ms = timeout_ms
        command_type = command.get("commandType", "command")
        waited = f", waited {timeout_ms} ms" if timeout_ms is not None else ""
        super().__init__(
            f"{command_type} did not complete (status={self.status!r}{waited})"
        )


class CommandFailed(RunEngineError):
    """A posted command reached a terminal ``failed`` status.

    ``error_type`` / ``detail`` / ``error_code`` are extracted from the command's
    ``error`` object when present, so callers can branch on the failure kind
    instead of string-matching.
    """

    def __init__(self, command: Dict[str, Any]) -> None:
        self.command = command
        error = command.get("error") or {}
        self.error_type: Optional[str] = error.get("errorType")
        self.error_code: Optional[str] = error.get("errorCode")
        self.detail: str = error.get("detail") or "command failed"
        super().__init__(
            f"{command.get('commandType', 'command')} failed: {self.detail}"
            + (f" ({self.error_type})" if self.error_type else "")
        )


# ---------------------------------------------------------------------------
# Command builders — encode the confirmed v8.7.0 param schemas in one place.
# Each returns a (commandType, params) pair ready for RunEngineClient.execute().
# ---------------------------------------------------------------------------


Command = Tuple[str, Dict[str, Any]]
SlotName = Union[str, int]
# A LoadableLabwareLocation: a deck slot object, the off-deck literal, or a
# module/on-labware object the caller supplies directly.
Location = Union[str, Dict[str, Any]]


def deck_slot(slot: SlotName) -> Dict[str, str]:
    """A DeckSlotLocation for an OT-2 slot (``"1"``..``"12"``)."""
    return {"slotName": str(slot)}


def _well_location(
    origin: Optional[str],
    offset: Optional[Dict[str, float]],
    *,
    volume_offset: Optional[Union[float, str]] = None,
) -> Optional[Dict[str, Any]]:
    if origin is None and offset is None and volume_offset is None:
        return None
    loc: Dict[str, Any] = {}
    if origin is not None:
        loc["origin"] = origin
    if offset is not None:
        loc["offset"] = offset
    if volume_offset is not None:
        loc["volumeOffset"] = volume_offset
    return loc


class RunEngineCommands:
    """Builders for the run-engine commands the gateway needs.

    ``origin`` values: ``top`` | ``bottom`` | ``center`` | ``meniscus`` for
    aspirate/dispense; ``top`` | ``bottom`` | ``center`` for pick-up-tip;
    ``top`` | ``bottom`` | ``center`` | ``default`` for drop-tip. ``offset`` is
    ``{"x","y","z"}`` in mm. ``flow_rate`` is µL/s; ``volume`` is µL.
    """

    @staticmethod
    def load_pipette(
        pipette_name: str,
        mount: str,
        *,
        pipette_id: Optional[str] = None,
    ) -> Command:
        params: Dict[str, Any] = {"pipetteName": pipette_name, "mount": mount}
        if pipette_id is not None:
            params["pipetteId"] = pipette_id
        return "loadPipette", params

    @staticmethod
    def load_labware(
        load_name: str,
        namespace: str,
        version: int,
        location: Location,
        *,
        labware_id: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> Command:
        params: Dict[str, Any] = {
            "location": location,
            "loadName": load_name,
            "namespace": namespace,
            "version": int(version),
        }
        if labware_id is not None:
            params["labwareId"] = labware_id
        if display_name is not None:
            params["displayName"] = display_name
        return "loadLabware", params

    @staticmethod
    def load_module(
        model: str,
        location: Dict[str, Any],
        *,
        module_id: Optional[str] = None,
    ) -> Command:
        params: Dict[str, Any] = {"model": model, "location": location}
        if module_id is not None:
            params["moduleId"] = module_id
        return "loadModule", params

    @staticmethod
    def pick_up_tip(
        pipette_id: str,
        labware_id: str,
        well_name: str,
        *,
        origin: Optional[str] = None,
        offset: Optional[Dict[str, float]] = None,
    ) -> Command:
        params: Dict[str, Any] = {
            "pipetteId": pipette_id,
            "labwareId": labware_id,
            "wellName": well_name,
        }
        well = _well_location(origin, offset)
        if well is not None:
            params["wellLocation"] = well
        return "pickUpTip", params

    @staticmethod
    def aspirate(
        pipette_id: str,
        labware_id: str,
        well_name: str,
        volume: float,
        flow_rate: float,
        *,
        origin: str = "bottom",
        offset: Optional[Dict[str, float]] = None,
    ) -> Command:
        params: Dict[str, Any] = {
            "pipetteId": pipette_id,
            "labwareId": labware_id,
            "wellName": well_name,
            "volume": float(volume),
            "flowRate": float(flow_rate),
            "wellLocation": _well_location(origin, offset or {"x": 0, "y": 0, "z": 0}),
        }
        return "aspirate", params

    @staticmethod
    def dispense(
        pipette_id: str,
        labware_id: str,
        well_name: str,
        volume: float,
        flow_rate: float,
        *,
        origin: str = "bottom",
        offset: Optional[Dict[str, float]] = None,
        push_out: Optional[float] = None,
    ) -> Command:
        params: Dict[str, Any] = {
            "pipetteId": pipette_id,
            "labwareId": labware_id,
            "wellName": well_name,
            "volume": float(volume),
            "flowRate": float(flow_rate),
            "wellLocation": _well_location(origin, offset or {"x": 0, "y": 0, "z": 0}),
        }
        if push_out is not None:
            params["pushOut"] = float(push_out)
        return "dispense", params

    @staticmethod
    def drop_tip(
        pipette_id: str,
        labware_id: str,
        well_name: str,
        *,
        origin: Optional[str] = None,
        offset: Optional[Dict[str, float]] = None,
        home_after: Optional[bool] = None,
    ) -> Command:
        params: Dict[str, Any] = {
            "pipetteId": pipette_id,
            "labwareId": labware_id,
            "wellName": well_name,
        }
        well = _well_location(origin, offset)
        if well is not None:
            params["wellLocation"] = well
        if home_after is not None:
            params["homeAfter"] = home_after
        return "dropTip", params

    @staticmethod
    def drop_tip_in_place(
        pipette_id: str,
        *,
        home_after: Optional[bool] = None,
    ) -> Command:
        params: Dict[str, Any] = {"pipetteId": pipette_id}
        if home_after is not None:
            params["homeAfter"] = home_after
        return "dropTipInPlace", params

    @staticmethod
    def move_to_addressable_area_for_drop_tip(
        pipette_id: str,
        addressable_area_name: str,
        *,
        alternate_drop_location: bool = True,
    ) -> Command:
        """Position over a deck *area* (e.g. the OT-2 fixed trash) for a drop.

        On modern robot-servers the OT-2 fixed trash is not labware — it is
        the ``fixedTrash`` addressable area — and this + ``dropTipInPlace``
        is exactly how the protocol API drops tips into it (API 2.16+).
        ``alternate_drop_location`` mirrors the protocol API's default of
        scattering drop positions so tips do not pile into one spot.
        """
        return (
            "moveToAddressableAreaForDropTip",
            {
                "pipetteId": pipette_id,
                "addressableAreaName": addressable_area_name,
                "alternateDropLocation": alternate_drop_location,
            },
        )

    @staticmethod
    def blow_out(
        pipette_id: str,
        flow_rate: float,
        labware_id: str,
        well_name: str,
        *,
        origin: str = "top",
        offset: Optional[Dict[str, float]] = None,
    ) -> Command:
        params: Dict[str, Any] = {
            "pipetteId": pipette_id,
            "flowRate": float(flow_rate),
            "labwareId": labware_id,
            "wellName": well_name,
            "wellLocation": _well_location(origin, offset or {"x": 0, "y": 0, "z": 0}),
        }
        return "blowout", params

    @staticmethod
    def move_labware(
        labware_id: str,
        new_location: Location,
        *,
        strategy: str = "manualMoveWithoutPause",
    ) -> Command:
        """Move a previously-loaded labware.

        ``strategy`` is fixed to ``manualMoveWithoutPause`` by default and that is
        the only value this gateway supports on an OT-2: it records the location
        change and succeeds immediately without leaving the SETUP phase.
        ``manualMoveWithPause`` and ``usingGripper`` are rejected (the former
        breaks the never-played model; the latter is Flex-only).
        """
        if strategy != "manualMoveWithoutPause":
            raise ValueError(
                "OT-2 gateway only supports moveLabware strategy "
                f"'manualMoveWithoutPause'; got {strategy!r} "
                "(manualMoveWithPause breaks the setup-only run; usingGripper is Flex-only)"
            )
        return "moveLabware", {
            "labwareId": labware_id,
            "newLocation": new_location,
            "strategy": strategy,
        }

    @staticmethod
    def home(axes: Optional[List[str]] = None) -> Command:
        params: Dict[str, Any] = {}
        if axes is not None:
            params["axes"] = axes
        return "home", params

    # -- protocol-level ------------------------------------------------------

    @staticmethod
    def comment(message: str) -> Command:
        return "comment", {"message": str(message)}

    @staticmethod
    def wait_for_duration(seconds: float) -> Command:
        return "waitForDuration", {"seconds": float(seconds)}

    @staticmethod
    def prepare_to_aspirate(pipette_id: str) -> Command:
        return "prepareToAspirate", {"pipetteId": pipette_id}

    # -- motion --------------------------------------------------------------

    @staticmethod
    def move_to_well(
        pipette_id: str,
        labware_id: str,
        well_name: str,
        *,
        origin: Optional[str] = None,
        offset: Optional[Dict[str, float]] = None,
        speed: Optional[float] = None,
        force_direct: Optional[bool] = None,
        minimum_z_height: Optional[float] = None,
    ) -> Command:
        params: Dict[str, Any] = {
            "pipetteId": pipette_id,
            "labwareId": labware_id,
            "wellName": well_name,
        }
        well = _well_location(origin, offset)
        if well is not None:
            params["wellLocation"] = well
        if speed is not None:
            params["speed"] = float(speed)
        if force_direct is not None:
            params["forceDirect"] = bool(force_direct)
        if minimum_z_height is not None:
            params["minimumZHeight"] = float(minimum_z_height)
        return "moveToWell", params

    @staticmethod
    def move_to_coordinates(
        pipette_id: str,
        coordinates: Dict[str, float],
        *,
        speed: Optional[float] = None,
        force_direct: Optional[bool] = None,
        minimum_z_height: Optional[float] = None,
    ) -> Command:
        params: Dict[str, Any] = {
            "pipetteId": pipette_id,
            "coordinates": {k: float(coordinates[k]) for k in ("x", "y", "z")},
        }
        if speed is not None:
            params["speed"] = float(speed)
        if force_direct is not None:
            params["forceDirect"] = bool(force_direct)
        if minimum_z_height is not None:
            params["minimumZHeight"] = float(minimum_z_height)
        return "moveToCoordinates", params

    # -- in-place liquid handling (after an explicit move) --------------------

    @staticmethod
    def aspirate_in_place(pipette_id: str, volume: float, flow_rate: float) -> Command:
        return "aspirateInPlace", {
            "pipetteId": pipette_id,
            "volume": float(volume),
            "flowRate": float(flow_rate),
        }

    @staticmethod
    def dispense_in_place(
        pipette_id: str,
        volume: float,
        flow_rate: float,
        *,
        push_out: Optional[float] = None,
    ) -> Command:
        params: Dict[str, Any] = {
            "pipetteId": pipette_id,
            "volume": float(volume),
            "flowRate": float(flow_rate),
        }
        if push_out is not None:
            params["pushOut"] = float(push_out)
        return "dispenseInPlace", params

    @staticmethod
    def blow_out_in_place(pipette_id: str, flow_rate: float) -> Command:
        return "blowOutInPlace", {"pipetteId": pipette_id, "flowRate": float(flow_rate)}

    @staticmethod
    def touch_tip(
        pipette_id: str,
        labware_id: str,
        well_name: str,
        *,
        radius: Optional[float] = None,
        v_offset: Optional[float] = None,
        speed: Optional[float] = None,
    ) -> Command:
        """``touchTip``. ``v_offset`` is mm relative to the well top (negative =
        below the rim), matching the protocol API's ``touch_tip(v_offset=...)``."""
        params: Dict[str, Any] = {
            "pipetteId": pipette_id,
            "labwareId": labware_id,
            "wellName": well_name,
        }
        if radius is not None:
            params["radius"] = float(radius)
        if speed is not None:
            params["speed"] = float(speed)
        if v_offset is not None:
            params["wellLocation"] = {
                "origin": "top",
                "offset": {"x": 0, "y": 0, "z": float(v_offset)},
            }
        return "touchTip", params

    # -- heater-shaker module --------------------------------------------------

    @staticmethod
    def hs_open_labware_latch(module_id: str) -> Command:
        return "heaterShaker/openLabwareLatch", {"moduleId": module_id}

    @staticmethod
    def hs_close_labware_latch(module_id: str) -> Command:
        return "heaterShaker/closeLabwareLatch", {"moduleId": module_id}

    @staticmethod
    def hs_set_and_wait_shake_speed(module_id: str, rpm: float) -> Command:
        return "heaterShaker/setAndWaitForShakeSpeed", {"moduleId": module_id, "rpm": float(rpm)}

    @staticmethod
    def hs_deactivate_shaker(module_id: str) -> Command:
        return "heaterShaker/deactivateShaker", {"moduleId": module_id}

    @staticmethod
    def hs_set_target_temperature(module_id: str, celsius: float) -> Command:
        return "heaterShaker/setTargetTemperature", {"moduleId": module_id, "celsius": float(celsius)}

    @staticmethod
    def hs_wait_for_temperature(module_id: str) -> Command:
        return "heaterShaker/waitForTemperature", {"moduleId": module_id}

    @staticmethod
    def hs_deactivate_heater(module_id: str) -> Command:
        return "heaterShaker/deactivateHeater", {"moduleId": module_id}

    # -- temperature module -----------------------------------------------------

    @staticmethod
    def temp_set_target(module_id: str, celsius: float) -> Command:
        return "temperatureModule/setTargetTemperature", {"moduleId": module_id, "celsius": float(celsius)}

    @staticmethod
    def temp_wait(module_id: str) -> Command:
        return "temperatureModule/waitForTemperature", {"moduleId": module_id}

    @staticmethod
    def temp_deactivate(module_id: str) -> Command:
        return "temperatureModule/deactivate", {"moduleId": module_id}

    # -- magnetic module ----------------------------------------------------------

    @staticmethod
    def mag_engage(module_id: str, height: float) -> Command:
        """``height`` is mm above the labware base (the run engine's only form)."""
        return "magneticModule/engage", {"moduleId": module_id, "height": float(height)}

    @staticmethod
    def mag_disengage(module_id: str) -> Command:
        return "magneticModule/disengage", {"moduleId": module_id}

    # -- thermocycler module ---------------------------------------------------------

    @staticmethod
    def tc_open_lid(module_id: str) -> Command:
        return "thermocycler/openLid", {"moduleId": module_id}

    @staticmethod
    def tc_close_lid(module_id: str) -> Command:
        return "thermocycler/closeLid", {"moduleId": module_id}

    @staticmethod
    def tc_set_target_block_temperature(
        module_id: str,
        celsius: float,
        *,
        hold_time_seconds: Optional[float] = None,
        block_max_volume_ul: Optional[float] = None,
    ) -> Command:
        params: Dict[str, Any] = {"moduleId": module_id, "celsius": float(celsius)}
        if hold_time_seconds is not None:
            params["holdTimeSeconds"] = float(hold_time_seconds)
        if block_max_volume_ul is not None:
            params["blockMaxVolumeUl"] = float(block_max_volume_ul)
        return "thermocycler/setTargetBlockTemperature", params

    @staticmethod
    def tc_wait_for_block_temperature(module_id: str) -> Command:
        return "thermocycler/waitForBlockTemperature", {"moduleId": module_id}

    @staticmethod
    def tc_set_target_lid_temperature(module_id: str, celsius: float) -> Command:
        return "thermocycler/setTargetLidTemperature", {"moduleId": module_id, "celsius": float(celsius)}

    @staticmethod
    def tc_wait_for_lid_temperature(module_id: str) -> Command:
        return "thermocycler/waitForLidTemperature", {"moduleId": module_id}

    @staticmethod
    def tc_deactivate_block(module_id: str) -> Command:
        return "thermocycler/deactivateBlock", {"moduleId": module_id}

    @staticmethod
    def tc_deactivate_lid(module_id: str) -> Command:
        return "thermocycler/deactivateLid", {"moduleId": module_id}


# ---------------------------------------------------------------------------
# Transport client
# ---------------------------------------------------------------------------


class RunEngineClient:
    """Thin client over the robot-server run engine.

    Holds one run id (created via :meth:`create_run`). Not thread-safe: the
    gateway serialises control actions through its own state machine.
    """

    def __init__(
        self,
        base_url: str,
        *,
        session: Optional[requests.Session] = None,
        opentrons_version: str = _DEFAULT_OPENTRONS_VERSION,
        command_timeout_s: float = _DEFAULT_COMMAND_TIMEOUT_S,
        request_timeout_s: float = _DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._headers = {"Opentrons-Version": opentrons_version}
        self.command_timeout_s = command_timeout_s
        self.request_timeout_s = request_timeout_s
        self.run_id: Optional[str] = None
        self._stop_requested = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def create_run(self) -> str:
        """Create an empty (protocol-less) run and remember its id.

        Uses the long *command* timeout, not the 10 s request timeout: run
        creation makes the robot-server prune old runs and spin up a protocol
        engine, and was observed live (2026-08-12) taking >10 s — a read
        timeout here failed the whole session bring-up over a slow, but
        healthy, robot.
        """
        data = self._request(
            "POST", "/runs", json_body={"data": {}}, timeout=self.command_timeout_s
        )
        run_id = data.get("id")
        if not run_id:
            raise RunEngineError(f"run creation returned no id: {data!r}")
        self.run_id = run_id
        return run_id

    def stop_run(self) -> None:
        """Best-effort ``stop`` action; safe to call without a run."""
        if self.run_id is None:
            return
        try:
            self._request(
                "POST",
                f"/runs/{self.run_id}/actions",
                json_body={"data": {"actionType": "stop"}},
                timeout=self.request_timeout_s,
            )
        except RunEngineError:
            pass  # shutdown is best-effort; the run id is discarded regardless

    def stop_and_confirm(self, timeout_s: float = 10.0) -> None:
        """Stop this run over a separate connection, then require stopped readback.

        Latch before I/O so composite operations cannot send their next command.
        A timeout is an unknown outcome, never an acknowledgment of stopped motion.
        """
        self._stop_requested.set()
        if self.run_id is None:
            raise RunEngineError("no active run to stop")
        other = RunEngineClient(self.base_url, opentrons_version=self._headers["Opentrons-Version"],
                                request_timeout_s=min(timeout_s, self.request_timeout_s))
        other.run_id = self.run_id
        deadline = time.monotonic() + timeout_s
        try:
            other._request("POST", f"/runs/{self.run_id}/actions",
                           json_body={"data": {"actionType": "stop"}},
                           timeout=other.request_timeout_s)
            while time.monotonic() < deadline:
                if other.get_run().get("status") == "stopped":
                    return
                time.sleep(0.1)
            raise TimeoutError("stop requested but robot did not confirm stopped")
        finally:
            other.close()

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    # -- commands ----------------------------------------------------------

    def execute(
        self,
        command: Command,
        *,
        intent: str = "setup",
        wait: bool = True,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Post one command and (by default) block until it finishes.

        Returns the command resource. Raises :class:`CommandFailed` if the command
        reached a terminal ``failed`` status.
        """
        if self._stop_requested.is_set():
            raise OSError("run stopped; no further commands permitted")
        if self.run_id is None:
            raise RunEngineError("no active run; call create_run() first")
        command_type, params = command

        params_q: Dict[str, Any] = {}
        read_timeout = self.request_timeout_s
        effective_ms: Optional[int] = None
        if wait:
            params_q["waitUntilComplete"] = "true"
            effective_ms = (
                timeout_ms if timeout_ms is not None else int(self.command_timeout_s * 1000)
            )
            params_q["timeout"] = effective_ms
            # Keep the socket read timeout comfortably above the server-side wait
            # so requests does not abort before the run engine returns.
            read_timeout = effective_ms / 1000.0 + 5.0

        body = {"data": {"commandType": command_type, "params": params, "intent": intent}}
        result = self._request(
            "POST",
            f"/runs/{self.run_id}/commands",
            json_body=body,
            params=params_q,
            timeout=read_timeout,
        )
        if self._stop_requested.is_set():
            raise OSError("stop requested during command; inspect its outcome")
        status = result.get("status")
        if status == "failed":
            raise CommandFailed(result)
        # A blocking call must actually reach ``succeeded``. If the server-side
        # ``waitUntilComplete`` timeout elapsed, robot-server returns 200 with the
        # command still queued/running — do not report that as a completed command.
        if wait and status != "succeeded":
            raise CommandNotCompleted(result, timeout_ms=effective_ms)
        return result

    def get_command(self, command_id: str) -> Dict[str, Any]:
        if self.run_id is None:
            raise RunEngineError("no active run")
        return self._request(
            "GET",
            f"/runs/{self.run_id}/commands/{command_id}",
            timeout=self.request_timeout_s,
        )

    # -- run state / labware ----------------------------------------------

    def get_run(self) -> Dict[str, Any]:
        if self.run_id is None:
            raise RunEngineError("no active run")
        return self._request("GET", f"/runs/{self.run_id}", timeout=self.request_timeout_s)

    def get_loaded_labware_definitions(self) -> List[Dict[str, Any]]:
        """Full schema-2 definitions of every labware loaded in the run.

        Backs the client-side geometry readbacks (well diameter/depth, tip
        length) that the SSH transport reads live from the protocol object.
        """
        if self.run_id is None:
            raise RunEngineError("no active run")
        data = self._request(
            "GET",
            f"/runs/{self.run_id}/loaded_labware_definitions",
            timeout=self.request_timeout_s,
        )
        return data if isinstance(data, list) else []

    def get_modules(self) -> List[Dict[str, Any]]:
        """Live attached-module telemetry (``GET /modules``): serial, model, and
        a ``data`` blob with current/target speed and temperature."""
        data = self._request("GET", "/modules", timeout=self.request_timeout_s)
        return data if isinstance(data, list) else []

    # -- robot-level (non-run) endpoints ------------------------------------

    def get_lights(self) -> bool:
        """Rail-light state via ``GET /robot/lights`` (no ``data`` envelope)."""
        payload = self._request_raw("GET", "/robot/lights", timeout=self.request_timeout_s)
        return bool(payload.get("on"))

    def set_lights(self, on: bool) -> None:
        """Set rail lights via ``POST /robot/lights`` (no ``data`` envelope)."""
        self._request_raw(
            "POST",
            "/robot/lights",
            json_body={"on": bool(on)},
            timeout=self.request_timeout_s,
        )

    def add_labware_definition(self, definition: Dict[str, Any]) -> str:
        """Register a custom labware definition on the run; return its uri.

        Idempotent server-side: re-registering the same namespace/loadName/version
        overwrites and returns the same uri.
        """
        if self.run_id is None:
            raise RunEngineError("no active run; call create_run() first")
        data = self._request(
            "POST",
            f"/runs/{self.run_id}/labware_definitions",
            json_body={"data": definition},
            timeout=self.request_timeout_s,
        )
        uri = data.get("definitionUri")
        if not uri:
            raise RunEngineError(f"labware definition registration returned no uri: {data!r}")
        return uri

    # -- internals ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: float,
    ) -> Any:
        """Issue one request and unwrap the ``{"data": ...}`` envelope."""
        payload = self._request_raw(
            method, path, json_body=json_body, params=params, timeout=timeout
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if data is not None else {}

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: float,
    ) -> Any:
        """Issue one request and return the parsed JSON body verbatim.

        For robot-level endpoints (e.g. ``/robot/lights``) that do not use the
        ``{"data": ...}`` envelope. Maps transport failures to
        :class:`RunEngineUnreachable` and non-2xx to :class:`RunEngineHTTPError`.
        """
        url = self.base_url + path
        try:
            response = self._session.request(
                method,
                url,
                json=json_body,
                params=params or None,
                headers=self._headers,
                timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise RunEngineUnreachable(f"{method} {path}: {exc}") from exc
        except requests.RequestException as exc:
            raise RunEngineError(f"{method} {path}: {exc}") from exc

        if response.status_code >= 400:
            raise RunEngineHTTPError(response.status_code, _error_detail(response), path=path)

        try:
            return response.json()
        except ValueError as exc:
            raise RunEngineError(f"{method} {path}: non-JSON response") from exc


def _error_detail(response: requests.Response) -> str:
    """Pull a human-readable detail out of an Opentrons error response body."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:500] or "<empty body>"
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return first.get("detail") or first.get("title") or str(first)
        if "detail" in body:
            return str(body["detail"])
    return str(body)[:500]
