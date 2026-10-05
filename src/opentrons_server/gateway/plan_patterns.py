"""Plan proposals by pattern: ``for_each_well`` expanded into flat steps.

A proposer (the assistant, Hermes, the API) may send a *pattern* instead of
writing every step out: a step template, the wells to apply it to, and
optional per-well overrides. This module expands it into the flat step list
the plan store already validates, hashes, approves and runs. The approved
artifact is the expansion, hashed exactly as a hand-written list would be; the
pattern is kept as provenance and summarised server-side for the review card
(``summarize``), so the reviewer checks intent against a summary the gateway
derived from the real steps — never against the proposer's own description.

Semantics (settled in review, docs/PLAN_REPEAT_DESIGN.md):

* ``{well}`` / ``{row}`` are strings, ``{index}`` (1-based) / ``{column}`` are
  integers. A string that is exactly one placeholder takes that type; a
  placeholder inside a longer string is spliced in as text. Substitution
  walks dict *values* and list items, never dict keys. Any ``{name}`` left
  unresolved is an error, not a literal.
* ``wells`` is an explicit list (order preserved) or a range ``A1:H12``
  walked in ``order`` (``column``: A1, B1, … — the OT-2 traversal — or
  ``row``). Wells are checked against the labware's grid; unknown labware,
  nonexistent or duplicate wells are refused.
* ``overrides`` are keyed by well, then by template step ``id``; the override
  deep-merges into that step's args (so overriding ``location.position``
  keeps the location's offsets). Unknown wells or ids are refused.
* v1 is single-channel: a template step naming a multi-channel pipette is
  refused, because an 8-channel pick takes a column, not a well.
* Nothing is inferred: tip handling and balance references are whatever the
  prelude / template / epilogue say, and the summary reports what the
  expansion actually does (picks, drops, reads, tares).
"""

from __future__ import annotations

import copy
import re
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_EXPANDED_STEPS = 2000

_WELL = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_KNOWN = ("well", "row", "column", "index")


class PatternError(ValueError):
    """A pattern that cannot be expanded; the message names the offending part."""


class PatternStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    args: Dict[str, Any] = Field(default_factory=dict)
    # Names the step for overrides; required only when an override targets it.
    id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,32}$")


class ForEachWell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    labware_nickname: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    wells: Any = Field(description="a range like 'A1:H12' or an explicit list of wells")
    order: Literal["column", "row"] = "column"
    steps: List[PatternStep] = Field(min_length=1)
    overrides: Dict[str, Dict[str, Dict[str, Any]]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _wells_shape(self) -> "ForEachWell":
        if isinstance(self.wells, str):
            if not re.fullmatch(r"[A-Z]+[1-9][0-9]*:[A-Z]+[1-9][0-9]*", self.wells):
                raise ValueError("wells must be a range like 'A1:H12' or a list of wells")
        elif isinstance(self.wells, list):
            if not self.wells or not all(isinstance(w, str) and _WELL.match(w) for w in self.wells):
                raise ValueError("wells list must hold well names like 'A1'")
        else:
            raise ValueError("wells must be a range like 'A1:H12' or a list of wells")
        ids = [s.id for s in self.steps if s.id]
        if len(ids) != len(set(ids)):
            raise ValueError("template step ids must be unique")
        return self


#: A labware's wells as the gateway knows them: ``(rows, columns)`` and, when
#: the definition lists its wells (custom labware can be sparse), the exact
#: set of well names; None when the labware is not declared / not loaded.
class WellGrid(BaseModel):
    rows: int
    columns: int
    wells: Optional[frozenset[str]] = None

    def has(self, well: str) -> bool:
        if self.wells is not None:
            return well in self.wells
        m = _WELL.match(well)
        return bool(m) and _row_index(m[1]) < self.rows and int(m[2]) <= self.columns


GridFor = Callable[[str], Optional[WellGrid]]
ChannelsFor = Callable[[str], int]


def _row_label(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    label = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        label = chr(ord("A") + rem) + label
    return label


def _row_index(label: str) -> int:
    n = 0
    for ch in label:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def resolve_wells(spec: ForEachWell, grid: WellGrid | Tuple[int, int]) -> List[str]:
    if isinstance(grid, tuple):
        grid = WellGrid(rows=grid[0], columns=grid[1])
    rows, columns = grid.rows, grid.columns
    if isinstance(spec.wells, str):
        start, end = spec.wells.split(":")
        r0, c0 = _row_index(_WELL.match(start)[1]), int(_WELL.match(start)[2])
        r1, c1 = _row_index(_WELL.match(end)[1]), int(_WELL.match(end)[2])
        if r1 < r0 or c1 < c0:
            raise PatternError(f"well range {spec.wells!r} runs backwards")
        # Size before building anything: a range is text from a model, and
        # "A1:A1000000000" must fail here, not in memory.
        if (r1 - r0 + 1) * (c1 - c0 + 1) > MAX_EXPANDED_STEPS:
            raise PatternError(
                f"well range {spec.wells!r} names {(r1 - r0 + 1) * (c1 - c0 + 1)} wells; "
                f"at most {MAX_EXPANDED_STEPS}")
        # A labware whose wells are known exactly is checked by membership
        # below; its rows x columns are a bounding box, not a rule.
        if grid.wells is None and (r1 >= rows or c1 > columns):
            raise PatternError(
                f"well range {spec.wells!r} exceeds labware {spec.labware_nickname!r} "
                f"({rows} rows x {columns} columns)")
        if spec.order == "column":
            wells = [f"{_row_label(r)}{c}" for c in range(c0, c1 + 1) for r in range(r0, r1 + 1)]
        else:
            wells = [f"{_row_label(r)}{c}" for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]
        missing = [w for w in wells if not grid.has(w)]
        if missing:
            raise PatternError(
                f"well range {spec.wells!r} covers wells labware {spec.labware_nickname!r} "
                f"does not have: {', '.join(missing[:6])}{'…' if len(missing) > 6 else ''}")
        return wells
    wells = list(spec.wells)
    seen = set()
    for well in wells:
        if not grid.has(well):
            raise PatternError(
                f"well {well!r} does not exist on labware {spec.labware_nickname!r} "
                f"({rows} rows x {columns} columns)")
        if well in seen:
            raise PatternError(f"well {well!r} is listed twice")
        seen.add(well)
    return wells


def _substitute(value: Any, vars: Dict[str, Any], where: str) -> Any:
    if isinstance(value, dict):
        return {k: _substitute(v, vars, where) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, vars, where) for v in value]
    if not isinstance(value, str):
        return value
    whole = _PLACEHOLDER.fullmatch(value)
    if whole:
        name = whole[1]
        if name not in vars:
            raise PatternError(f"{where}: unknown placeholder {{{name}}} (known: "
                               + ", ".join("{" + k + "}" for k in _KNOWN) + ")")
        return vars[name]
    def repl(m: re.Match) -> str:
        if m[1] not in vars:
            raise PatternError(f"{where}: unknown placeholder {{{m[1]}}}")
        return str(vars[m[1]])
    out = _PLACEHOLDER.sub(repl, value)
    if "{" in out or "}" in out:
        # {Well}, {sample1}, {{well}}: braces are reserved for placeholders,
        # and anything they leave behind would reach the robot as text.
        raise PatternError(f"{where}: {value!r} has braces that are not a known placeholder "
                           "(" + ", ".join("{" + k + "}" for k in _KNOWN) + ")")
    return out


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def expand(
    spec: ForEachWell,
    *,
    grid_for: GridFor,
    channels_for: ChannelsFor,
    prelude: Sequence[Dict[str, Any]] = (),
    epilogue: Sequence[Dict[str, Any]] = (),
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Expand to ``(steps, pattern)``: flat ``{action, args}`` dicts in run
    order, and the pattern kept as provenance (wells resolved)."""
    grid = grid_for(spec.labware_nickname)
    if grid is None:
        raise PatternError(
            f"labware {spec.labware_nickname!r} is not declared or loaded, so its wells cannot "
            "be checked; declare the deck first")
    wells = resolve_wells(spec, grid)
    ids = {s.id for s in spec.steps if s.id}
    for well, per_step in spec.overrides.items():
        if well not in wells:
            raise PatternError(f"override for {well!r}, which is not among the wells")
        for step_id in per_step:
            if step_id not in ids:
                raise PatternError(f"override for {well!r} names step id {step_id!r}, "
                                   "which no template step has")
    total = len(prelude) + len(wells) * len(spec.steps) + len(epilogue)
    if total > MAX_EXPANDED_STEPS:
        raise PatternError(f"pattern expands to {total} steps; at most {MAX_EXPANDED_STEPS}")

    steps: List[Dict[str, Any]] = [copy.deepcopy(dict(s)) for s in prelude]
    for index, well in enumerate(wells, start=1):
        m = _WELL.match(well)
        vars = {"well": well, "row": m[1], "column": int(m[2]), "index": index}
        for step in spec.steps:
            args = _substitute(step.args, vars, f"well {well}, step {step.action}")
            override = spec.overrides.get(well, {}).get(step.id or "", None)
            if override:
                args = _deep_merge(args, _substitute(override, vars, f"override {well}/{step.id}"))
            # On the final arguments, so an override cannot swap in a
            # multi-channel head after the template passed.
            pipette = args.get("pipette")
            if isinstance(pipette, str) and channels_for(pipette) != 1:
                raise PatternError(
                    f"well {well}, step {step.action!r} uses {pipette!r}, a multi-channel "
                    "pipette; for_each_well is single-channel only (a multi-channel pick "
                    "takes a column)")
            steps.append({"action": step.action, "args": args})
    steps.extend(copy.deepcopy(dict(s)) for s in epilogue)
    pattern = {
        "for_each_well": spec.model_dump(mode="json"),
        "wells": wells,
        "prelude": [dict(s) for s in prelude],
        "epilogue": [dict(s) for s in epilogue],
    }
    return steps, pattern


def _volume(args: Dict[str, Any]) -> Optional[float]:
    v = args.get("volume_ul")
    return float(v) if isinstance(v, (int, float)) else None


def _location(args: Dict[str, Any]) -> Optional[str]:
    loc = args.get("location")
    if isinstance(loc, dict) and loc.get("labware_nickname") and loc.get("position"):
        return f"{loc['labware_nickname']}/{loc['position']}"
    return None


def summarize(pattern: Dict[str, Any], steps: Sequence[Any]) -> Dict[str, Any]:
    """What the review card shows for a pattern plan — derived from the
    expanded steps, not from the proposer's words.

    ``steps`` are the plan's steps (``PlanStep`` or dicts). Volumes and
    locations are read from the real expanded arguments; the template line
    shows the first well's values with ``{well}`` tokens for what varies.
    """
    def dump(step: Any) -> Dict[str, Any]:
        return step.model_dump() if hasattr(step, "model_dump") else dict(step)

    flat = [dump(s) for s in steps]
    spec = pattern.get("for_each_well") or {}
    wells: List[str] = list(pattern.get("wells") or [])
    template: List[Dict[str, Any]] = list(spec.get("steps") or [])
    n_pre, n_epi = len(pattern.get("prelude") or []), len(pattern.get("epilogue") or [])
    per_iter = len(template)
    body = flat[n_pre: len(flat) - n_epi if n_epi else None]

    actions = []
    for t_index, t in enumerate(template):
        instances = body[t_index::per_iter] if per_iter else []
        volumes = [v for v in (_volume(s["args"]) for s in instances) if v is not None]
        first = instances[0]["args"] if instances else t.get("args", {})
        actions.append({
            "action": t["action"],
            "pipette": t.get("args", {}).get("pipette"),
            "template_location": _location(t.get("args", {})),  # with {well} tokens
            "first_location": _location(first),
            "volume_ul": _volume(t.get("args", {})),
            "total_volume_ul": round(sum(volumes), 3) if volumes else None,
            "count": len(instances),
        })

    counts = {name: sum(1 for s in flat if s["action"] == name)
              for name in ("pick_up_tip", "drop_tip", "platebalance.read", "platebalance.tare",
                           "platebalance.zero")}
    pipettes = sorted({s["args"].get("pipette") for s in flat if isinstance(s["args"].get("pipette"), str)})
    overrides = spec.get("overrides") or {}
    return {
        "labware": spec.get("labware_nickname"),
        "well_count": len(wells),
        "wells_first": wells[:4],
        "wells_last": wells[-2:] if len(wells) > 4 else [],
        # An explicit list has no walk order: it runs as listed.
        "order": spec.get("order") if isinstance(spec.get("wells"), str) else "as listed",
        "pipettes": pipettes,
        "per_well": actions,
        "prelude": [s["action"] for s in pattern.get("prelude") or []],
        "epilogue": [s["action"] for s in pattern.get("epilogue") or []],
        "tip_pickups": counts["pick_up_tip"],
        "tip_drops": counts["drop_tip"],
        "balance_reads": counts["platebalance.read"],
        "balance_tares": counts["platebalance.tare"] + counts["platebalance.zero"],
        # The exceptions with what they change, so the reviewer need not open
        # the collapsed list to see them.
        "overrides": {well: {step_id: args for step_id, args in per.items()}
                      for well, per in overrides.items()},
        "total_steps": len(flat),
    }
