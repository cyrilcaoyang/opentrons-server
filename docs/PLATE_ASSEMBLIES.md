# Fixed plate assemblies

The gateway supports a well plate raised by a **5 mm effective lift**, and a
96-well filter plate seated above a separate 96-well collection plate, optionally
on that riser. Tip racks are forbidden in either position.

This is a fixed assembly for the duration of one robot session. The gateway
compiles one custom Opentrons schema-2 definition containing **only the top
plate's wells**. Both HTTP and SSH load that same definition. The component
definitions and separate plate IDs remain in `declared.assembly`; the collector
is never exposed as an accessible set of wells. This does not implement native
Opentrons adapter objects, gripper handling, or in-session stack disassembly.

## Operator panel

1. End any session that has already loaded the affected slot, then claim the
   gateway again. Changing a declaration cannot change already-loaded geometry.
2. Select an empty declared slot, open **Declare deck intent → Plate riser /
   filter assembly**, and choose the arrangement.
3. Select each plate from the existing gateway/central labware catalog, or import
   its schema-2 JSON exported from the central labware builder. Enter distinct
   plate IDs; avoid deck-slot strings and session nicknames.
4. For a filter stack, enter its measured nesting overlap:

   `collector height + filter height − measured seated pair height`

   The riser is excluded from this measurement. Zero means no overlap; the UI
   requires an explicit value. Filter depth must describe its usable internal
   well down to the membrane, not the outlet or external underside.
5. **Validate and preview assembly**. Check the base and total heights against
   the seated hardware, confirm the geometry, then **Declare assembly**.
6. Start a session if needed. Existing well-addressed controls can reference the
   deck slot or the top plate ID. Geometry is loaded on first use, not on preview,
   declaration or status polling.

The riser must fit within the declared plate footprint, with no lateral shift.
Filter pairs currently require standard A1–H12 ordering and corresponding well
centres aligned within 0.25 mm. Nonzero `cornerOffsetFromSlot`, rotated/shifted
stacks, and layouts with modules are refused. Total assembled height is bounded
at 200 mm; this validation does not certify physical clearance for a given
pipette/tip combination.

To access the collector, end the session, remove the filter physically, clear
the assembly declaration, and declare the collector with its own definition
(or as a plate on the riser). Start a fresh session. The collector ID is rejected
as a pipetting target while covered; moving an assembly through `move_labware`
is also refused. A failed shutdown does not release the loaded-geometry guard.

## Data model and API

`POST /labware/assemblies/preview` accepts this envelope and returns the validated
assembly, compiled definition, `top_origin_z_mm`, and `total_height_mm`. It has no
robot I/O, file writes, or deck side effects.

```python
assembly = {
    "schema_version": 1,
    "kind": "filter_stack",  # or "plate_on_riser"
    "riser_height_mm": 5,    # 0 or 5; plate_on_riser requires 5
    "top": {"plate_id": "filter_plate", "definition": filter_definition},
    "collector": {"plate_id": "collection_plate", "definition": collector_definition},
    "nesting_overlap_mm": measured_overlap,
}
```

`definition` is the entire original plate JSON. For `plate_on_riser`, set
`collector` to null and overlap to zero. Declarations accept
`{"slots": {"2": {"assembly": assembly}}}`. This endpoint is still a full-layout
replacement: preserve all other slots. The gateway recompiles from the original
components, ignoring any caller-supplied compiled geometry alongside them.
Repeated saves never apply the lift twice.

Workflow setup also accepts an `assembly` on a labware item alongside `nickname`
and a bare deck-slot `location`. It derives `ot_default=False` and the custom
definition itself. Workflow callers continue to use `lab-skills`, its claims and
plan validation. This gateway does not orchestrate filtration or other devices.

Generated load names contain a hash of the component geometry and seating.
Different geometry therefore yields a different name and can be detected as a
deck mismatch. Matching robot readback confirms that compiled definition was
loaded; it does **not** confirm the physical stack. Components remain explicitly
operator-declared. The inspector uses the compiled geometry only when the
observed name matches it.

The declaration retains identities, not a second per-well experiment database.
Filter and collector contents and filtration outcomes belong to the existing
workflow/BitacoraDB records. Dispensing into the filter never creates a collected
volume automatically; collector samples are never folded onto the filter's wells.

## Offline verification and operator acceptance

`tests/unit/test_assemblies.py` covers geometry, malformed inputs, schema
conformance, persistence, declared/observed provenance, covered-collector guards,
session transitions and transport parity. UI tests cover declaration preservation.

After a reviewed deployment, operator acceptance is on **Complexation only**:
verify the actual riser lift and seated pair dimensions, use empty plates and
approved motion checks to confirm well positions/clearance, then verify collector
access only after ending the session and redeclaring. No development test moves
hardware. Deploying and hardware acceptance are separate from the offline checks.

The central builder/store remains owned by `ac-organic-lab`. This gateway consumes
its existing JSON definitions; it does not add a competing component catalog.
