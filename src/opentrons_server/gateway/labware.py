"""Read-only labware catalog for the gateway UI.

Serves grid summaries of the official Opentrons schema-2 definitions from the
``opentrons-shared-data`` package when it is installed. The package is an
optional dependency: without it the endpoint returns an empty catalog and the
UI falls back to its authored picker entries — nothing else in the gateway
depends on it.

This is a deliberate subset of the dashboard's labware store
(``ac-organic-lab/api/app/labware.py``): summaries only, no uploads, no
custom-definition store, no auth. The gateway owns runtime deck state; the
catalog here exists so the deck-declare picker can offer exact load_names.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _standard_index() -> dict[str, Path]:
    """{load_name: path to the latest schema-2 definition JSON} from the
    installed ``opentrons-shared-data`` package. Empty (with a log line) if
    the package is missing."""
    try:
        import opentrons_shared_data  # type: ignore[import-untyped]
    except ImportError:
        logger.info("opentrons-shared-data not installed; /labware serves an empty catalog")
        return {}
    root = Path(opentrons_shared_data.__file__).parent / "data" / "labware" / "definitions" / "2"
    index: dict[str, Path] = {}
    if not root.is_dir():
        return index
    for d in root.iterdir():
        if not d.is_dir():
            continue
        versions = sorted(
            (p for p in d.glob("*.json") if p.stem.isdigit()),
            key=lambda p: int(p.stem),
        )
        if versions:
            index[d.name] = versions[-1]
    return index


def _grid(defn: dict[str, Any]) -> tuple[int, int]:
    ordering = defn.get("ordering")
    if isinstance(ordering, list) and ordering and isinstance(ordering[0], list):
        return (len(ordering[0]), len(ordering))
    return (0, 0)


def _summary(load_name: str, defn: dict[str, Any]) -> dict[str, Any]:
    rows, columns = _grid(defn)
    meta = defn.get("metadata") or {}
    params = defn.get("parameters") or {}
    brand = defn.get("brand") or {}
    wells = defn.get("wells") or {}
    first_well = next(iter(wells.values()), {}) if isinstance(wells, dict) else {}
    return {
        "load_name": load_name,
        "display_name": meta.get("displayName") or load_name,
        "display_category": meta.get("displayCategory") or "wellPlate",
        "is_tiprack": bool(params.get("isTiprack")),
        "rows": rows,
        "columns": columns,
        "well_count": len(wells) if isinstance(wells, dict) else 0,
        "well_volume_ul": first_well.get("totalLiquidVolume"),
        "height_mm": (defn.get("dimensions") or {}).get("zDimension"),
        "version": defn.get("version"),
        "namespace": defn.get("namespace"),
        # Schema-2 manufacturer metadata (brand.brand / brandId[] / links[]),
        # same summary keys the dashboard store serves.
        "vendor": brand.get("brand"),
        "product_numbers": brand.get("brandId") or [],
        "product_links": brand.get("links") or [],
        "source": "standard",
    }


@lru_cache(maxsize=64)
def standard_definition(load_name: str) -> dict[str, Any] | None:
    """The full schema-2 definition for ``load_name``, or ``None`` if unknown.

    Summaries (:func:`standard_summaries`) carry only the grid; the UI's
    side-view cross-section needs the real geometry — footprint dimensions, well
    depth / shape / diameter, and the A1 offset and spacing the definition's own
    well coordinates imply. Returns the definition verbatim rather than a
    derived spec, mirroring the dashboard's
    ``GET /api/labware/standard/{load_name}`` so both UIs can share one
    client-side reader.

    Cached per load_name (bounded: a deck holds at most a dozen distinct
    definitions, and the shared-data package never changes at runtime).
    """

    path = _standard_index().get(load_name)
    if path is None:
        return None
    try:
        defn = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("standard labware definition %s unreadable", path)
        return None
    return defn if isinstance(defn, dict) else None


@lru_cache(maxsize=1)
def standard_summaries() -> tuple[dict[str, Any], ...]:
    """Summaries for every standard definition, sorted by load_name and cached
    for the process lifetime (the shared-data package never changes at runtime)."""
    out: list[dict[str, Any]] = []
    for load_name in sorted(_standard_index()):
        path = _standard_index()[load_name]
        try:
            defn = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("standard labware definition %s unreadable", path)
            continue
        out.append(_summary(load_name, defn))
    return tuple(out)
