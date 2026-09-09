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

## Dashboard builder implementation and deployment

The separate `ac-organic-lab` builder change is committed locally as `c10fb2b`
on `feat/irregular-labware-grids`. It supports independent grids with separate
positions, shapes, sizes, depths, bottom Z and capacities. Imported definitions
retain exact well IDs, ordering, metadata, coordinates and internal profiles
on an unchanged export. Non-grid arrangements become individual well grids.

The implementation affects these files:

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

Editing an imported grid's geometry replaces that grid's internal profiles
with schematic wells, as explained in the editor. Unedited grids retain their
source data. Changing row/column counts generates new IDs; duplicate IDs,
overlapping wells and out-of-bounds grids block saving.

Validation passed: 18 schema tests, TypeScript checking and a production
Next.js build. An offline sweep confirmed exact import/export preservation for
all 118 installed nonempty standard definitions. The build reports existing
hook-dependency warnings in unrelated components.

The gateway UI release `422f38b` was deployed to both gateway checkouts and
verified over HTTP. This static-asset update required no restart. The dashboard
builder is not deployed: SSH authentication to the central server was refused,
and automatic approval review blocked the branch push pending explicit user
approval to upload this repository's changes to GitHub.

### Prompt for the dashboard server agent, after the branch is published

> Deploy ac-organic-lab commit `c10fb2b` from
> `origin/feat/irregular-labware-grids`. Read AGENTS.md and deploy/README.md.
> Inspect the live web service's WorkingDirectory and the checkout's branch
> and cleanliness. Fetch and integrate this specific commit through the normal
> review/deployment workflow; do not replace the server branch with the feature
> branch or discard local work. Run the labware schema tests, typecheck and
> production build with locked dependencies. Include static/public assets in
> the standalone build, then restart only ac-organic-lab-web. Verify
> /utils/labware_builder loads the new bundle and exposes independent grids.
> Inspect the mixed Falcon rack and rectangular wells without saving any
> definitions or issuing robot commands. Report the deployed commit and checks.
> No gateway, API, Caddy or other service restart is needed for this web change.
