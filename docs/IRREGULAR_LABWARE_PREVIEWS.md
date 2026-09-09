# Irregular labware previews

The gateway UI renders each well using the supplied definition's x/y location,
shape, diameter or rectangular dimensions. `ordering` is access order, not a
requirement that all columns have the same length. The deck thumbnail and
selected-slot top view use the same renderer, including custom definitions
carried in deck state. Exact load names with unavailable geometry show an
unavailable message; they do not silently become a uniform circular grid.

The standard `opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical` definition
has columns of 3, 3, 2, and 2 wells: six 14.7 mm diameter wells and four
27.81 mm diameter wells. It has ten wells, not a 2-by-5 or 3-by-4 grid.
Well identifiers are drawn individually for irregular arrangements because
the same letter can sit at different y coordinates in different groups.

Side views group wells by physical x coordinate and show the nearest well
in each overlapping group. They use supplied conical and spherical internal
sections when available, preserving each tube's height and diameter. Without
supported section data, walls remain schematic. The outer outline is the
definition's bounding envelope, not a model of the rack's exterior supports.
Tip inventory coloring continues to come from tracked well IDs.

## Verification

Run `npm test` from `ui/`, then `npm run build`. The tests use the installed
standard definitions in `.venv.test` (with the labware extra), including the
mixed Falcon rack, and synthetic mixed-shape definitions. They check both
the detailed SVG and the actual deck component, missing/duplicate well
references, arbitrary ordering groups, and the installed standard catalog.
The tests forbid network requests. The build includes TypeScript checking.

## Separate dashboard builder follow-up

The `ac-organic-lab` builder still uses one global `LabwareSpec` grid.
`specFromDefinition()` derives spacing and shape from A1 and warns that
nonuniform geometry will be lost on rebuild. Gateway rendering does not fix
this separate editor. Do not rebuild a mixed rack with its uniform form.

The proposed dashboard change affects these files, subject to approval for
edits outside this repository:

- `../ac-organic-lab/web/src/lib/labware-schema.ts`: preserve imported well IDs,
  ordering, groups, individual coordinates, shapes, z/depth, capacities and
  internal geometry sections; support multiple independently positioned grids
  for newly authored definitions. Retain exact imported geometry on a no-op
  save, including wells that cannot be represented as regular subgrids.
- `../ac-organic-lab/web/src/app/utils/labware_builder/page.tsx`: expose well
  groups with separate row/column counts, offsets, spacing, dimensions and
  capacities; preview the resulting explicit wells, not the first group's grid.
- `../ac-organic-lab/web/src/lib/labware-geometry.ts`: render explicit well
  geometry consistently with the gateway.
- `../ac-organic-lab/web/src/lib/labware-schema.test.ts`: add lossless
  import/export tests for the mixed Falcon rack, rectangular reservoirs,
  staggered/custom wells and existing single-grid plates.

This changes the dashboard builder for all users, not only this gateway.
It does not require altering existing stored definitions or device state.
