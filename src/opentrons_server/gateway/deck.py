"""Normalized OT-2 deck / labware state — the merge of up to three sources.

Phase 0 (this module): pure, hardware-free building blocks only —

- :func:`classify_labware` — an Opentrons ``load_name`` -> ``(kind, rows, columns)``.
- :func:`normalize_repl_slots` — ``state_readers.get_deck_state()`` output -> per-slot
  :class:`SlotLabware` / :class:`SlotModule`.
- :func:`normalize_run_slots` — an active robot-server run's labware -> per-slot
  :class:`SlotLabware`.
- :func:`build_deck` — merge the sources with precedence **run > repl > declared >
  empty**, attach the tracked plate's wells to its slot, and flag declared-vs-observed
  mismatches. A pure function of its inputs; no I/O.
- :class:`DeckDeclarationStore` — JSON-backed operator-declared layout (mirrors
  :class:`PlateStateStore`).

Wiring these into ``OT2Service.get_status`` (side-effect-free, cached inputs only) is
Phase 1+. See ``docs/DECK_STATE.md``.

The Opentrons labware definition already carries everything the normalized model needs
(``parameters.loadName``, ``metadata.displayName``, ``ordering`` for the grid), so this
module has **no** dependency on the Opentrons SDK or PyLabRobot — it derives the shape
from the structured ``load_name`` and enriches from whatever the readers hand it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from .models import (
    DeckSlot,
    DeckSource,
    DeckState,
    LabwareKind,
    LoadedPlate,
    SlotLabware,
    SlotModule,
)

logger = logging.getLogger(__name__)

from .robot_profile import PROFILE

SLOTS = list(PROFILE.slots)

# Standard well-plate / tip-rack grids keyed by total well count -> (rows, columns).
# rows <= columns by Opentrons landscape convention.
_PLATE_GRIDS: Dict[int, Tuple[int, int]] = {
    6: (2, 3),
    12: (3, 4),
    24: (4, 6),
    48: (6, 8),
    96: (8, 12),
    384: (16, 24),
}
# Tube racks are irregular; fall back to the plate grid when unknown.
_TUBE_GRIDS: Dict[int, Tuple[int, int]] = {
    4: (2, 2),
    6: (2, 3),
    10: (2, 5),
    15: (3, 5),
    24: (4, 6),
}
_NAMED_PLATE_KINDS = {6, 12, 24, 48, 96, 384}


def classify_labware(
    load_name: str,
    is_tiprack: Optional[bool] = None,
    display_category: Optional[str] = None,
) -> Tuple[LabwareKind, Optional[int], Optional[int]]:
    """Derive ``(kind, rows, columns)`` from an Opentrons ``load_name``.

    ``load_name`` is highly structured (``<brand>_<count>_<category>_<...>``)::

        corning_96_wellplate_360ul_flat        -> ("96-well",  8, 12)
        opentrons_96_tiprack_300ul             -> ("tiprack",  8, 12)
        nest_12_reservoir_15ml                 -> ("reservoir", 1, 12)
        opentrons_24_tuberack_nest_1.5ml_...   -> ("tuberack",  4,  6)
        opentrons_1_trash_1100ml_fixed         -> ("trash",   None, None)

    Category is detected by substring so token order/extra tokens don't matter; the
    first standalone integer token is the item count. Falls back to ``display_category``
    then ``is_tiprack`` then ``("unknown", None, None)``. The 12-count ambiguity
    (12-reservoir = 1x12 vs 12-wellplate = 3x4) is resolved by the category token.
    """

    name = (load_name or "").lower()
    count = _first_int_token(name)

    category = _detect_category(name, display_category)

    if category == "tiprack":
        return "tiprack", *_grid_or_none(_PLATE_GRIDS, count)
    if category == "wellplate":
        rows, cols = _grid_or_none(_PLATE_GRIDS, count)
        kind: LabwareKind = f"{count}-well" if count in _NAMED_PLATE_KINDS else "well_plate"  # type: ignore[assignment]
        return kind, rows, cols
    if category == "reservoir":
        # A reservoir is a single row of `count` channels (count=1 -> a single trough).
        return "reservoir", (1 if count else None), (count or None)
    if category == "tuberack":
        rows, cols = _grid_or_none(_TUBE_GRIDS, count)
        if rows is None:
            rows, cols = _grid_or_none(_PLATE_GRIDS, count)
        return "tuberack", rows, cols
    if category == "trash":
        return "trash", None, None
    if category == "adapter":
        return "adapter", None, None

    # No recognized category token.
    if is_tiprack:
        return "tiprack", *_grid_or_none(_PLATE_GRIDS, count)
    return "unknown", None, None


def make_slot_labware(
    load_name: str,
    *,
    is_tiprack: Optional[bool] = None,
    display_name: Optional[str] = None,
    display_category: Optional[str] = None,
    rows: Optional[int] = None,
    columns: Optional[int] = None,
) -> SlotLabware:
    """Build a :class:`SlotLabware` from a ``load_name``.

    Grid is taken from the ``load_name`` classification, but explicit ``rows`` /
    ``columns`` (e.g. from an enriched REPL reader that called ``labware.rows()``)
    win when provided.
    """

    kind, derived_rows, derived_cols = classify_labware(load_name, is_tiprack, display_category)
    return SlotLabware(
        kind=kind,
        load_name=load_name,
        display_name=display_name,
        is_tiprack=(kind == "tiprack"),
        rows=rows if rows is not None else derived_rows,
        columns=columns if columns is not None else derived_cols,
    )


def _slot_labware_from_definition(
    load_name: str, definition: Dict[str, Any], *, display_name: Optional[str] = None
) -> SlotLabware:
    """Build a :class:`SlotLabware` from a full Opentrons schema-2 definition
    instead of guessing the grid from ``load_name`` alone.

    Used when a declared slot arrives with its real definition attached (e.g.
    a dashboard custom-labware upload this gateway has no local copy of) —
    ``ordering`` gives the exact grid and ``parameters``/``metadata`` give the
    tiprack flag and display name/category, all authoritative over the
    regex-based :func:`classify_labware` guess.
    """

    ordering = definition.get("ordering")
    if isinstance(ordering, list) and ordering and isinstance(ordering[0], list):
        rows, columns = len(ordering[0]), len(ordering)
    else:
        rows = columns = None
    meta = definition.get("metadata") or {}
    params = definition.get("parameters") or {}
    is_tiprack = bool(params.get("isTiprack"))
    kind, _, _ = classify_labware(
        load_name, is_tiprack=is_tiprack, display_category=meta.get("displayCategory")
    )
    return SlotLabware(
        kind=kind,
        load_name=load_name,
        display_name=display_name or meta.get("displayName"),
        is_tiprack=is_tiprack,
        rows=rows,
        columns=columns,
        definition=definition,
    )


def normalize_repl_slots(
    repl_deck: Optional[Dict[str, Any]],
) -> Dict[str, Union[SlotLabware, SlotModule]]:
    """Map ``state_readers.get_deck_state()`` output to per-slot normalized items.

    ``repl_deck`` is the ``deck`` sub-dict of ``OT2Service.last_snapshot`` (i.e.
    ``{"slots": {"1": {type, load_name, is_tiprack, name} | None, ...}, ...}``).
    Empty / ``None`` slots are omitted from the result (a missing slot == empty).

    A single slot carries labware xor a module (module-with-labware-on-top is a known
    Phase-0 limitation — modules render as a module-only slot).
    """

    out: Dict[str, Union[SlotLabware, SlotModule]] = {}
    if not repl_deck:
        return out
    slots = repl_deck.get("slots") or {}
    for slot, item in slots.items():
        if not item:
            continue
        item_type = item.get("type")
        if item_type == "labware":
            out[str(slot)] = make_slot_labware(
                item.get("load_name") or "",
                is_tiprack=item.get("is_tiprack"),
                display_name=item.get("name"),
            )
        elif item_type == "module":
            out[str(slot)] = SlotModule(
                module_name=item.get("module_name") or item.get("name") or "unknown",
                status=item.get("status"),
                serial_number=item.get("serial_number"),
            )
        else:
            # "unknown" deck item — represent as unknown labware so the slot reads
            # occupied rather than silently empty.
            out[str(slot)] = SlotLabware(kind="unknown", load_name="", display_name=str(item.get("object", "")))
    return out


def run_slot_for(entry: Dict[str, Any], run_doc: Dict[str, Any]) -> Optional[str]:
    """Resolve deck/module/adapter nesting from observed run locations only."""
    items = {item.get("id"): item for item in [*(run_doc.get("labware") or []),
                                                *(run_doc.get("modules") or [])] if item.get("id")}
    location = entry.get("location")
    seen = set()
    while isinstance(location, dict):
        if "slotName" in location:
            slot = str(location["slotName"])
            return slot if slot in SLOTS else None
        parent = location.get("moduleId") or location.get("labwareId")
        if parent is None or parent in seen or parent not in items:
            return None
        seen.add(parent)
        location = items[parent].get("location")
    return None


def normalize_run_modules(run_doc: Optional[Dict[str, Any]]) -> Dict[str, SlotModule]:
    if not run_doc:
        return {}
    return {slot: SlotModule(module_name=item.get("model") or "unknown",
                             serial_number=item.get("serialNumber"))
            for item in run_doc.get("modules", [])
            if (slot := run_slot_for(item, run_doc)) is not None}


def normalize_run_slots(run_doc: Optional[Dict[str, Any]]) -> Dict[str, SlotLabware]:
    """Map an active robot-server run's labware list to per-slot :class:`SlotLabware`.

    ``run_doc`` is the shape the gateway's HTTP probe extracts from ``/runs/{id}``::

        {"labware": [{"loadName": "...", "location": {"slotName": "2"},
                      "displayName": "..."}]}

    Off-deck / module-addressed labware (no ``slotName``) is skipped.
    """

    out: Dict[str, SlotLabware] = {}
    if not run_doc:
        return out
    labwares = run_doc.get("labware") or []
    # A carrier underneath another labware must not hide its contents.
    parents = {lw["location"].get("labwareId") for lw in labwares if isinstance(lw.get("location"), dict)}
    parents.discard(None)
    for lw in labwares:
        if lw.get("id") in parents:
            continue
        slot = run_slot_for(lw, run_doc)
        if slot is None:
            continue
        load_name = lw.get("loadName") or ""
        if not load_name and lw.get("definitionUri"):
            parts = lw["definitionUri"].split("/")
            if len(parts) == 3:
                load_name = parts[1]
        out[slot] = make_slot_labware(load_name, display_name=lw.get("displayName")).model_copy(
            update={"nickname": lw.get("id")}
        )
    return out


def _kinds_agree(a: SlotLabware, b: SlotLabware) -> bool:
    """Whether two labware are the same for mismatch purposes.

    Prefer ``load_name`` equality when both carry a real name; otherwise fall back
    to ``kind`` (a legacy declared entry may only have a kind string). ``unknown`` on
    either side can't prove a conflict, so it agrees.
    """

    if a.kind == "unknown" or b.kind == "unknown":
        return True
    if a.load_name and b.load_name:
        return a.load_name.lower() == b.load_name.lower()
    return a.kind == b.kind


def build_deck(
    *,
    run: Optional[Dict[str, SlotLabware]] = None,
    run_modules: Optional[Dict[str, SlotModule]] = None,
    repl: Optional[Dict[str, Union[SlotLabware, SlotModule]]] = None,
    declared: Optional[Dict[str, Union[SlotLabware, SlotModule]]] = None,
    loaded_plate: Optional[LoadedPlate] = None,
    nickname_to_slot: Optional[Dict[str, str]] = None,
    busy: bool = False,
    now: Optional[datetime] = None,
) -> DeckState:
    """Merge deck sources into a normalized :class:`DeckState`.

    Precedence **run > repl > declared > empty**. Per-slot lifecycle follows the
    decision table in ``docs/DECK_STATE.md`` §2.4. The orchestrator-tracked plate
    (``loaded_plate``) is folded in as a declared entry on its slot (resolved via
    ``nickname_to_slot``), so its wells attach to whichever labware wins that slot.
    Pure: no I/O, no clocks other than the injected ``now``.
    """

    run = run or {}
    repl = repl or {}
    declared = declared or {}
    nickname_to_slot = nickname_to_slot or {}
    now = now or datetime.now(timezone.utc)

    # Fold the tracked plate into an effective declared map so its wells travel with
    # the slot regardless of which source ultimately wins.
    effective_declared: Dict[str, Union[SlotLabware, SlotModule]] = dict(declared)
    plate_slot: Optional[str] = None
    if loaded_plate is not None:
        plate_slot = nickname_to_slot.get(loaded_plate.plate_id)
        if plate_slot in SLOTS:
            plate_lw = make_slot_labware(loaded_plate.model)
            plate_lw = plate_lw.model_copy(
                update={"plate_id": loaded_plate.plate_id, "wells": loaded_plate.wells}
            )
            effective_declared[plate_slot] = plate_lw

    # Reverse the nickname map once. A slot's recipe nickname belongs to the
    # slot, not to whichever source wins it, so it has to survive run > repl >
    # declared precedence: it is the join key for every nickname-addressed
    # surface (details.tip_racks, /control/* labware_nickname). Sorted so a
    # recipe that maps two nicknames to one slot resolves deterministically.
    slot_to_nickname: Dict[str, str] = {}
    for nickname, nickname_slot in sorted(nickname_to_slot.items()):
        slot_to_nickname.setdefault(str(nickname_slot), str(nickname))

    slots: Dict[str, DeckSlot] = {}
    contributing: set[DeckSource] = set()

    for slot in SLOTS:
        observed: Optional[SlotLabware] = None
        observed_source: Optional[DeckSource] = None
        module: Optional[SlotModule] = (run_modules or {}).get(slot)
        module_source: Optional[DeckSource] = "run" if module is not None else None

        if slot in run:
            observed = run[slot]
            observed_source = "run"
        elif slot in repl:
            item = repl[slot]
            if isinstance(item, SlotModule):
                module = item
                module_source = "repl"
            else:
                observed = item
                observed_source = "repl"

        decl_raw = effective_declared.get(slot)
        declared_labware = decl_raw if isinstance(decl_raw, SlotLabware) else None
        # A declared (sticky) module surfaces only when nothing live occupies the
        # slot — a live run/REPL source always wins over operator intent.
        if module is None and observed is None and isinstance(decl_raw, SlotModule):
            module = decl_raw
            module_source = "declared"

        deck_slot = _resolve_slot(
            observed, observed_source, module, module_source, declared_labware, busy
        )
        # Surface the declaration on the slot regardless of which source won, so
        # a declared slot stays recognizable as declared after a run or the REPL
        # loads it. `slot_state` alone cannot carry this: it flips to
        # occupied/in_use and the declaration becomes invisible, which made the
        # panel's full-layout declare round-trip silently delete the
        # declaration of every slot the robot happened to have loaded. Read from
        # `declared`, not `effective_declared`, to keep the folded tracked plate
        # out of the operator's declared layout.
        operator_declared = declared.get(slot)
        if isinstance(operator_declared, SlotModule):
            deck_slot = deck_slot.model_copy(update={"declared_module": operator_declared})
        elif isinstance(operator_declared, SlotLabware) and deck_slot.declared is None:
            deck_slot = deck_slot.model_copy(update={"declared": operator_declared})
        nickname = slot_to_nickname.get(slot)
        if nickname and deck_slot.labware is not None and deck_slot.labware.nickname is None:
            deck_slot = deck_slot.model_copy(
                update={"labware": deck_slot.labware.model_copy(update={"nickname": nickname})}
            )
        slots[slot] = deck_slot
        if deck_slot.source != "empty":
            contributing.add(deck_slot.source)

    deck_source: DeckSource = (
        "run" if "run" in contributing
        else "repl" if "repl" in contributing
        else "declared" if "declared" in contributing
        else "empty"
    )
    return DeckState(source=deck_source, slots=slots, timestamp=now)


def _resolve_slot(
    observed: Optional[SlotLabware],
    observed_source: Optional[DeckSource],
    module: Optional[SlotModule],
    module_source: Optional[DeckSource],
    declared: Optional[SlotLabware],
    busy: bool,
) -> DeckSlot:
    occupied_state = "in_use" if busy else "occupied"

    if observed is not None:
        labware = observed
        # Carry plate_id / wells from the declared (plate-folded) entry when present.
        if declared is not None and declared.wells is not None and labware.wells is None:
            labware = labware.model_copy(
                update={"plate_id": declared.plate_id, "wells": declared.wells}
            )
        if declared is not None and not _kinds_agree(observed, declared):
            return DeckSlot(
                labware=labware,
                module=module,
                slot_state="mismatch",
                source=observed_source or "run",
                declared=declared,
            )
        return DeckSlot(
            labware=labware,
            module=module,
            slot_state=occupied_state,
            source=observed_source or "run",
        )

    if module is not None:
        # Module present, no labware observed on it. A declared (sticky) module is
        # operator intent -> slot_state "declared"; a live module is occupied.
        if module_source == "declared":
            return DeckSlot(module=module, slot_state="declared", source="declared")
        return DeckSlot(module=module, slot_state=occupied_state, source=module_source or "repl")

    if declared is not None:
        # Declared-only: either operator intent or an orchestrator-loaded plate that
        # no live source has confirmed yet.
        return DeckSlot(labware=declared, slot_state="declared", source="declared")

    return DeckSlot(slot_state="empty", source="empty")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_int_token(name: str) -> Optional[int]:
    for tok in name.split("_"):
        if tok.isdigit():
            return int(tok)
    return None


def _detect_category(name: str, display_category: Optional[str]) -> Optional[str]:
    # Explicit hint from an Opentrons definition's metadata.displayCategory wins.
    if display_category:
        dc = display_category.lower()
        mapping = {
            "wellplate": "wellplate",
            "tiprack": "tiprack",
            "reservoir": "reservoir",
            "tuberack": "tuberack",
            "trash": "trash",
            "adapter": "adapter",
            "aluminumblock": "adapter",
        }
        if dc in mapping:
            return mapping[dc]

    # Substring scan of the load_name (handles "well_plate" / "tip_rack" variants).
    compact = re.sub(r"[^a-z0-9]", "", name)
    if "tiprack" in compact:
        return "tiprack"
    if "wellplate" in compact:
        return "wellplate"
    if "reservoir" in compact:
        return "reservoir"
    if "tuberack" in compact:
        return "tuberack"
    if "trash" in compact:
        return "trash"
    if "aluminumblock" in compact or "adapter" in compact:
        return "adapter"
    return None


def _grid_or_none(
    table: Dict[int, Tuple[int, int]], count: Optional[int]
) -> Tuple[Optional[int], Optional[int]]:
    if count is not None and count in table:
        return table[count]
    return None, None


# ---------------------------------------------------------------------------
# Operator-declared layout store (mirrors PlateStateStore)
# ---------------------------------------------------------------------------


def _resolve_state_path(state_path: Union[str, Path]) -> Path:
    p = Path(state_path)
    if not p.is_absolute():
        # deck.py lives at src/opentrons_server/gateway/, so the repo root is
        # four levels up — same anchoring as PlateStateStore.
        p = (Path(__file__).resolve().parents[3] / p).resolve()
    return p


class DeckDeclarationStore:
    """Thread-safe, JSON-backed operator/recipe-declared deck layout.

    Persists a ``{slot -> SlotLabware}`` map so a declared layout survives a gateway
    restart, exactly like the dashboard's ``deck_layouts.json`` stopgap it is meant to
    replace. :meth:`declare` accepts, per slot, a ``load_name`` (preferred), a bare
    ``kind`` string (legacy dashboard compat), a ``{"load_name": ...}`` /
    ``{"kind": ...}`` object, or ``None`` to clear that slot.
    """

    def __init__(self, *, state_path: Union[str, Path]) -> None:
        self._path = _resolve_state_path(state_path)
        self._lock = threading.Lock()
        # A declared slot is a persistent operator-declared fixture: labware OR a
        # module (e.g. a temperature module bolted at a slot). Movable modules are
        # NOT declared — they flow through the live run/REPL deck instead.
        self._slots: Dict[str, Union[SlotLabware, SlotModule]] = {}
        self._load_from_disk()

    @property
    def state_path(self) -> Path:
        return self._path

    def get(self) -> Dict[str, Union[SlotLabware, SlotModule]]:
        with self._lock:
            return {s: item.model_copy(deep=True) for s, item in self._slots.items()}

    def declare(
        self, mapping: Dict[str, Optional[Union[str, Dict[str, Any]]]]
    ) -> Dict[str, Union[SlotLabware, SlotModule]]:
        """Replace the declaration with ``mapping`` (a full-layout PUT).

        A value may be labware (load_name / kind) or a module (a module kind
        string, or a dict with ``module_name``). An empty ``mapping`` clears
        everything. A ``None`` value clears that slot. Unknown slot ids
        (outside 1..12) raise :class:`ValueError`.
        """

        resolved: Dict[str, Union[SlotLabware, SlotModule]] = {}
        for slot, value in mapping.items():
            slot = str(slot)
            if slot not in SLOTS:
                raise ValueError(f"Invalid slot {slot!r}; expected one of {SLOTS}")
            if value is None:
                continue
            resolved[slot] = _coerce_declaration(value)
        with self._lock:
            self._slots = resolved
            self._persist_locked()
        return {s: item.model_copy(deep=True) for s, item in resolved.items()}

    def clear(self) -> None:
        with self._lock:
            self._slots = {}
            self._persist_locked()

    # ---- internals ----------------------------------------------------

    def _load_from_disk(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("deck declaration at %s is unreadable; ignoring", self._path)
            return
        slots_raw = raw.get("slots") or {}
        parsed: Dict[str, Union[SlotLabware, SlotModule]] = {}
        for slot, item_raw in slots_raw.items():
            if str(slot) not in SLOTS:
                continue
            try:
                # A persisted module carries `module_name`; labware carries
                # `load_name`/`kind`. Round-trips model_dump() from either type.
                if isinstance(item_raw, dict) and "module_name" in item_raw:
                    parsed[str(slot)] = SlotModule.model_validate(item_raw)
                else:
                    parsed[str(slot)] = SlotLabware.model_validate(item_raw)
            except Exception:
                logger.exception("deck declaration slot %s is malformed; ignoring", slot)
        self._slots = parsed

    def _persist_locked(self) -> None:
        body = {"slots": {s: lw.model_dump(mode="json") for s, lw in self._slots.items()}}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._path)  # atomic on POSIX + Windows
        except OSError:
            logger.exception("Failed to persist deck declaration to %s", self._path)


# Module-kind shorthands the deck picker can send -> a human-readable
# `module_name` matching what run/REPL-sourced modules report, so a declared
# module and the same module seen live render identically.
_MODULE_KINDS: Dict[str, str] = {
    "temperature_module": "temperature module gen2",
    "magnetic_module": "magnetic module gen2",
    "heater_shaker_module": "heater-shaker module gen1",
    "thermocycler_module": "thermocycler module gen2",
}


def _coerce_declaration(value: Union[str, Dict[str, Any]]) -> Union[SlotLabware, SlotModule]:
    """Turn one declared slot value into a :class:`SlotLabware` or :class:`SlotModule`.

    Modules are declared either as a known module-kind string (e.g.
    ``"temperature_module"``) or a dict carrying ``module_name``.
    """

    if isinstance(value, str):
        if value in _MODULE_KINDS:
            return SlotModule(module_name=_MODULE_KINDS[value])
        return _from_load_name_or_kind(value)
    if isinstance(value, dict):
        if value.get("module_name"):
            return SlotModule(
                module_name=value["module_name"],
                status=value.get("status"),
                serial_number=value.get("serial_number"),
            )
        kind = value.get("kind")
        if isinstance(kind, str) and kind in _MODULE_KINDS:
            return SlotModule(module_name=_MODULE_KINDS[kind])
        if value.get("load_name"):
            definition = value.get("definition")
            if isinstance(definition, dict):
                return _slot_labware_from_definition(
                    value["load_name"], definition, display_name=value.get("display_name")
                )
            return make_slot_labware(value["load_name"], display_name=value.get("display_name"))
        if kind:
            return _kind_only(kind, display_name=value.get("display_name"))
        raise ValueError("declaration object needs 'load_name', 'kind', or 'module_name'")
    raise ValueError(f"unsupported declaration value: {value!r}")


def _from_load_name_or_kind(value: str) -> SlotLabware:
    # A bare string is a load_name if it looks like one (contains '_'); otherwise a
    # legacy kind string (e.g. "96-well", "24-well", "waste").
    if "_" in value:
        return make_slot_labware(value)
    return _kind_only(value)


# Legacy dashboard kind strings -> normalized kind + grid.
_LEGACY_KIND_ALIASES: Dict[str, Tuple[LabwareKind, Optional[int], Optional[int]]] = {
    "waste": ("trash", None, None),
    "trash": ("trash", None, None),
    "96-well": ("96-well", 8, 12),
    "384-well": ("384-well", 16, 24),
    "48-well": ("48-well", 6, 8),
    "24-well": ("24-well", 4, 6),
    "12-well": ("12-well", 3, 4),
    "6-well": ("6-well", 2, 3),
    "tiprack": ("tiprack", None, None),
    "reservoir": ("reservoir", None, None),
    "tuberack": ("tuberack", None, None),
}


def _kind_only(kind: str, display_name: Optional[str] = None) -> SlotLabware:
    normalized, rows, cols = _LEGACY_KIND_ALIASES.get(kind.lower(), ("unknown", None, None))
    return SlotLabware(
        kind=normalized,
        load_name="",
        display_name=display_name,
        is_tiprack=(normalized == "tiprack"),
        rows=rows,
        columns=cols,
    )


__all__ = [
    "SLOTS",
    "classify_labware",
    "make_slot_labware",
    "normalize_repl_slots",
    "normalize_run_slots",
    "build_deck",
    "DeckDeclarationStore",
]
