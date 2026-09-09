import "./browser";
import assert from "node:assert/strict";
import { test } from "node:test";
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { PlanView, ElevationView } from "../src/components/PlateInspector";
import { DeckPanel } from "../src/components/DeckPanel";
import { geometryFromDefinition, frontProjectionColumns } from "../src/lib/labware-geometry";
import { buildWellModel } from "../src/lib/plate-wells";
import rack from "../../.venv.test/Lib/site-packages/opentrons_shared_data/data/labware/definitions/2/opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical/3.json";

function modelFor(definition: unknown) {
  const geometry = geometryFromDefinition(definition);
  assert.ok(geometry);
  return { geometry, model: buildWellModel({
    geometry, rows: geometry.rows, columns: geometry.columns,
    isTiprack: geometry.isTiprack, tipRack: null, samples: null,
  }) };
}

test("official mixed Falcon rack has ten wells, unequal columns and two diameters", () => {
  const { geometry, model } = modelFor(rack);
  assert.deepEqual(geometry.ordering.map((column) => column.length), [3, 3, 2, 2]);
  assert.equal(model.total, 10);
  assert.equal(model.byWell.C3, undefined);
  assert.equal(geometry.wells.A1.diameter, 14.7);
  assert.equal(geometry.wells.A3.diameter, 27.81);
  const html = renderToStaticMarkup(<PlanView geometry={geometry} model={model} />);
  assert.equal((html.match(/<ellipse /g) ?? []).length, 10);
  assert.match(html, /cx="13.88" cy="17.75" rx="7.35"/);
  assert.match(html, /cx="71.38" cy="25.25" rx="13.905"/);
  assert.match(html, />A3<\/text>/);
  const side = renderToStaticMarkup(<ElevationView geometry={geometry} model={model} />);
  assert.equal((side.match(/<polygon /g) ?? []).length, 5); // envelope + 4 distinct x profiles
  assert.match(side, /15000 µL/);
  assert.match(side, /50000 µL/);
  assert.ok(geometry.wells.A1.sections!.some((s) => s.bottomDiameter === 4));
  assert.ok(geometry.wells.A3.sections!.some((s) => s.bottomDiameter === 6.15));
});

const square = {
  dimensions: { xDimension: 100, yDimension: 80, zDimension: 20 },
  parameters: { loadName: "synthetic_mixed", isTiprack: false },
  ordering: [["A1", "B1"], ["A2"]],
  wells: {
    A1: { x: 20, y: 60, z: 2, depth: 18, shape: "rectangular", xDimension: 12, yDimension: 8 },
    B1: { x: 20, y: 20, z: 2, depth: 18, shape: "circular", diameter: 10 },
    A2: { x: 70, y: 40, z: 4, depth: 16, shape: "rectangular", xDimension: 20, yDimension: 30 },
  },
};

test("detail and deck thumbnail preserve mixed shapes, spacing, and exact well count", () => {
  const { geometry, model } = modelFor(square);
  const detail = renderToStaticMarkup(<PlanView geometry={geometry} model={model} />);
  const thumbnail = renderToStaticMarkup(<DeckPanel deviceDeck={{ source: "declared", slots: {
    "2": { slot_state: "declared", source: "declared", module: null, labware: {
      kind: "unknown", load_name: "synthetic_mixed", definition: square,
    } },
  } }} />);
  for (const html of [detail, thumbnail]) {
    assert.match(html, /x="14" y="16" width="12" height="8"/);
    assert.match(html, /x="60" y="25" width="20" height="30"/);
    assert.match(html, /cx="20" cy="60" rx="5" ry="5"/);
    assert.equal((html.match(/<ellipse /g) ?? []).length, 1);
  }
});

test("missing and duplicate well references remain invalid; unequal columns are valid", () => {
  assert.ok(geometryFromDefinition(square));
  assert.equal(geometryFromDefinition({ ...square, ordering: [["missing"]] }), null);
  assert.equal(geometryFromDefinition({ ...square, ordering: [["A1", "A1"]] }), null);
});

test("front projection uses physical x coordinates even when ordering is one arbitrary group", () => {
  const { geometry } = modelFor({ ...square, ordering: [["A1", "A2", "B1"]] });
  assert.deepEqual(frontProjectionColumns(geometry), [["B1", "A1"], ["A2"]]);
});

test("installed standard catalog preserves all ordered wells in accepted definitions", () => {
  const root = resolve("../.venv.test/Lib/site-packages/opentrons_shared_data/data/labware/definitions/2");
  let checked = 0;
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const versions = readdirSync(resolve(root, entry.name)).filter((file) => /^\d+\.json$/.test(file))
      .sort((a, b) => parseInt(a) - parseInt(b));
    if (!versions.length) continue;
    const definition = JSON.parse(readFileSync(resolve(root, entry.name, versions.at(-1)!), "utf8"));
    if (!Object.keys(definition.wells ?? {}).length) continue; // lids have no wells
    const { geometry, model } = modelFor(definition);
    assert.equal(model.total, Object.keys(definition.wells).length, entry.name);
    const plan = renderToStaticMarkup(<PlanView geometry={geometry} model={model} compact />);
    assert.equal((plan.match(/<title>/g) ?? []).length, model.total, entry.name);
    assert.doesNotMatch(plan, /NaN|Infinity/, entry.name);
    const side = renderToStaticMarkup(<ElevationView geometry={geometry} model={model} />);
    assert.doesNotMatch(side, /NaN|Infinity/, entry.name);
    checked++;
  }
  assert.ok(checked > 100);
  console.log(`Checked ${checked} standard definitions`);
});
