"""Typed, allowlisted operations shared by routes, plans and the service.

These are gateway operations, never arbitrary Python method dispatch. Transport
methods that lack working HTTP semantics deliberately do not appear here.
"""

from dataclasses import dataclass
from typing import Optional

from pydantic import Field, model_validator

from .limits import MAX_PIPETTE_VOLUME_UL, MAX_WELL_OFFSET_MM
from .models import LiquidMoveRequest, StrictRequest, WellLocation, RobotReference
from .robot_profile import IS_FLEX


class PipetteRequest(StrictRequest):
    pipette: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class BlowOutRequest(PipetteRequest):
    """Expel residual liquid at an explicit well, or explicitly in place."""

    location: Optional[WellLocation] = None
    in_place: bool = False

    @model_validator(mode="after")
    def target(self):
        if (self.location is not None) == self.in_place:
            raise ValueError("provide a location or in_place=true, exclusively")
        return self


class MixRequest(LiquidMoveRequest):
    repetitions: int = Field(ge=1, le=1000)
    rate: float = Field(default=1, gt=0, le=10)

    @model_validator(mode="after")
    def no_flow_rate(self):
        if self.flow_rate is not None:
            raise ValueError("mix uses rate; set individual flows with set_flow_rate first")
        return self


class AirGapRequest(PipetteRequest):
    """Draw air above an explicit well; never depend on another call's location."""

    location: WellLocation
    volume_ul: float = Field(gt=0, le=MAX_PIPETTE_VOLUME_UL)
    height: float = Field(default=5, ge=0, le=MAX_WELL_OFFSET_MM)

    @model_validator(mode="after")
    def no_well_offsets(self):
        if self.location.top is not None or self.location.bottom is not None or self.location.center:
            raise ValueError("air_gap uses height above the well top; omit location offsets")
        return self


class TouchTipRequest(PipetteRequest):
    labware_nickname: RobotReference = Field(min_length=1)
    position: str = Field(pattern=r"^[A-Z]+[1-9][0-9]*$")
    radius: float = Field(default=1, gt=0, le=1)
    v_offset: float = Field(default=-1, ge=-MAX_WELL_OFFSET_MM, le=0)
    speed: float = Field(default=60, ge=1, le=80)


class FlowRateRequest(PipetteRequest):
    aspirate: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    dispense: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    blow_out: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def at_least_one(self):
        if all(getattr(self, name) is None for name in ("aspirate", "dispense", "blow_out")):
            raise ValueError("provide at least one flow rate in uL/s")
        return self


class PipetteSpeedRequest(PipetteRequest):
    speed: float = Field(gt=0, le=400, description="Explicit gantry move speed in mm/s.")


class ModuleRequest(StrictRequest):
    module: RobotReference = Field(min_length=1, description="Loaded module nickname or declared deck slot.")


class HeaterShakerTemperatureRequest(ModuleRequest):
    celsius: float = Field(ge=37, le=95)


class ShakeSpeedRequest(ModuleRequest):
    rpm: int = Field(ge=200, le=3000)


class MagnetEngageRequest(ModuleRequest):
    # Common conservative envelope; firmware enforces the installed generation.
    height_from_base: float = Field(ge=0, le=20, description="Height above labware base in mm.")


class BlockTemperatureRequest(ModuleRequest):
    temperature: float = Field(ge=4, le=99)
    hold_time_seconds: Optional[float] = Field(default=None, ge=0, le=86400)
    block_max_volume: Optional[float] = Field(default=None, gt=0, le=100)


class LidTemperatureRequest(ModuleRequest):
    temperature: float = Field(ge=37, le=110)


class CommentRequest(StrictRequest):
    message: str = Field(min_length=1, max_length=2000)


class DelayRequest(StrictRequest):
    seconds: float = Field(ge=0, le=86400)


@dataclass(frozen=True)
class AdvancedAction:
    model: type[StrictRequest]
    method: str
    family: Optional[str] = None
    idempotent: bool = False

    @property
    def path(self) -> str:
        return "/control/" + self.method.replace("_", "-")


ADVANCED_ACTIONS = {
    "comment": AdvancedAction(CommentRequest, "comment", idempotent=True),
    "delay": AdvancedAction(DelayRequest, "delay"),
    "blow_out": AdvancedAction(BlowOutRequest, "blow_out"),
    "touch_tip": AdvancedAction(TouchTipRequest, "touch_tip"),
    "mix": AdvancedAction(MixRequest, "mix"),
    "air_gap": AdvancedAction(AirGapRequest, "air_gap"),
    "prepare_aspirate": AdvancedAction(PipetteRequest, "prepare_aspirate"),
    "home_pipette": AdvancedAction(PipetteRequest, "home_pipette", idempotent=True),
    "home_plunger": AdvancedAction(PipetteRequest, "home_plunger", idempotent=True),
    "set_flow_rate": AdvancedAction(FlowRateRequest, "set_flow_rate", idempotent=True),
    "set_speed": AdvancedAction(PipetteSpeedRequest, "set_speed", idempotent=True),
    "hs_latch_open": AdvancedAction(ModuleRequest, "hs_latch_open", "heater_shaker"),
    "hs_latch_close": AdvancedAction(ModuleRequest, "hs_latch_close", "heater_shaker"),
    "hs_set_and_wait_shake_speed": AdvancedAction(ShakeSpeedRequest, "hs_set_and_wait_shake_speed", "heater_shaker"),
    "hs_deactivate_shaker": AdvancedAction(ModuleRequest, "hs_deactivate_shaker", "heater_shaker", True),
    "hs_set_target_temperature": AdvancedAction(HeaterShakerTemperatureRequest, "hs_set_target_temperature", "heater_shaker", True),
    "hs_set_and_wait_temperature": AdvancedAction(HeaterShakerTemperatureRequest, "hs_set_and_wait_temperature", "heater_shaker"),
    "hs_wait_for_temperature": AdvancedAction(ModuleRequest, "hs_wait_for_temperature", "heater_shaker"),
    "hs_deactivate_heater": AdvancedAction(ModuleRequest, "hs_deactivate_heater", "heater_shaker", True),
    "hs_deactivate": AdvancedAction(ModuleRequest, "hs_deactivate", "heater_shaker", True),
    "tempmod_await_temperature": AdvancedAction(ModuleRequest, "tempmod_await_temperature", "temperature"),
    "magmod_engage": AdvancedAction(MagnetEngageRequest, "magmod_engage", "magnetic"),
    "magmod_disengage": AdvancedAction(ModuleRequest, "magmod_disengage", "magnetic", True),
    "thermocycler_open_lid": AdvancedAction(ModuleRequest, "thermocycler_open_lid", "thermocycler"),
    "thermocycler_close_lid": AdvancedAction(ModuleRequest, "thermocycler_close_lid", "thermocycler"),
    "thermocycler_set_block_temperature": AdvancedAction(BlockTemperatureRequest, "thermocycler_set_block_temperature", "thermocycler"),
    "thermocycler_set_lid_temperature": AdvancedAction(LidTemperatureRequest, "thermocycler_set_lid_temperature", "thermocycler"),
    "thermocycler_deactivate_block": AdvancedAction(ModuleRequest, "thermocycler_deactivate_block", "thermocycler", True),
    "thermocycler_deactivate_lid": AdvancedAction(ModuleRequest, "thermocycler_deactivate_lid", "thermocycler", True),
    "thermocycler_deactivate": AdvancedAction(ModuleRequest, "thermocycler_deactivate", "thermocycler", True),
}


class GripperMoveRequest(StrictRequest):
    """Direct mount movement in deck mm; the approved path must avoid obstacles."""
    force_direct: bool = Field(default=True, description="True preserves direct motion without a Z-retract waypoint; false requests the robot's arced mount move.")
    x: float = Field(ge=0, le=477.2)
    y: float = Field(ge=0, le=493.8)
    z: float = Field(ge=0, le=218)
    speed: float = Field(default=50, gt=0, le=400)


class GripperRelativeRequest(StrictRequest):
    dx: float = Field(default=0, ge=-100, le=100)
    dy: float = Field(default=0, ge=-100, le=100)
    dz: float = Field(default=0, ge=-100, le=100)
    speed: float = Field(default=50, gt=0, le=400)


class TrashBinRequest(StrictRequest):
    nickname: str = Field(default="default_trash", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    location: str = Field(pattern=r"^[A-D][13]$")


FLEX_ACTIONS = {
    "gripper_move_to_relative": AdvancedAction(GripperRelativeRequest, "gripper_move_to_relative"),
    "gripper_move_to_absolute": AdvancedAction(GripperMoveRequest, "gripper_move_to_absolute"),
    "gripper_open_jaw": AdvancedAction(StrictRequest, "gripper_open_jaw"),
    "gripper_close_jaw": AdvancedAction(StrictRequest, "gripper_close_jaw"),
    "home_gripper": AdvancedAction(StrictRequest, "home_gripper", idempotent=True),
    "load_trash_bin": AdvancedAction(TrashBinRequest, "load_trash_bin", idempotent=True),
}
if IS_FLEX:
    ADVANCED_ACTIONS.update(FLEX_ACTIONS)
    ADVANCED_ACTIONS.pop("magmod_engage")
    ADVANCED_ACTIONS.pop("magmod_disengage")
