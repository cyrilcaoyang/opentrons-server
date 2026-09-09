"""Layer-1 hardware limits for the OT-2.

`INTERLOCKS.md` puts hardware limits in layer 1 and `AGENTS.md` §1 puts layer 1
*here* — "hardware limits in the Pydantic request bodies". STATUS_SPEC §9's
**v1.0** checklist asks for the same thing in so many words: "All control
endpoints under ``/control/*``, gated by Pydantic body schemas with
``Field(ge=, le=)`` ranges." Motion coordinates, well offsets, and pipetting
volumes had none, so an out-of-envelope request reached the robot and failed
mid-motion instead of being refused as a 422 before anything moved.

Two tiers, because not every limit is a constant:

* **Static** — the machine's own envelope. Belongs in the request models as
  ``Field(ge=, le=, description=...)``, which is what makes it *visible*: the
  same JSON schema is what ``/openapi.json`` publishes and what the assistant's
  ``list_actions`` hands the model. A limit written here cannot drift from the
  limit enforced, because they are the same declaration — the argument-range
  analogue of STATUS_SPEC §6.2's "one helper feeds both surfaces".
* **Live** — bounds that depend on what is actually attached or loaded: the
  volume of *this* pipette. Those cannot be schema constants, so they are
  checked before the motion and refused with HTTP 412 and a structured body
  (§6.1), the same shape the tip interlocks use.

Sources, and their honesty:

* X/Y come from ``opentrons_shared_data``'s own OT-2 robot definition
  (``extents``), so they track the package rather than this file.
* **Z does not**: that definition publishes ``extents[2] == 0.0``, i.e. no Z
  travel at all. :data:`MAX_Z_MM` is therefore a documented conservative
  constant, not a measured envelope, and the robot's own rejection remains the
  real backstop. Treat it as "obviously out of range", never as "exactly the
  reachable limit".
* :data:`MAX_WELL_OFFSET_MM` is a **sanity bound on intent**, not a claim about
  the machine. A protocol addresses a well with offsets of single-digit
  millimetres; a 200 mm offset is a mistake in every real protocol, and it is
  the one that prompted this module. Both are env-overridable so a genuine
  outlier is a config change rather than a patch.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

from .robot_profile import PROFILE, IS_FLEX

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _ot2_extents() -> Tuple[float, float]:
    """(x, y) gantry travel in mm from the installed shared-data definition.

    Falls back to the values shipped with ``opentrons-shared-data`` 8.x when the
    package is absent — it is an optional dependency here (see ``labware.py``),
    and a gateway without it must still refuse an obviously impossible move
    rather than silently accept every coordinate.
    """

    fallback = (446.75, 347.5)
    try:
        import json
        import pathlib

        import opentrons_shared_data  # type: ignore[import-untyped]

        path = (
            pathlib.Path(opentrons_shared_data.__file__).parent
            / "data" / "robot" / "definitions" / "ot2.json"
        )
        extents = json.loads(path.read_text(encoding="utf-8"))["extents"]
        x, y = float(extents[0]), float(extents[1])
        if x > 0 and y > 0:
            return x, y
    except Exception:  # missing package, moved path, changed schema
        logger.info("shared-data OT-2 extents unavailable; using built-in fallback")
    return fallback


MAX_X_MM, MAX_Y_MM = (PROFILE.max_x, PROFILE.max_y) if IS_FLEX else _ot2_extents()

# See the module docstring: conservative, not measured. shared-data publishes no
# Z extent for the OT-2.
MAX_Z_MM = _env_float("OT2_MAX_Z_MM", 218.0)

# Sanity bound on a well-relative offset, in either direction.
MAX_WELL_OFFSET_MM = _env_float("OT2_MAX_WELL_OFFSET_MM", 100.0)

# The largest single-channel volume any OT-2 pipette holds (P1000). The live
# per-pipette limit is tighter and is enforced by `check_volume`.
MAX_PIPETTE_VOLUME_UL = _env_float("OT2_MAX_PIPETTE_VOLUME_UL", 1000.0)


class OutOfEnvelope(Exception):
    """A request exceeded a live hardware limit. ``body`` is the 412 payload."""

    def __init__(self, body: Dict[str, Any]) -> None:
        super().__init__(body.get("detail", "out of envelope"))
        self.body = body


def check_volume(
    pipette: str,
    volume_ul: float,
    limits: Optional[Tuple[float, float]],
    *,
    action: str = "aspirate",
) -> None:
    """Refuse a volume this pipette cannot handle, before any motion.

    ``limits`` is ``(min_ul, max_ul)`` for the pipette, or ``None`` when the
    gateway could not determine them — an unprobed robot, or a pipette the
    recipe never declared. Unknown limits **pass**: the static schema bound has
    already rejected the absurd cases, and refusing every aspirate because an
    instrument probe is unreachable would be a worse failure than the one this
    guards against. The same reasoning as ``_channels_for``'s fallback to 1, and
    the binding is published on ``details.pipette_volumes`` so a silent pass is
    diagnosable.
    """

    if limits is None:
        return
    min_ul, max_ul = limits
    if volume_ul > max_ul:
        raise OutOfEnvelope(
            {
                "detail": (
                    f"{action} of {volume_ul} uL exceeds pipette {pipette!r}'s "
                    f"capacity of {max_ul} uL"
                ),
                "pipette": pipette,
                "requested_ul": volume_ul,
                "max_ul": max_ul,
                "min_ul": min_ul,
                "retry_after_s": None,
            }
        )
    if min_ul > 0 and volume_ul < min_ul:
        # Below the minimum the pipette cannot meter accurately; the robot will
        # happily attempt it and deliver an unknown volume, which is worse than
        # a refusal because nothing downstream can tell.
        raise OutOfEnvelope(
            {
                "detail": (
                    f"{action} of {volume_ul} uL is below pipette {pipette!r}'s "
                    f"minimum of {min_ul} uL; it cannot meter this accurately"
                ),
                "pipette": pipette,
                "requested_ul": volume_ul,
                "max_ul": max_ul,
                "min_ul": min_ul,
                "retry_after_s": None,
            }
        )


__all__ = [
    "MAX_PIPETTE_VOLUME_UL",
    "MAX_WELL_OFFSET_MM",
    "MAX_X_MM",
    "MAX_Y_MM",
    "MAX_Z_MM",
    "OutOfEnvelope",
    "check_volume",
]
