"""Fixed plate assemblies: declared components and compiled accessible geometry.

The assembly is operator intent, never a sensor observation. Both transports
load the same compiled schema-2 definition. Only the top plate has addressable
wells; a collector is retained as a separate identity in the declaration.
"""

from copy import deepcopy
import hashlib
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{label} must be {'positive' if positive else 'nonnegative'}")
    return float(value)


class AssemblyPlate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plate_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    definition: dict[str, Any]

    @model_validator(mode="after")
    def validate_plate(self):
        d = self.definition
        if d.get("schemaVersion") != 2:
            raise ValueError("assembly plates require schema-2 definitions")
        p, m = d.get("parameters", {}), d.get("metadata", {})
        if not isinstance(p, dict) or not isinstance(m, dict):
            raise ValueError("plate parameters and metadata must be objects")
        if p.get("isTiprack") is not False or m.get("displayCategory") != "wellPlate":
            raise ValueError("assemblies accept well plates only; never tip racks")
        if not p.get("loadName") or not d.get("namespace") or not isinstance(d.get("version"), int):
            raise ValueError("plate definition needs loadName, namespace and version")
        if (
            not isinstance(d.get("brand"), dict)
            or not isinstance(d.get("groups"), list)
            or not m.get("displayName")
        ):
            raise ValueError("plate definition needs brand, groups and displayName")
        # All embedded metadata must also be finite JSON before it is hashed
        # and shipped unchanged to either transport.
        try:
            json.dumps(d, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("plate definition must contain finite JSON values") from exc
        # Nonzero offsets and module-specific geometry require a separate qualification.
        if d.get("cornerOffsetFromSlot", {}) != {"x": 0, "y": 0, "z": 0}:
            raise ValueError("assembly plates require zero cornerOffsetFromSlot")
        dims = d.get("dimensions")
        if not isinstance(dims, dict):
            raise ValueError("plate dimensions are required")
        for axis, maximum in (("x", 128), ("y", 86), ("z", 200)):
            if _number(dims.get(f"{axis}Dimension"), f"plate {axis} size", positive=True) > maximum:
                raise ValueError(f"plate {axis} size exceeds {maximum} mm")
        wells, ordering = d.get("wells"), d.get("ordering")
        if (
            not isinstance(wells, dict)
            or not wells
            or not isinstance(ordering, list)
            or not ordering
        ):
            raise ValueError("plate wells and ordering are required")
        if any(not isinstance(col, list) or not col for col in ordering):
            raise ValueError("ordering must contain nonempty columns")
        ids = [well for col in ordering for well in col]
        if (
            any(not isinstance(w, str) for w in ids)
            or len(ids) != len(set(ids))
            or set(ids) != set(wells)
        ):
            raise ValueError("ordering must name every well exactly once")
        for name, well in wells.items():
            if not isinstance(well, dict):
                raise ValueError(f"{name} must be a well object")
            for axis in ("x", "y", "z"):
                _number(well.get(axis), f"{name}.{axis}")
            depth = _number(well.get("depth"), f"{name}.depth", positive=True)
            _number(well.get("totalLiquidVolume"), f"{name}.volume", positive=True)
            if well["z"] + depth > dims["zDimension"] + 1e-6:
                raise ValueError(f"{name} extends above the plate envelope")
            if well.get("shape") == "circular":
                widths = [_number(well.get("diameter"), f"{name}.diameter", positive=True)] * 2
            elif well.get("shape") == "rectangular":
                widths = [
                    _number(well.get(f"{a}Dimension"), f"{name}.{a} size", positive=True)
                    for a in ("x", "y")
                ]
            else:
                raise ValueError(f"{name} needs circular or rectangular geometry")
            for axis, width in zip(("x", "y"), widths):
                if well[axis] - width / 2 < 0 or well[axis] + width / 2 > dims[f"{axis}Dimension"]:
                    raise ValueError(f"{name} extends outside the plate footprint")
        return self


class PlateAssembly(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    kind: Literal["plate_on_riser", "filter_stack"]
    riser_height_mm: Literal[0, 5] = 5
    top: AssemblyPlate
    collector: AssemblyPlate | None = None
    # Measured vertical overlap between the collector top and filter base.
    nesting_overlap_mm: float = Field(default=0, ge=0, le=200)

    @model_validator(mode="after")
    def validate_stack(self):
        if self.kind == "plate_on_riser":
            if (
                self.collector is not None
                or self.nesting_overlap_mm != 0
                or self.riser_height_mm != 5
            ):
                raise ValueError(
                    "plate_on_riser requires the 5 mm riser and no collector or overlap"
                )
        else:
            if self.collector is None:
                raise ValueError("filter_stack requires a collection plate")
            if self.top.plate_id == self.collector.plate_id:
                raise ValueError("filter and collector must have different plate IDs")
            expected = [[f"{row}{col}" for row in "ABCDEFGH"] for col in range(1, 13)]
            for plate in (self.top, self.collector):
                if plate.definition["ordering"] != expected:
                    raise ValueError(
                        "filter stacks require aligned standard 96-well ordering (A1–H12)"
                    )
            if self.nesting_overlap_mm >= min(self.height(self.top), self.height(self.collector)):
                raise ValueError("nesting overlap must be less than both plate heights")
            for name, well in self.top.definition["wells"].items():
                lower = self.collector.definition["wells"][name]
                if any(abs(well[a] - lower[a]) > 0.25 for a in ("x", "y")):
                    raise ValueError(
                        "filter/collector well centres must align within 0.25 mm; shifted stacks are unsupported"
                    )
        if self.total_height_mm > 200:
            raise ValueError("assembled height exceeds 200 mm")
        return self

    @staticmethod
    def height(plate: AssemblyPlate) -> float:
        return plate.definition["dimensions"]["zDimension"]

    @property
    def top_origin_z_mm(self) -> float:
        return self.riser_height_mm + (
            self.height(self.collector) - self.nesting_overlap_mm if self.collector else 0
        )

    @property
    def total_height_mm(self) -> float:
        return self.top_origin_z_mm + self.height(self.top)

    def compile_definition(self) -> dict[str, Any]:
        """Compile a fixed stack without mutating component definitions.

        Content-addressed names make changed seating/geometry detectable in
        run readback. Identity is retained only in the assembly envelope.
        """
        geometry = self.model_dump(mode="json")
        for role in ("top", "collector"):
            if geometry.get(role):
                geometry[role].pop("plate_id")
        digest = hashlib.sha256(
            json.dumps(geometry, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()[:20]
        d = deepcopy(self.top.definition)
        d["namespace"], d["version"] = "custom", 1
        d["parameters"]["loadName"] = f"ac_assembly_{digest}"
        d["parameters"]["isMagneticModuleCompatible"] = False
        d["metadata"][
            "displayName"
        ] = f"{d['metadata'].get('displayName', 'Plate')} · {self.kind} · {self.total_height_mm:g} mm"
        for well in d["wells"].values():
            well["z"] += self.top_origin_z_mm
        d["dimensions"]["zDimension"] = self.total_height_mm
        if self.collector:
            for axis in ("x", "y"):
                key = f"{axis}Dimension"
                d["dimensions"][key] = max(
                    d["dimensions"][key], self.collector.definition["dimensions"][key]
                )
        # Component-specific handling metadata cannot describe the assembly.
        for key in (
            "stackingOffsetWithLabware",
            "stackingOffsetWithModule",
            "gripperOffsets",
            "gripForce",
            "gripHeightFromLabwareBottom",
            "allowedRoles",
        ):
            d.pop(key, None)
        return d
