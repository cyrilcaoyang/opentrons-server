"""High-level OT-2/Flex command wrapper.

Drives an Opentrons protocol session over SSH via a Python REPL on the
robot. ``OT2Control`` is the canonical class; ``OpentronsControl`` is
preserved as an alias for back-compat with older callers.

Remote calls are built through two small helper layers:

- **Typed readback** (``_invoke_scalar_line`` / ``_invoke_float`` /
  ``_invoke_bool``) parses the REPL transcript (echoed command, printed
  value, trailing ``>>>`` prompt) into a Python value instead of ad-hoc
  ``split("\\r\\n")[-2]`` at every call site.
- **Kwargs formatting** (``_format_kwargs``) renders optional keyword
  arguments into remote-Python source, skipping ``None`` values so the
  invoked string stays minimal. ``_RAW_LOCATION`` marks the remote
  ``location`` variable so it is emitted unquoted.
"""

from __future__ import annotations

import os
import keyword
from typing import Any, Dict, List, Optional

from ..transport import SSHClient


class _Raw(str):
    """A pre-formatted remote Python expression — emitted verbatim, not repr'd."""


_RAW_LOCATION = _Raw("location")


class OT2Control:
    def __init__(self, host_alias: str = None, password: str = "", simulation: bool = False):
        self._connect(host_alias, password)
        self._get_protocol(simulation)

    def _connect(self, host_alias: str = None, password: str = ""):
        command_timeout = int(os.getenv("OT2_SSH_COMMAND_TIMEOUT", "120"))

        self.client = SSHClient(
            hostname=os.getenv("HOSTNAME"),
            username=os.getenv("USERNAME"),
            key_file_path=os.getenv("KEY_FILE_PATH"),
            host_alias=host_alias,
            password=password,
            command_timeout=command_timeout,
        )
        # SSHClient.connect() returns False after exhausting its retries rather
        # than raising, so discarding the result let construction continue into
        # _get_protocol(), where the first invoke() failed with the misleading
        # "SSH client is not connected". Raise the real thing here instead; the
        # per-attempt cause is logged by SSHClient.connect at WARNING.
        if not self.client.connect():
            raise ConnectionError(
                f"SSH connect to {self.client.hostname!r} failed after "
                f"{self.client.max_retries} attempts "
                f"({self.client.connection_timeout}s timeout each); "
                "see the gateway log for the per-attempt reason"
            )

    def invoke(self, code: str) -> str:
        """Execute Python code on the robot via SSH."""
        if not self.client.is_connected:
            raise Exception("SSH client is not connected")

        if self.client.session_state.value != "python":
            self.client.start_python_session()

        return self.client.execute_python_command(code)

    def _disconnect(self) -> None:
        self.client.close()

    def _get_protocol(self, simulation: bool) -> None:
        self.invoke("from opentrons.types import Point, Location")
        self.invoke("from opentrons import protocol_api")
        self.invoke("import json")
        if simulation:
            self.invoke("from opentrons import simulate")
            self.invoke("protocol = simulate.get_protocol_api('2.21')")
        else:
            self.invoke("from opentrons import execute")
            self.invoke("protocol = execute.get_protocol_api('2.21')")

    def initialize_protocol(self, simulation: bool = False) -> None:
        """Initialize or reinitialize the remote Opentrons protocol context."""
        self._get_protocol(simulation)

    def shutdown(self) -> None:
        """Close the active robot session without issuing extra workflow logic."""
        self._disconnect()

    # ---- remote-call helpers ----------------------------------------------

    def _invoke_lines(self, code: str) -> List[str]:
        return self.invoke(code).split("\r\n")

    def _invoke_scalar_line(self, code: str) -> str:
        """The printed value of a single-expression invoke.

        The transcript is ``[echoed command, printed value, '>>> ' prompt]``,
        so the value is the second-to-last line.
        """
        return self._invoke_lines(code)[-2].strip()

    def _invoke_float(self, code: str) -> float:
        return float(self._invoke_scalar_line(code))

    def _invoke_bool(self, code: str) -> bool:
        value = self._invoke_scalar_line(code)
        if value == "True":
            return True
        if value == "False":
            return False
        raise ValueError(f"Expected bool-like SSH response, got: {value!r}")

    @staticmethod
    def _py_repr(value: Any) -> str:
        if isinstance(value, _Raw):
            return str(value)
        return "None" if value is None else repr(value)

    def _format_kwargs(self, **kwargs: Any) -> str:
        """Render kwargs as remote-Python source, skipping None values."""
        return ", ".join(
            f"{key} = {self._py_repr(value)}"
            for key, value in kwargs.items()
            if value is not None
        )

    # ---- protocol setup -----------------------------------------------------

    def setup_protocol(
        self,
        *,
        labware: Optional[List[Dict[str, Any]]] = None,
        instruments: Optional[List[Dict[str, Any]]] = None,
        modules: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Load labware, instruments, and modules into the active protocol."""
        for labware_config in labware or []:
            self.load_labware(labware_config)
        for instrument_config in instruments or []:
            self.load_instrument(instrument_config)
        for module_config in modules or []:
            self.load_module(module_config)

    @staticmethod
    def _validated_identifier(name: str) -> str:
        if (not isinstance(name, str) or not name.isascii() or not name.isidentifier()
                or keyword.iskeyword(name) or name in {"protocol", "json", "Point", "Location", "location"}):
            raise ValueError("nickname must be a non-reserved Python identifier")
        return name

    def _load_custom_labware(self, nickname: str, labware_config: Dict, location: str):
        nickname = self._validated_identifier(nickname)
        # The definition is data, including its loadName. Never use a name
        # inside a custom definition as a Python assignment target.
        self.invoke(f"{nickname} = protocol.load_labware_from_definition(labware_def = {labware_config!r}, location = {location!r})")

    def _load_default_labware(self, nickname: str, loadname: str, location: str):
        nickname = self._validated_identifier(nickname)
        self.invoke(f"{nickname} = protocol.load_labware(load_name = {loadname!r}, location = {location!r})")

    def _load_default_instrument(self, nickname: str, instrument_name: str, mount: str):
        nickname = self._validated_identifier(nickname)
        self.invoke(f"{nickname} = protocol.load_instrument(instrument_name = {instrument_name!r}, mount = {mount!r})")

    def _load_custom_instrument(self, nickname: str, instrument_config: Dict, mount: str):
        raise NotImplementedError("custom instrument not implemented")

    def _setup_device_metadata(self):
        self.invoke("p300.well_bottom_clearance.dispense=10")

    def load_labware(self, labware: Dict):
        if labware["ot_default"]:
            self._load_default_labware(nickname=labware["nickname"], loadname=labware["loadname"], location=labware["location"])
        else:
            self._load_custom_labware(nickname=labware["nickname"], labware_config=labware["config"], location=labware["location"])

    def load_instrument(self, instrument: Dict):
        if instrument["ot_default"]:
            self._load_default_instrument(nickname=instrument["nickname"], instrument_name=instrument["instrument_name"], mount=instrument["mount"])
        else:
            self._load_custom_instrument(nickname=instrument["nickname"], instrument_config=instrument["config"], mount=instrument["mount"])

    def load_module(self, module: Dict):
        nickname = self._validated_identifier(module["nickname"])
        module_name = module["module_name"]
        location = module["location"]
        adapter = module.get("adapter")
        self.invoke(f"{nickname} = protocol.load_module(module_name = {module_name!r}, location = {location!r})")
        if adapter:
            self.invoke(f"{nickname}_adapter = {nickname}.load_adapter(name = {adapter!r})")

    def load_trash_bin(self, nickname: str = "default_trash", location: str = "A3"):
        """Flex only; the OT-2's fixed trash is always present in slot 12."""
        nickname = self._validated_identifier(nickname)
        self.invoke(f"{nickname} = protocol.load_trash_bin(location = {location!r})")

    def remove_labware(self, labware_nickname: str):
        self.invoke(f"deck_pos = {labware_nickname}.parent")
        self.invoke(f"del protocol.deck[deck_pos]")

    # ---- protocol-level controls ---------------------------------------------

    def home(self):
        self.invoke("protocol.home()")

    def comment(self, message: str):
        self.invoke(f"protocol.comment({self._py_repr(message)})")

    def set_rail_lights(self, on: bool = True):
        self.invoke(f"protocol.set_rail_lights({self._py_repr(bool(on))})")

    def get_rail_lights(self) -> bool:
        return self._invoke_bool("protocol.rail_lights_on")

    def set_max_speed(self, axis: str, speed: float):
        # axis examples: 'X', 'Y', 'Z', 'A'
        self.invoke(f"protocol.max_speeds[{self._py_repr(axis)}] = {speed}")

    def clear_max_speed(self, axis: str):
        self.invoke(f"protocol.max_speeds[{self._py_repr(axis)}] = None")

    def delay(self, seconds: float = 0, minutes: float = 0):
        self.invoke(f"protocol.delay(seconds={seconds}, minutes = {minutes})")

    def resume(self):
        self.invoke("protocol.resume()")

    def pause(self):
        self.invoke("protocol.pause()")

    # ---- labware geometry readbacks -------------------------------------------

    def well_diameter(self, labware_nickname: str, position: str) -> float:
        return self._invoke_float(f"{labware_nickname}['{position}'].diameter")

    def well_depth(self, labware_nickname: str, position: str) -> float:
        return self._invoke_float(f"{labware_nickname}['{position}'].depth")

    def tip_length(self, labware_nickname: str, position: str) -> Optional[float]:
        rtn = self._invoke_lines(f"{labware_nickname}['{position}'].length")
        if len(rtn) == 3:
            return float(rtn[-2])
        return None

    # ---- locations -------------------------------------------------------------

    def get_location_from_labware(
        self,
        labware_nickname: str,
        position: str,
        top: float = 0,
        bottom: float = 0,
        center: float = 0,
        default_origin: str = "top",
        default_offset: float = 0,
    ):
        """Stash a well location. ``default_origin`` / ``default_offset`` decide
        the reference when the caller names none — the *action* owns that
        choice, not this helper (an aspirate defaults to the well bottom, a
        move to its top), so both are passed in rather than assumed here."""
        if top:
            append = f".top({top})"
        elif bottom:
            append = f".bottom({bottom})"
        elif center:
            append = ".center()"
        elif default_origin == "center":
            append = ".center()"
        else:
            append = f".{default_origin}({default_offset})"
        self.invoke(f"location = {labware_nickname}['{position}']{append}")

    def get_location_absolute(self, x: float, y: float, z: float, reference: str = None):
        self.invoke(f"location = Location(Point({x},{y},{z}), '{str(reference)}')")

    def move_to_pip(
        self,
        pip_name: str,
        *,
        speed: Optional[float] = None,
        force_direct: Optional[bool] = None,
        minimum_z_height: Optional[float] = None,
    ):
        kwargs = self._format_kwargs(
            location=_RAW_LOCATION,
            speed=speed,
            force_direct=force_direct,
            minimum_z_height=minimum_z_height,
        )
        self.invoke(f"{pip_name}.move_to({kwargs})")

    # ---- tips --------------------------------------------------------------------

    def pick_up_tip(
        self,
        pip_name: str,
        *,
        presses: Optional[int] = None,
        increment: Optional[float] = None,
        prep_after: Optional[bool] = None,
    ):
        kwargs = self._format_kwargs(
            location=_RAW_LOCATION,
            presses=presses,
            increment=increment,
            prep_after=prep_after,
        )
        self.invoke(f"{pip_name}.pick_up_tip({kwargs})")

    def return_tip(self, pip_name: str, *, home_after: Optional[bool] = None):
        kwargs = self._format_kwargs(home_after=home_after)
        self.invoke(f"{pip_name}.return_tip({kwargs})")

    def drop_tip(self, pip_name: str, *, home_after: Optional[bool] = None):
        kwargs = self._format_kwargs(home_after=home_after)
        self.invoke(f"{pip_name}.drop_tip({kwargs})")

    def has_tip(self, pip_name: str) -> bool:
        return self._invoke_bool(f"{pip_name}.has_tip")

    def set_starting_tip(self, pip_name: str, tiprack_nickname: str, position: str):
        self.invoke(f"{pip_name}.starting_tip = {tiprack_nickname}['{position}']")

    def reset_tipracks(self, pip_name: str):
        self.invoke(f"{pip_name}.reset_tipracks()")

    # ---- liquid handling ------------------------------------------------------------

    def prepare_aspirate(self, pip_name: str):
        self.invoke(f"{pip_name}.prepare_to_aspirate()")

    def aspirate(
        self,
        pip_name: str,
        volume: float,
        *,
        rate: Optional[float] = None,
        flow_rate: float = None,
    ):
        # flow_rate (µL/s) is optional; when omitted the pipette keeps its
        # protocol-API default, so the invoke is byte-for-byte unchanged.
        # rate is the protocol-API multiplier on that flow rate.
        if flow_rate is not None:
            self.invoke(f"{pip_name}.flow_rate.aspirate = {flow_rate}")
        kwargs = self._format_kwargs(volume=volume, location=_RAW_LOCATION, rate=rate)
        self.invoke(f"{pip_name}.aspirate({kwargs})")

    def dispense(
        self,
        pip_name: str,
        volume: float,
        push_out: float = None,
        *,
        rate: Optional[float] = None,
        flow_rate: float = None,
    ):
        if flow_rate is not None:
            self.invoke(f"{pip_name}.flow_rate.dispense = {flow_rate}")
        kwargs = self._format_kwargs(
            volume=volume, location=_RAW_LOCATION, rate=rate, push_out=push_out
        )
        self.invoke(f"{pip_name}.dispense({kwargs})")

    def mix(
        self,
        pip_name: str,
        repetitions: int,
        volume: Optional[float] = None,
        rate: Optional[float] = None,
    ):
        kwargs = self._format_kwargs(
            repetitions=repetitions, volume=volume, location=_RAW_LOCATION, rate=rate
        )
        self.invoke(f"{pip_name}.mix({kwargs})")

    def air_gap(self, pip_name: str, volume: float, height: Optional[float] = None):
        kwargs = self._format_kwargs(volume=volume, height=height)
        self.invoke(f"{pip_name}.air_gap({kwargs})")

    def touch_tip(
        self,
        pip_name: str,
        labware_nickname: str,
        position: str,
        radius: float = 1.0,
        v_offset: float = -1.0,
        speed: float = 60.0,
    ):
        self.invoke(
            f"{pip_name}.touch_tip({labware_nickname}['{position}'], "
            f"radius = {radius}, v_offset = {v_offset}, speed = {speed})"
        )

    def blow_out(self, pip_name: str):
        self.invoke(f"{pip_name}.blow_out(location = location)")

    def blow_out_in_place(self, pip_name: str):
        self.invoke(f"{pip_name}.blow_out()")

    # ---- pipette configuration ---------------------------------------------------------

    def set_speed(self, pip_name: str, speed: float):
        self.invoke(f"{pip_name}.default_speed = {speed}")

    def set_flow_rate(
        self,
        pip_name: str,
        aspirate: Optional[float] = None,
        dispense: Optional[float] = None,
        blow_out: Optional[float] = None,
    ):
        if aspirate is not None:
            self.invoke(f"{pip_name}.flow_rate.aspirate = {aspirate}")
        if dispense is not None:
            self.invoke(f"{pip_name}.flow_rate.dispense = {dispense}")
        if blow_out is not None:
            self.invoke(f"{pip_name}.flow_rate.blow_out = {blow_out}")

    def get_flow_rate(self, pip_name: str) -> Dict[str, float]:
        return {
            "aspirate": self._invoke_float(f"{pip_name}.flow_rate.aspirate"),
            "dispense": self._invoke_float(f"{pip_name}.flow_rate.dispense"),
            "blow_out": self._invoke_float(f"{pip_name}.flow_rate.blow_out"),
        }

    def set_well_bottom_clearance(
        self,
        pip_name: str,
        aspirate: Optional[float] = None,
        dispense: Optional[float] = None,
    ):
        if aspirate is not None:
            self.invoke(f"{pip_name}.well_bottom_clearance.aspirate = {aspirate}")
        if dispense is not None:
            self.invoke(f"{pip_name}.well_bottom_clearance.dispense = {dispense}")

    def get_well_bottom_clearance(self, pip_name: str) -> Dict[str, float]:
        return {
            "aspirate": self._invoke_float(f"{pip_name}.well_bottom_clearance.aspirate"),
            "dispense": self._invoke_float(f"{pip_name}.well_bottom_clearance.dispense"),
        }

    def current_volume(self, pip_name: str) -> float:
        return self._invoke_float(f"{pip_name}.current_volume")

    def home_pipette(self, pip_name: str):
        self.invoke(f"{pip_name}.home()")

    def home_plunger(self, pip_name: str):
        self.invoke(f"{pip_name}.home_plunger()")

    # ---- labware movement -------------------------------------------------------------

    def move_labware_w_gripper(self, labware_nickname: str, new_location: str):
        if new_location == "OFF_DECK":
            self.invoke(f"protocol.move_labware(labware = {labware_nickname}, new_location = protocol_api.OFF_DECK, use_gripper = True)")
        elif "adapter" in new_location:
            self.invoke(f"protocol.move_labware(labware = {labware_nickname}, new_location = {new_location}, use_gripper = True)")
        else:
            self.invoke(f"protocol.move_labware(labware = {labware_nickname}, new_location = '{new_location}', use_gripper = True)")

    def move_labware(self, labware_nickname: str, new_location: str) -> None:
        """Move labware using the robot gripper when available."""
        self.move_labware_w_gripper(labware_nickname, new_location)

    # ---- heater-shaker module ----------------------------------------------------------

    def hs_latch_open(self, nickname: str):
        self.invoke(f"{nickname}.open_labware_latch()")

    def hs_latch_close(self, nickname: str):
        self.invoke(f"{nickname}.close_labware_latch()")

    def hs_set_and_wait_shake_speed(self, nickname: str, rpm: int):
        self.invoke(f"{nickname}.set_and_wait_for_shake_speed(rpm = {rpm})")

    def hs_deactivate_shaker(self, nickname: str):
        self.invoke(f"{nickname}.deactivate_shaker()")

    def hs_set_and_wait_temperature(self, nickname: str, celsius: float):
        self.invoke(f"{nickname}.set_and_wait_for_temperature(celsius = {celsius})")

    def hs_set_target_temperature(self, nickname: str, celsius: float):
        self.invoke(f"{nickname}.set_target_temperature(celsius = {celsius})")

    def hs_wait_for_temperature(self, nickname: str):
        self.invoke(f"{nickname}.wait_for_temperature()")

    def hs_deactivate_heater(self, nickname: str):
        self.invoke(f"{nickname}.deactivate_heater()")

    def hs_deactivate(self, nickname: str):
        self.invoke(f"{nickname}.deactivate()")

    def set_rpm(self, nickname: str, rpm: int):
        """Set-and-wait shake speed; out-of-band values deactivate the shaker."""
        if 200 <= rpm <= 3000:
            self.hs_set_and_wait_shake_speed(nickname, rpm)
        else:
            self.hs_deactivate_shaker(nickname)

    def set_temp(self, nickname: str, temp: float):
        """Set-and-wait heater temperature; out-of-band values deactivate it."""
        if 27 <= temp <= 95:
            self.hs_set_and_wait_temperature(nickname, temp)
        else:
            self.hs_deactivate_heater(nickname)

    def get_rpm(self, nickname: str) -> float:
        return self._invoke_float(f"{nickname}.current_speed")

    def get_temp(self, nickname: str) -> float:
        return self._invoke_float(f"{nickname}.current_temperature")

    # ---- temperature module -----------------------------------------------------------

    def tempmod_start_set_temperature(self, nickname: str, celsius: float):
        """Set the target without waiting. Public ``set_temperature`` blocks.

        Protocol API < 2.13 exposed ``start_set_temperature``; later cores keep
        the same non-blocking call on ``_core``. One expression so a single
        REPL invoke covers both.
        """
        self.invoke(
            f"{nickname}.start_set_temperature(celsius = {celsius}) "
            f"if hasattr({nickname}, 'start_set_temperature') else "
            f"{nickname}._core.set_target_temperature({celsius})"
        )

    def tempmod_set_temperature(self, nickname: str, celsius: float):
        self.invoke(f"{nickname}.set_temperature(celsius = {celsius})")

    def tempmod_await_temperature(self, nickname: str):
        self.invoke(f"{nickname}.await_temperature()")

    def tempmod_deactivate(self, nickname: str):
        self.invoke(f"{nickname}.deactivate()")

    # ---- magnetic module --------------------------------------------------------------

    def magmod_engage(
        self,
        nickname: str,
        height_from_base: Optional[float] = None,
        offset: Optional[float] = None,
    ):
        kwargs = self._format_kwargs(height_from_base=height_from_base, offset=offset)
        self.invoke(f"{nickname}.engage({kwargs})")

    def magmod_disengage(self, nickname: str):
        self.invoke(f"{nickname}.disengage()")

    # ---- thermocycler module ------------------------------------------------------------

    def thermocycler_open_lid(self, nickname: str):
        self.invoke(f"{nickname}.open_lid()")

    def thermocycler_close_lid(self, nickname: str):
        self.invoke(f"{nickname}.close_lid()")

    def thermocycler_open_labware_latch(self, nickname: str):
        self.invoke(f"{nickname}.open_labware_latch()")

    def thermocycler_close_labware_latch(self, nickname: str):
        self.invoke(f"{nickname}.close_labware_latch()")

    def thermocycler_set_block_temperature(
        self,
        nickname: str,
        temperature: float,
        hold_time_seconds: Optional[float] = None,
        hold_time_minutes: Optional[float] = None,
        block_max_volume: Optional[float] = None,
        ramp_rate: Optional[float] = None,
    ):
        kwargs = self._format_kwargs(
            temperature=temperature,
            hold_time_seconds=hold_time_seconds,
            hold_time_minutes=hold_time_minutes,
            block_max_volume=block_max_volume,
            ramp_rate=ramp_rate,
        )
        self.invoke(f"{nickname}.set_block_temperature({kwargs})")

    def thermocycler_set_lid_temperature(self, nickname: str, temperature: float):
        self.invoke(f"{nickname}.set_lid_temperature(temperature = {temperature})")

    def thermocycler_deactivate_block(self, nickname: str):
        self.invoke(f"{nickname}.deactivate_block()")

    def thermocycler_deactivate_lid(self, nickname: str):
        self.invoke(f"{nickname}.deactivate_lid()")

    def thermocycler_deactivate(self, nickname: str):
        self.invoke(f"{nickname}.deactivate()")

    # ---- session ---------------------------------------------------------------------

    def close_session(self):
        self.home()
        self._disconnect()


OpentronsControl = OT2Control

__all__ = ["OT2Control", "OpentronsControl"]
