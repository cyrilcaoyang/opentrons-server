"""One configured robot model per gateway process; OT-2 remains the default."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RobotProfile:
    model: str
    slots: tuple[str, ...]
    max_x: float
    max_y: float
    # Conservative shared ceiling, not a guarantee of reachability.
    max_z: float = 218


def profile_for(model: str) -> RobotProfile:
    if model.lower() in {"ot-2", "ot2"}:
        return RobotProfile("OT-2", tuple(str(i) for i in range(1, 13)), 446.75, 347.5)
    if model.lower() in {"flex", "ot-3", "ot3"}:
        # X/Y from opentrons_shared_data robot/definitions/1/ot3.json.
        return RobotProfile("Flex", tuple(f"{row}{col}" for row in "ABCD" for col in range(1, 5)), 477.2, 493.8)
    raise ValueError(f"unknown OT2_ROBOT_MODEL {model!r}; expected OT-2 or Flex")


PROFILE = profile_for(os.getenv("OT2_ROBOT_MODEL", "OT-2"))
IS_FLEX = PROFILE.model == "Flex"
