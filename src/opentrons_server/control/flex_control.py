"""Flex HTTP commands, based on Opentrons v8.7.0 Protocol Engine schemas.

The OT-2 adapter remains unchanged. This adapter supports full 1/8-channel
layouts; 96-channel and partial nozzle layouts need a different tracking model.
"""
import re
from typing import Any, Dict, Optional

from .http_control import OT2HttpControl
from .http_run import RunEngineCommands, RunEngineError


class FlexHttpControl(OT2HttpControl):
    def initialize_protocol(self, simulation: bool = False) -> None:
        health = self.client._request_raw("GET", "/health", timeout=self.client.request_timeout_s)
        model = str(health.get("robot_model") or "").lower()
        if not any(name in model for name in ("flex", "ot-3", "ot3")):
            raise RunEngineError(f"Flex profile requires observed Flex identity, got {model!r}")
        # Fail before creating a run when the target server lacks this surface.
        schema = self.client._request_raw("GET", "/openapi.json", timeout=self.client.request_timeout_s)
        schemas = schema.get("components", {}).get("schemas", {})
        commands = set()
        for item in schemas.values():
            command = item.get("properties", {}).get("commandType", {})
            commands.update(command.get("enum", []))
            if "const" in command:
                commands.add(command["const"])
        required = {"moveLabware", "robot/moveTo", "robot/openGripperJaw", "robot/closeGripperJaw", "robot/moveAxesRelative", "robot/moveAxesTo"}
        if missing := required - commands:
            raise RunEngineError(f"Flex server is missing required commands: {sorted(missing)}")
        super().initialize_protocol(simulation)

    def setup_protocol(self, *, labware=None, instruments=None, modules=None) -> None:
        # Preflight every head before any setup command can partially execute.
        for config in instruments or []:
            self._validate_instrument(config)
        for config in modules or []:
            self.load_module(config)
        for config in labware or []:
            if config.get("loadname") == "trash_bin":
                self.load_trash_bin(config.get("nickname", "default_trash"), config["location"])
            else:
                self.load_labware(config)
        for config in instruments or []:
            self.load_instrument(config)
        self.adopt_run_state()

    @staticmethod
    def _validate_instrument(config: Dict[str, Any]) -> None:
        if not re.fullmatch(r"flex_(1|8)channel_(50|1000)", config["instrument_name"]):
            raise ValueError("Flex currently supports full 1/8-channel layouts only; 96-channel/partial layouts are not supported")

    def load_instrument(self, instrument: Dict[str, Any]) -> str:
        self._validate_instrument(instrument)
        return super().load_instrument(instrument)

    def load_trash_bin(self, nickname: str = "default_trash", location: str = "A3") -> None:
        if not re.fullmatch(r"[A-D][13]", location):
            raise ValueError("Flex trash bins occupy A-D, column 1 or 3")
        # Registration only; the engine checks the actual deck configuration
        # when a drop is attempted. No automatic assumption of a present bin.
        self._trash_area = f"movableTrash{location}"

    def drop_tip(self, pip_name: str, *, home_after: Optional[bool] = None) -> None:
        if self._pending is None and self._trash_area is None and self._trash_nickname is None:
            raise ValueError("Flex requires an explicit drop well or a configured trash bin")
        super().drop_tip(pip_name, home_after=home_after)

    def move_labware_w_gripper(self, labware_nickname: str, new_location: Any,
                              pick_up_offset=None, drop_offset=None) -> None:
        location = self._location(new_location)
        if location == "offDeck":
            raise ValueError("gripper needs a physical destination; OFF_DECK is a manual relocation")
        params = {"labwareId": self._labware_id(labware_nickname), "newLocation": location,
                  "strategy": "usingGripper"}
        if pick_up_offset is not None:
            params["pickUpOffset"] = pick_up_offset
        if drop_offset is not None:
            params["dropOffset"] = drop_offset
        self.client.execute(("moveLabware", params))

    def gripper_open_jaw(self) -> None:
        self.client.execute(("robot/openGripperJaw", {}))

    def gripper_close_jaw(self) -> None:
        # Use the robot default force; no unvalidated arbitrary force override.
        self.client.execute(("robot/closeGripperJaw", {}))

    def gripper_move_to_absolute(self, x: float, y: float, z: float, speed: float = 50,
                                 force_direct: bool = True) -> None:
        if force_direct:
            # robot/moveTo plans an arc (direct=False in MovementHandler).
            # Axis moves apply the robot's own mount/critical-point offsets and
            # preserve the sample-prep wrapper's direct hardware.move_to path.
            command = ("robot/moveAxesTo", {"axis_map": {"x": x, "y": y, "extensionZ": z}, "speed": speed})
        else:
            command = ("robot/moveTo", {"mount": "extension", "destination": {"x": x, "y": y, "z": z}, "speed": speed})
        self.client.execute(command)

    def gripper_move_to_relative(self, dx: float = 0, dy: float = 0, dz: float = 0, speed: float = 50) -> None:
        self.client.execute(("robot/moveAxesRelative", {
            "axis_map": {"x": dx, "y": dy, "extensionZ": dz}, "speed": speed,
        }))

    def _location(self, new_location: Any) -> Any:
        if isinstance(new_location, str):
            if new_location in self._module_ids:
                return {"moduleId": self._module_id(new_location)}
            if new_location in self._labware_ids:
                return {"labwareId": self._labware_id(new_location)}
        return super()._location(new_location)

    def home_gripper(self) -> None:
        self.client.execute(RunEngineCommands.home(axes=["extensionZ"]))
