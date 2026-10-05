"""Opt-in, gateway-local Sartorius peripheral. No hardware I/O from status.

platebalanceV1 is a display/control module, not an Opentrons loadModule model.
Extends the Matter Lab driver with WZB254-N SBI commands (manual §8.5).
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator



logger = logging.getLogger(__name__)
class BalancePipettingGeometry(BaseModel):
    """Operator-qualified geometry for one exact plate definition in a fixed holder."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    load_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    xy_alignment: Literal["slot"]
    seating_height_mm: float = Field(gt=0, le=200)
    rim_height_mm: float = Field(gt=0, le=200)

    @model_validator(mode="after")
    def rim_above_seat(self) -> BalancePipettingGeometry:
        if self.rim_height_mm <= self.seating_height_mm:
            raise ValueError("balance plate rim must be above its seating surface")
        return self

    @staticmethod
    def digest(definition: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode()).hexdigest()

    def compiled_load_name(self) -> str:
        geometry_hash = hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True,
                                                 separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        return f"ac_balance_{geometry_hash[:20]}"

    def compile_definition(self, definition: dict[str, Any]) -> dict[str, Any]:
        """Raise the plate wells into deck coordinates without changing the source."""
        from .assemblies import AssemblyPlate

        validate_balance_plate(definition)
        AssemblyPlate(plate_id="balance", definition=definition)
        if definition.get("parameters", {}).get("loadName") != self.load_name:
            raise ValueError("balance plate load name differs from qualified geometry")
        if self.digest(definition) != self.definition_sha256:
            raise ValueError("balance plate definition differs from qualified geometry")
        if definition.get("cornerOffsetFromSlot") != {"x": 0, "y": 0, "z": 0}:
            raise ValueError("balance plate requires zero cornerOffsetFromSlot")
        source_top = float(definition["dimensions"]["zDimension"])
        if (self.seating_height_mm + source_top > self.rim_height_mm + 1e-6
                or self.rim_height_mm - self.seating_height_mm - source_top > 0.5):
            raise ValueError("measured rim and seating heights disagree with the plate definition")
        result = deepcopy(definition)
        result["namespace"], result["version"] = "custom", 1
        result["parameters"]["loadName"] = self.compiled_load_name()
        result["parameters"]["isMagneticModuleCompatible"] = False
        result["metadata"]["displayName"] = (
            f"{result['metadata'].get('displayName', self.load_name)} · balance · {self.rim_height_mm:g} mm"
        )
        result["dimensions"]["zDimension"] = self.rim_height_mm
        for well in result["wells"].values():
            well["z"] += self.seating_height_mm
        for key in ("stackingOffsetWithLabware", "stackingOffsetWithModule", "gripperOffsets",
                    "gripForce", "gripHeightFromLabwareBottom", "allowedRoles"):
            result.pop(key, None)
        return result


class PlateBalanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["WZB254-N"] = "WZB254-N"
    slot: Literal["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"] = "9"
    # Metadata only until a qualified plate/support geometry is implemented.
    adapter_height_mm: float | None = Field(default=None, gt=0, le=200, allow_inf_nan=False)
    pipetting_geometry: BalancePipettingGeometry | None = None
    # Enabled only after the guarded blow-out path is accepted on this holder.
    balance_blow_out_enabled: bool = False
    max_labware_height_mm: float | None = Field(default=25, gt=0, le=25, allow_inf_nan=False)
    com_port: str | None = Field(default=None, pattern=r"^(COM[1-9][0-9]*|/dev/[A-Za-z0-9_./-]+)$")
    baudrate: int = Field(default=9600, ge=300, le=115200)
    bytesize: Literal[7, 8] = 7
    parity: Literal["none", "odd", "even"] = "odd"
    stopbits: Literal[1, 2] = 1
    timeout: float = Field(default=1.0, gt=0, le=5)
    units: Literal["g"] = "g"


class PlateBalanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wait_until_stable: bool = False
    timeout_s: float = Field(default=10.0, ge=1, le=30, allow_inf_nan=False)
    # Tare only: how many times the tare command may be sent before the step
    # gives up, each followed by its own `timeout_s` baseline wait. A tare the
    # balance applied before the pan settled leaves a stable non-zero
    # baseline; sending tare again re-zeroes on the settled pan. Safe here
    # because nothing is dosed between attempts — the step holds the command
    # lock throughout. Never resent after silence or a stop.
    attempts: int | None = Field(default=None, ge=1, le=3)


_TARE_ATTEMPTS_DEFAULT = 3


class PlateBalanceReferenceRequest(BaseModel):
    """Reference plan steps take no parameters; tare verifies its baseline."""

    model_config = ConfigDict(extra="forbid")


class BalanceStabilityTimeout(RuntimeError):
    """Valid readbacks were received, but stability was not reached in time."""


class BalanceReferenceTimeout(BalanceStabilityTimeout):
    """Tare was sent, but a stable zero baseline was not observed in time."""


class BalanceNoFrame(TimeoutError):
    """A weight query returned no bytes before its serial read timeout."""


_TARE_ZERO_TOLERANCE_G = 0.0002  # Two WZB254-N display increments.
# Floor for the per-read serial timeout while a wait loop runs down its
# deadline. A 22-character SBI frame at 9600 baud takes ~25 ms, at the 1200
# baud factory default ~200 ms; a timeout below that returns the frame cut
# off mid-line ("-   0.0005 g  " with no CR/LF, observed live 2026-10-02),
# which no parser should be asked to read. The loops still stop at their own
# deadline after the read returns.
_MIN_FRAME_READ_S = 0.5


def validate_balance_plate(definition: dict[str, Any], *, max_height_mm: float = 25) -> None:
    """Qualify a single well plate for placement/weight only, never pipetting."""
    from .assemblies import _number

    if (not isinstance(definition.get("parameters"), dict)
            or not isinstance(definition.get("metadata"), dict)
            or definition.get("schemaVersion") != 2
            or definition.get("parameters", {}).get("isTiprack") is not False
            or definition.get("metadata", {}).get("displayCategory") != "wellPlate"):
        raise ValueError("The balance accepts well plates only, not tip racks, reservoirs, or adapters")
    dims = definition.get("dimensions")
    if not isinstance(dims, dict):
        raise ValueError("Balance plate requires a definition with measured dimensions")
    height = _number(dims.get("zDimension"), "Plate height", positive=True)
    if height >= max_height_mm:
        raise ValueError(f"Balance plate height must be below {max_height_mm:g} mm; received {height:g} mm")
    for axis in ("x", "y"):
        _number(dims.get(f"{axis}Dimension"), f"Plate {axis} dimension", positive=True)
    wells, ordering = definition.get("wells"), definition.get("ordering")
    if not isinstance(wells, dict) or not wells or not isinstance(ordering, list) or not ordering:
        raise ValueError("Balance plate requires wells and ordering")
    if any(not isinstance(column, list) or not column for column in ordering):
        raise ValueError("Balance plate ordering must contain nonempty columns")
    names = [name for column in ordering for name in column]
    if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names) or set(names) != set(wells):
        raise ValueError("Balance plate ordering must list each well exactly once")
    for name, well in wells.items():
        if not isinstance(well, dict):
            raise ValueError(f"Invalid well geometry: {name}")
        bottom = _number(well.get("z"), f"{name}.z")
        depth = _number(well.get("depth"), f"{name}.depth", positive=True)
        if bottom + depth > height + 1e-6:
            raise ValueError(f"{name} extends above the declared plate height")


def load_platebalance_config() -> PlateBalanceConfig | None:
    path = os.getenv("OT2_PLATEBALANCE_CONFIG")
    return PlateBalanceConfig.model_validate_json(Path(path).read_text()) if path else None


def matterlab_driver(config: PlateBalanceConfig) -> Any:
    from matterlab_balances import SartoriusBalance
    from matterlab_serial_device import open_close

    class WZB254N(SartoriusBalance):
        @open_close
        def _weigh(self, *, timeout_s: float | None = None):
            previous_timeout = self.device.timeout
            previous_write_timeout = self.device.write_timeout
            try:
                if timeout_s is not None:
                    self.device.timeout = min(config.timeout, max(timeout_s, _MIN_FRAME_READ_S))
                    self.device.write_timeout = min(config.timeout, max(timeout_s, _MIN_FRAME_READ_S))
                # read_until already waits for a complete frame; no fixed delay.
                response = self.query("\x1bP\r\n", num_bytes=64, read_delay=0)
                if response == "":
                    raise BalanceNoFrame("Balance returned no weight frame")
                return parse_weight(response)
            finally:
                self.device.timeout = previous_timeout
                self.device.write_timeout = previous_write_timeout

        @open_close
        def set_reference(self, action):
            # U/V are explicitly TARE/ZERO; T is the combined zero/tare command.
            command = {"tare": "U", "zero": "V"}[action]
            self.write("\x1b" + command + "\r\n")
            time.sleep(1)

    # Defer opening the port until an explicit operator action. The serial
    # dependency version supporting this option is pinned by the extra.
    return WZB254N(**config.model_dump(exclude={"model", "slot", "adapter_height_mm", "pipetting_geometry", "balance_blow_out_enabled", "max_labware_height_mm"}),
                  connect_hardware=False, write_timeout=config.timeout)


def parse_weight(response: str) -> tuple[bool, float]:
    # SBI standard 16-character and labelled 22-character net-weight frames.
    # Never interpret error codes, overload, partial frames, or another unit as g.
    match = re.fullmatch(r" *(?:N +)?([+-]?) *(\d+\.\d+) *(g)? *\r\n", response)
    if not match:
        # The raw frame goes to the gateway log only: a cut-off frame can hold
        # measured digits, and this message travels into /status, halt
        # reasons and exported events, which are not access-controlled.
        logger.warning("unparseable balance frame: %r", response)
        raise ValueError(
            f"Invalid balance weight frame ({len(response)} characters; raw frame in the gateway log)")
    value = float(match[1] + match[2])
    if not math.isfinite(value):
        raise ValueError("Balance returned a non-finite weight")
    return match[3] == "g", value


class PlateBalanceV1:
    code = "platebalanceV1"

    def __init__(self, config: PlateBalanceConfig, *, driver_factory: Callable = matterlab_driver):
        self.config = config
        self.slot = config.slot
        self._factory = driver_factory
        self._driver: Any = None
        self._reading: dict[str, Any] | None = None
        self._error: str | None = None
        self._operation: dict[str, Any] | None = None
        # The run (plan id) on whose behalf the balance is acting, set by the
        # service around each operation; stamped onto each reading and error
        # as it is stored, so their owner is never a guess (run_access.py).
        self.owner: str | None = None
        self._reading_owner: str | None = None
        self._error_owner: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "module_name": self.code, "slot": self.slot, "model": self.config.model,
            "configured": self.config.com_port is not None,
            "state": "unknown",
            "reading": dict(self._reading) if self._reading else None,
            "reading_owner": self._reading_owner if self._reading else None,
            "last_error": self._error,
            "error_owner": self._error_owner if self._error else None,
            "last_operation": dict(self._operation) if self._operation else None,
            "capabilities": {"read": True, "tare": True, "zero": True},
            "geometry": {"adapter_height_mm": self.config.adapter_height_mm,
                         "max_labware_height_mm": self.config.max_labware_height_mm or 25,
                         "pipetting_enabled": self.config.pipetting_geometry is not None,
                         "blow_out_enabled": (self.config.balance_blow_out_enabled
                                              and self.config.pipetting_geometry is not None),
                         "qualified_plate": (self.config.pipetting_geometry.model_dump(mode="json")
                                             if self.config.pipetting_geometry else None)},
        }

    def supports(self, action: str) -> bool:
        return self.config.com_port is not None and action in {"read", "tare", "zero"}

    def execute(self, action: str, *, request: PlateBalanceRequest | None = None,
                cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
        request = request or PlateBalanceRequest(timeout_s=30 if action == "tare" else 10)
        if not self.supports(action):
            raise ValueError("Balance is unconfigured or this operation is unsupported")
        if action == "zero" and request.wait_until_stable:
            raise ValueError("wait_until_stable applies only to read or tare")
        if action != "tare" and request.attempts is not None:
            raise ValueError("attempts applies only to tare")
        attempts_allowed = request.attempts or _TARE_ATTEMPTS_DEFAULT
        attempts_made = 0
        if action != "read":
            self._reading = None  # A pre-tare net weight no longer describes this reference.
        try:
            if self._driver is None:
                self._driver = self._factory(self.config)
            def observe(deadline: float | None = None) -> tuple[bool, float]:
                if cancelled():
                    raise RuntimeError("Balance read interrupted by stop")
                stable, weight = (self._driver._weigh(timeout_s=max(0.001, deadline - time.monotonic()))
                                  if deadline is not None else self._driver._weigh())
                if type(stable) is not bool or not math.isfinite(float(weight)):
                    raise ValueError("Balance returned an invalid weight or stability flag")
                self._reading = {"value": float(weight), "unit": "g", "stable": stable,
                                 "observed_at": datetime.now(timezone.utc).isoformat()}
                self._reading_owner = self.owner
                if cancelled():
                    raise RuntimeError("Balance read interrupted by stop")
                return stable, float(weight)

            if action == "read":
                deadline = time.monotonic() + request.timeout_s
                unreadable: str | None = None
                while True:
                    if request.wait_until_stable and time.monotonic() >= deadline:
                        raise BalanceStabilityTimeout(
                            f"Weight did not stabilize within {request.timeout_s:g} s"
                            + (f"; last frame was unreadable: {unreadable}" if unreadable else "")
                        )
                    # Repeat valid unstable readings and, while waiting, frames
                    # the parser rejects (a frame cut off at the read timeout,
                    # an overload or error code): none of them is a weight, and
                    # a one-shot read still fails loudly on them.
                    try:
                        stable, weight = observe(deadline if request.wait_until_stable else None)
                    except ValueError as exc:
                        if not request.wait_until_stable:
                            raise
                        unreadable = str(exc)
                        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                        continue
                    unreadable = None
                    if request.wait_until_stable and time.monotonic() >= deadline:
                        raise BalanceStabilityTimeout(f"Weight did not stabilize within {request.timeout_s:g} s")
                    if not request.wait_until_stable or stable:
                        break
                    time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                outcome = "observed"
            elif action == "zero":
                self._driver.set_reference(action)  # one write, never repeat automatically
                attempts_made = 1
                outcome = "sent_unconfirmed"  # zero has no acknowledgment
            else:
                # Tare: the serial write is not an acknowledgment. Observe two
                # fresh, stable near-zero values before the next plan step. A
                # baseline that settles off zero (the balance tared before the
                # pan was still) earns another attempt, up to `attempts`;
                # silence through a whole wait, or a stop, never does.
                while True:
                    self._driver.set_reference(action)
                    attempts_made += 1
                    deadline = time.monotonic() + request.timeout_s
                    consecutive = 0
                    no_frame_seen = False
                    unreadable = None
                    try:
                        while True:
                            if time.monotonic() >= deadline:
                                if no_frame_seen:
                                    raise TimeoutError(
                                        f"Tare sent, but no frame was available at the {request.timeout_s:g} s deadline"
                                    )
                                raise BalanceReferenceTimeout(
                                    f"Tare sent, but stable zero was not observed within {request.timeout_s:g} s"
                                    + (f"; last frame was unreadable: {unreadable}" if unreadable else "")
                                )
                            try:
                                stable, weight = observe(deadline)
                            except BalanceNoFrame:
                                # The scale can be silent while its tare is still
                                # settling. Keep the robot stationary and query
                                # again, but never resend the reference command
                                # inside a wait.
                                no_frame_seen = True
                                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                                continue
                            except ValueError as exc:
                                # The scale answered, so it is alive and the tare
                                # was delivered; this frame just is not a weight
                                # (cut off at the read timeout, overload, error
                                # code). Query again — the baseline needs two
                                # stable near-zero readings, so an unreadable
                                # frame can only cost time, never be mistaken
                                # for one.
                                no_frame_seen = False
                                unreadable = str(exc)
                                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                                continue
                            no_frame_seen = False
                            unreadable = None
                            if time.monotonic() >= deadline:
                                raise BalanceReferenceTimeout(
                                    f"Tare sent, but stable zero was not observed within {request.timeout_s:g} s"
                                )
                            consecutive = consecutive + 1 if stable and abs(weight) <= _TARE_ZERO_TOLERANCE_G else 0
                            if consecutive >= 2:
                                break
                            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                    except BalanceReferenceTimeout as exc:
                        if attempts_made >= attempts_allowed:
                            raise BalanceReferenceTimeout(
                                f"{exc} (after {attempts_made} tare attempt{'s' if attempts_made > 1 else ''})"
                            ) from None
                        continue
                    outcome = "baseline_observed"
                    break
            self._error = None
            self._operation = {"action": action, "outcome": outcome,
                               "at": datetime.now(timezone.utc).isoformat()}
            if action != "read":
                self._operation["attempts"] = attempts_made
        except Exception as exc:
            self._error = str(exc)
            self._error_owner = self.owner
            self._operation = {"action": action, "outcome": ("baseline_unconfirmed" if isinstance(exc, BalanceReferenceTimeout)
                               else "stability_timeout" if isinstance(exc, BalanceStabilityTimeout)
                               else "failed" if action == "read" else "unknown_outcome"),
                               "at": datetime.now(timezone.utc).isoformat()}
            if action != "read":
                self._operation["attempts"] = attempts_made
            if isinstance(exc, BalanceReferenceTimeout):
                raise
            if action != "read":
                raise OSError(f"Balance {action} outcome is unknown: {exc}") from exc
            raise
        return self.snapshot()
