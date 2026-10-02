import "./browser";
import { formatApiDetail } from "../src/lib/api";
import assert from "node:assert/strict";
import { test } from "node:test";
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { PlanView, ElevationView } from "../src/components/PlateInspector";
import { DeckPanel } from "../src/components/DeckPanel";
import { geometryFromDefinition, frontProjectionColumns } from "../src/lib/labware-geometry";
import { buildWellModel } from "../src/lib/plate-wells";
import { declarationPayload, placeModule, unassignedModules } from "../src/lib/module-placement";
import { AssemblyPicker } from "../src/components/AssemblyPicker";
import { ManualPipettePanel } from "../src/components/ManualPipettePanel";
import { JOG_STEPS, JOG_SPEEDS, validJogSettings, xyzText, copyText, jogLimitReason } from "../src/lib/manual-motion";
import { moveWindow, resizeWindow } from "../src/lib/floating-window";
import { declaredMapFromDeck, pairModuleSlots } from "../src/lib/ot2-deck";
import type { DeviceDeck, PlateAssembly, RobotModule } from "../src/lib/types";
import rack from "../../.venv.test/Lib/site-packages/opentrons_shared_data/data/labware/definitions/2/opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical/3.json";

function modelFor(definition: unknown) {
  const geometry = geometryFromDefinition(definition);
  assert.ok(geometry);
  return { geometry, model: buildWellModel({
    geometry, rows: geometry.rows, columns: geometry.columns,
    isTiprack: geometry.isTiprack, tipRack: null, samples: null,
  }) };
}

test("chat window moves inside the viewport and resizes from every corner", () => {
  const rect = { left: 300, top: 200, right: 760, bottom: 720 };
  assert.deepEqual(resizeWindow(rect, "nw", -30, -40, 1200, 900),
    { left: 270, top: 160, right: 760, bottom: 720 });
  assert.deepEqual(resizeWindow(rect, "ne", 30, -40, 1200, 900),
    { left: 300, top: 160, right: 790, bottom: 720 });
  assert.deepEqual(resizeWindow(rect, "sw", -30, 40, 1200, 900),
    { left: 270, top: 200, right: 760, bottom: 760 });
  assert.deepEqual(resizeWindow(rect, "se", 30, 40, 1200, 900),
    { left: 300, top: 200, right: 790, bottom: 760 });
  assert.deepEqual(resizeWindow(rect, "nw", 1000, 1000, 1200, 900),
    { left: 440, top: 360, right: 760, bottom: 720 });
  assert.deepEqual(moveWindow(rect, -1000, -1000, 1200, 900),
    { left: 16, top: 16, right: 476, bottom: 536 });
  assert.deepEqual(moveWindow(rect, 1000, 1000, 1200, 900),
    { left: 724, top: 364, right: 1184, bottom: 884 });
});

test("camera window resizes from every corner with a compact minimum", () => {
  const rect = { left: 100, top: 80, right: 580, bottom: 390 };
  const limits = { margin: 8, minWidth: 240, minHeight: 180 };
  assert.deepEqual(resizeWindow(rect, "nw", -30, -20, 1200, 900, limits),
    { left: 70, top: 60, right: 580, bottom: 390 });
  assert.deepEqual(resizeWindow(rect, "ne", 30, -20, 1200, 900, limits),
    { left: 100, top: 60, right: 610, bottom: 390 });
  assert.deepEqual(resizeWindow(rect, "sw", -30, 20, 1200, 900, limits),
    { left: 70, top: 80, right: 580, bottom: 410 });
  assert.deepEqual(resizeWindow(rect, "se", 30, 20, 1200, 900, limits),
    { left: 100, top: 80, right: 610, bottom: 410 });
  assert.deepEqual(resizeWindow(rect, "nw", 1000, 1000, 1200, 900, limits),
    { left: 340, top: 210, right: 580, bottom: 390 });
});

test("manual panel provides increments, speed, XYZ copy and stays disarmed initially", () => {
  const html = renderToStaticMarkup(<ManualPipettePanel token="claim" locked={false} offline={false}
    allowedActions={["jog", "pipette_position", "move_to", "stop"]}
    refetch={() => {}} onBusy={() => {}} onError={() => {}} onStop={() => {}} stopping={false} />);
  for (const label of ["Copy XYZ", "XY distance presets", "Z distance presets", "XY speed presets", "Z speed presets", "Move Z", "Move XY"]) assert.ok(html.includes(label));
  assert.match(html, /disabled=""[^>]*>X\+/);
  assert.match(html, /disabled=""[^>]*>Copy XYZ/);
  assert.doesNotMatch(html, /Refresh XYZ|Position unknown|Move to XYZ|Absolute target|Approach well|Target X/);
  assert.doesNotMatch(html, /<summary[^>]*>Pipette<\/summary>/);
  assert.match(html, /role="group" aria-label="Selected pipette"/);
  assert.equal((html.match(/aria-label="Home Z selected pipette"/g) ?? []).length, 1);
  assert.match(html, /aria-label="Home Z selected pipette"[^>]*disabled=""[^>]*>Z Home/);
  assert.match(html, /role="switch" aria-label="Manual" aria-checked="false"/);
  assert.match(html, /No tip recorded/);
  assert.match(html, /Z:/);
  assert.match(html, /aria-expanded="false"/);
  assert.ok(html.includes("DIRECT DRIVE"));
  assert.deepEqual(JOG_STEPS, [0.1, 0.5, 1, 5, 10]);
  assert.ok(JOG_SPEEDS.includes(100));
  assert.ok(validJogSettings(0.1, 1));
  for (const [step, speed] of [[0, 1], [-1, 5], [11, 10], [1, 101], [NaN, 1], [1, Infinity]]) assert.equal(validJogSettings(step, speed), false);
});

test("Copy XYZ preserves readback precision and refuses unknown or nonfinite positions", () => {
  const readback = { pipette: "left", source: "robot" as const, reference: "pipette_critical_point" as const,
    observed_at: "2026-01-01T00:00:00Z", coordinates: { x: 1.1234567, y: 2, z: 3 } };
  assert.deepEqual(JSON.parse(xyzText(readback)), readback.coordinates);
  assert.throws(() => xyzText({ ...readback, coordinates: null }), /No controller/);
  assert.throws(() => xyzText({ ...readback, source: "dry_run" }), /No controller/);
  assert.throws(() => xyzText({ ...readback, coordinates: { x: NaN, y: 2, z: 3 } }), /No controller/);
});

test("Copy XYZ supports HTTP clipboard fallback and reports failure", async () => {
  let selected = false, removed = false, copied = "";
  const originalDocument = globalThis.document;
  const originalElement = globalThis.HTMLElement;
  const textarea = { value: "", style: { cssText: "" }, focus() {}, select() { selected = true; }, remove() { removed = true; } };
  Object.assign(globalThis, { HTMLElement: class {}, document: {
    activeElement: null, createElement: () => textarea, body: { appendChild() {} },
    execCommand: () => { copied = textarea.value; return true; },
  } });
  try {
    await copyText('{"x":1,"y":2,"z":3}');
    assert.equal(copied, '{"x":1,"y":2,"z":3}');
    assert.ok(selected && removed);
    Object.assign(document, { execCommand: () => false });
    await assert.rejects(copyText("coordinates"), /Clipboard unavailable/);
  } finally { Object.assign(globalThis, { document: originalDocument, HTMLElement: originalElement }); }
});

test("assembly identities and embedded definitions survive a full-layout update", () => {
  const assembly: PlateAssembly = { schema_version: 1, kind: "filter_stack", riser_height_mm: 5,
    top: { plate_id: "filter-001", definition: { marker: "filter definition" } },
    collector: { plate_id: "collector-001", definition: { marker: "collector definition" } }, nesting_overlap_mm: 2 };
  const declaration = { kind: "well_plate", load_name: "ac_assembly_test", definition: { marker: "compiled" }, assembly };
  const deck = { source: "run", timestamp: "2026-01-01", slots: {
    "2": { source: "run", slot_state: "occupied", module: null, labware: { kind: "unknown", load_name: "ac_assembly_test" }, declared: declaration },
  } } as DeviceDeck;
  assert.deepEqual(declarationPayload(deck)["2"], declaration);
  const html = renderToStaticMarkup(<AssemblyPicker entries={[]} disabled current={assembly} onDeclare={async () => {}} />);
  assert.match(html, /collector-001.*covered/);
  assert.match(html, /operator-declared/);
  assert.match(html, /Only the top plate is accessible/);
});

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

function moduleDeck(): DeviceDeck {
  return { source: "declared", slots: Object.fromEntries(Array.from({ length: 12 }, (_, i) =>
    [String(i + 1), { source: "empty", slot_state: "empty", module: null, labware: null }])) };
}
const attached: RobotModule = { model: "temperatureModuleV2", type: "temperatureModuleType", serial: "synthetic-module-1" };

test("unassigned attached modules stay in inventory without acquiring deck slots", () => {
  const deck = moduleDeck();
  assert.deepEqual(unassignedModules(deck, [attached]), [attached]);
  assert.equal(pairModuleSlots(deck, [attached]).size, 0);
  const declared = placeModule(deck, null, "11", { module_name: attached.model, serial_number: attached.serial });
  assert.deepEqual(declared["11"], { module_name: attached.model, serial_number: attached.serial });
  assert.equal(deck.slots["11"].slot_state, "empty");
});

test("moving and clearing a declared module preserve unrelated custom labware and module identity", () => {
  const deck = moduleDeck();
  deck.slots["11"] = { source: "declared", slot_state: "declared", labware: null,
    module: { module_name: attached.model, serial_number: attached.serial } };
  deck.slots["2"] = { source: "declared", slot_state: "declared", module: null,
    labware: { kind: "unknown", load_name: "synthetic_mixed", definition: square } };
  const moved = placeModule(deck, "11", "10", deck.slots["11"].module!);
  assert.equal(moved["11"], undefined);
  assert.deepEqual(moved["10"], deck.slots["11"].module);
  assert.deepEqual(moved["2"], deck.slots["2"].labware);
  assert.equal(placeModule(deck, "11", null, deck.slots["11"].module!)["11"], undefined);
  assert.equal(declaredMapFromDeck(deck)["11"], "temperatureModuleV2");
  assert.deepEqual(unassignedModules(deck, [attached]), []);
});

test("module reassignment refuses observed modules, occupied targets and thermocycler overlaps", () => {
  const deck = moduleDeck();
  deck.slots["11"] = { source: "run", slot_state: "occupied", labware: null,
    module: { module_name: attached.model, serial_number: attached.serial } };
  assert.throws(() => placeModule(deck, "11", null, deck.slots["11"].module!), /robot session/);
  assert.throws(() => placeModule(deck, null, "11", { module_name: attached.model }), /occupied/);
  assert.throws(() => placeModule(deck, null, "7", { module_name: "thermocyclerModuleV2" }), /Slot 11 is occupied/);
  assert.throws(() => placeModule(deck, null, "3", { module_name: "thermocyclerModuleV2" }), /slot 7/);
});

test("module pairing never substitutes another serial or guesses between identical modules", () => {
  const deck = moduleDeck();
  deck.slots["11"] = { source: "declared", slot_state: "declared", labware: null,
    module: { module_name: attached.model, serial_number: "absent-module" } };
  assert.deepEqual(unassignedModules(deck, [attached]), [attached]);
  deck.slots["11"].module!.serial_number = null;
  const second = { ...attached, serial: "synthetic-module-2" };
  assert.deepEqual(unassignedModules(deck, [attached, second]), [attached, second]);
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

test("jog disables only destinations outside the supplied limits, including step overshoot", () => {
  const position = { pipette: "left", source: "robot" as const, reference: "pipette_critical_point" as const,
    observed_at: "2026-01-01T00:00:00Z", coordinates: { x: 99, y: 0, z: 50 },
    coordinate_limits: { x: [0, 100] as [number, number], y: [0, 100] as [number, number], z: [0, 100] as [number, number] } };
  assert.equal(jogLimitReason(position, "x", 1), null);
  assert.match(jogLimitReason(position, "x", 5)!, /X target 104/);
  assert.equal(jogLimitReason(position, "x", -5), null);
  assert.match(jogLimitReason(position, "y", -0.1)!, /Y target/);
  assert.equal(jogLimitReason(position, "y", 0.1), null);
  assert.match(jogLimitReason(null, "z", 1)!, /Waiting for position/);
});

test("validation errors show the field and reason instead of object strings", () => {
  assert.equal(formatApiDetail([{ loc: ["body", "coordinates", "x"], msg: "Must be at most 100" }]), "coordinates.x: Must be at most 100");
});


test("Tapo viewing uses broker authorization and releases its lease on close", async () => {
  const { openCameraSession } = await import("../src/lib/camera-session");
  const oldFetch = globalThis.fetch;
  const oldSocket = globalThis.WebSocket;
  const calls: Array<{url: string; method?: string}> = [];
  class Socket extends EventTarget {
    sent: string[] = [];
    closed = false;
    constructor(public url: string) { super(); }
    send(value: string) { this.sent.push(value); }
    close() { this.closed = true; }
  }
  Object.assign(globalThis, { WebSocket: Socket, fetch: async (url: string, options: RequestInit) => {
    calls.push({ url, method: options.method });
    return { ok: true, json: async () => ({ id: "test-session", ticket: "test-ticket", heartbeat_seconds: 20 }) };
  } });
  try {
    const abort = new AbortController();
    const socket = await openCameraSession("/streams/api/ws?src=cam_echem_tapo_c100_main", abort.signal, () => {}) as unknown as Socket;
    assert.equal(calls[0].url, "/api/camera-streams/sessions");
    assert.equal(socket.url, "ws://localhost/api/camera-streams/ws");
    socket.dispatchEvent(new Event("open"));
    assert.deepEqual(JSON.parse(socket.sent[0]), { type: "session", value: "test-ticket" });
    abort.abort();
    assert.equal(socket.closed, true);
    assert.ok(calls.some(c => c.method === "DELETE" && c.url.endsWith("/test-session")));
  } finally { Object.assign(globalThis, { fetch: oldFetch, WebSocket: oldSocket }); }
});

test("Tapo viewing refuses denied admission without opening a socket", async () => {
  const { openCameraSession } = await import("../src/lib/camera-session");
  const oldFetch = globalThis.fetch;
  Object.assign(globalThis, { fetch: async () => ({ ok: false, status: 403, json: async () => ({ detail: "Camera access denied" }) }) });
  try {
    await assert.rejects(openCameraSession("/streams/api/ws?src=cam_echem_tapo_c100_main", new AbortController().signal, () => {}), /Camera access denied/);
  } finally { Object.assign(globalThis, { fetch: oldFetch }); }
});


test("position polling waits for each read and stops scheduling after cleanup", async () => {
  const { startPositionPolling } = await import("../src/lib/position-polling");
  const oldSet = globalThis.setTimeout, oldClear = globalThis.clearTimeout;
  const tasks = new Map<number, () => Promise<void>>();
  let next = 0, reads = 0, allowed = false;
  let finish!: () => void;
  Object.assign(globalThis, {
    setTimeout: (fn: () => Promise<void>) => { tasks.set(++next, fn); return next; },
    clearTimeout: (id: number) => { tasks.delete(id); },
  });
  try {
    const stop = startPositionPolling(() => allowed, async () => { reads++; await new Promise<void>(resolve => { finish = resolve; }); });
    const first = tasks.get(1)!; tasks.delete(1); await first();
    assert.equal(reads, 0);
    allowed = true;
    const second = tasks.get(2)!; tasks.delete(2); const pending = second();
    assert.equal(reads, 1);
    assert.equal(tasks.size, 0); // No next timer during an outstanding read.
    stop(); finish(); await pending;
    assert.equal(tasks.size, 0);
  } finally { Object.assign(globalThis, { setTimeout: oldSet, clearTimeout: oldClear }); }
});

test("balance has three claim-gated controls and never becomes a native module declaration", async () => {
  const { PlateBalanceControls } = await import("../src/components/PlateBalanceControls");
  const balance = { configured: true, model: "WZB254-N", reading: null, last_error: null, last_operation: null };
  const html = renderToStaticMarkup(<PlateBalanceControls balance={balance} disabled={true}
    allowedActions={["platebalance.read", "platebalance.tare", "platebalance.zero"]} onAction={() => {}} />);
  for (const label of ["Weight", "Tare", "Zero"]) assert.match(html, new RegExp(`disabled=""[^>]*>${label}</button>`));
  assert.match(html, /Weight read: wait until stable/);
  assert.match(html, /checked=""/);
  const deck: DeviceDeck = { slots: { "9": { labware: null, module: { module_name: "platebalanceV1", local_peripheral: true }, source: "declared", slot_state: "declared" } } } as DeviceDeck;
  assert.deepEqual(declarationPayload(deck), {});
  assert.throws(() => placeModule(deck, "9", null, { module_name: "platebalanceV1" }), /fixed slot/);
});


test("balance reading keeps aligned digits and omits timestamp", async () => {
  const { PlateBalanceControls } = await import("../src/components/PlateBalanceControls");
  const html = renderToStaticMarkup(<PlateBalanceControls balance={{ configured: true,
    model: "WZB254-N", reading: {value: 9.9999, unit: "g", stable: true, observed_at: "2026-09-30T00:00:00Z"},
    last_error: null, last_operation: null }} disabled={false} allowedActions={[]} onAction={() => {}} />);
  assert.match(html, /whitespace-pre/);
  assert.match(html, />  9.9999<\/span>/);
  assert.doesNotMatch(html, /Last read|2026-09-30/);
});


test("clearing labware keeps module declarations and serial identities", async () => {
  const { moduleDeclarationPayload } = await import("../src/lib/module-placement");
  const module = { module_name: "temperatureModuleV2", serial_number: "synthetic" };
  const deck = { slots: {
    "3": { source: "declared", module, declared_module: module },
    "4": { source: "declared", labware: { kind: "96-well", load_name: "plate" } },
    "9": { source: "declared", module: { module_name: "platebalanceV1", local_peripheral: true } },
  } } as unknown as DeviceDeck;
  assert.deepEqual(moduleDeclarationPayload(deck), { "3": module });
});

test("balance declarations preserve the plate and never submit the peripheral as labware", () => {
  const plate = { kind: "96-well", load_name: "custom_plate", support_module: "platebalanceV1" as const,
    definition: { dimensions: { zDimension: 14 } } };
  const deck = { slots: {
    "9": { source: "declared", slot_state: "declared", module: { module_name: "platebalanceV1", local_peripheral: true },
      labware: plate, declared: plate },
  } } as unknown as DeviceDeck;
  assert.deepEqual(declaredMapFromDeck(deck), { "9": "custom_plate" });
  assert.deepEqual(declarationPayload(deck), { "9": plate });
  deck.slots["9"].labware = null;
  deck.slots["9"].declared = null;
  assert.deepEqual(declaredMapFromDeck(deck), {});
  assert.deepEqual(declarationPayload(deck), {});
});

test("balance picker hides modules, nonplates and known plates at or above 25 mm", async () => {
  const { DeclarePicker } = await import("../src/components/DeclarePicker");
  const { catalogEntryFromLabware } = await import("../src/lib/ot2-catalog");
  const entries = [
    { load_name: "short_custom", display_category: "wellPlate", height_mm: 24.999 },
    { load_name: "tall_custom", display_category: "wellPlate", height_mm: 25 },
    { load_name: "reservoir_custom", display_category: "reservoir", height_mm: 14 },
  ].map(e => catalogEntryFromLabware({ ...e, display_name: e.load_name }));
  const html = renderToStaticMarkup(<DeclarePicker selectedSlot={9} currentDeclare={null} locked={false}
    balanceOnly customEntries={entries} onDeclare={() => {}} />);
  assert.match(html, /short_custom/);
  assert.doesNotMatch(html, /tall_custom|reservoir_custom|Temperature module/);
});
