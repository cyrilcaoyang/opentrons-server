import { useState } from "react";
import { getLabwareDefinition, getLabStoreDefinition, previewAssembly } from "../lib/api";
import type { CatalogEntry } from "../lib/ot2-catalog";
import type { AssemblyPreview, PlateAssembly } from "../lib/types";
import { geometryFromDefinition } from "../lib/labware-geometry";

/** Fixed stacks retain component identities; the preview comes from the gateway compiler. */
export function AssemblyPicker({ entries, disabled, current, onDeclare }: {
  entries: CatalogEntry[]; disabled: boolean; current?: PlateAssembly | null;
  onDeclare: (assembly: PlateAssembly) => Promise<void>;
}) {
  const [kind, setKind] = useState<PlateAssembly["kind"]>("plate_on_riser");
  const [riser, setRiser] = useState(true);
  const [overlap, setOverlap] = useState("");
  const [top, setTop] = useState<{ plate_id: string; definition: unknown }>({ plate_id: "", definition: null });
  const [collector, setCollector] = useState<{ plate_id: string; definition: unknown }>({ plate_id: "", definition: null });
  const [preview, setPreview] = useState<AssemblyPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const available = entries.filter(e => !e.isTiprack && !!e.rows);
  function invalidate() { setPreview(null); setConfirmed(false); setError(null); }
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError(null);
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  function component(role: "top" | "collector") {
    const item = role === "top" ? top : collector;
    const update = role === "top" ? setTop : setCollector;
    const geometry = geometryFromDefinition(item.definition);
    return <fieldset className="flex flex-col gap-2" disabled={disabled || busy}>
      <legend className="font-semibold">{role === "top" ? (kind === "filter_stack" ? "Top filter plate" : "Well plate") : "Collection plate (covered)"}</legend>
      <input aria-label={`${role} plate ID`} placeholder="Plate ID" value={item.plate_id}
        onChange={e => { invalidate(); update({ ...item, plate_id: e.target.value }); }} />
      <select aria-label={`${role} definition`} value="" onChange={e => {
        const entry = available.find(v => v.key === e.target.value);
        if (!entry) return;
        invalidate();
        void action(async () => update({ ...item, definition: await (entry.category === "labstore" ? getLabStoreDefinition : getLabwareDefinition)(entry.declare) }));
      }}>
        <option value="">Choose a plate definition…</option>
        {available.map(e => <option key={e.key} value={e.key}>{e.label}</option>)}
      </select>
      <label>Or import a plate JSON from the labware builder
        <input type="file" accept=".json,application/json" aria-label={`${role} JSON`} onChange={e => {
          const file = e.target.files?.[0]; if (!file) return;
          invalidate(); void action(async () => update({ ...item, definition: JSON.parse(await file.text()) }));
          e.target.value = "";
        }} />
      </label>
      {geometry && <span>{geometry.displayName} · {geometry.footprintZ} mm high · {Object.keys(geometry.wells).length} wells</span>}
    </fieldset>;
  }
  return <details className="mt-3 rounded border border-slate-300 p-3 text-xs dark:border-slate-700">
    <summary className="cursor-pointer font-semibold">Plate riser / filter assembly</summary>
    {current && <p className="my-2">Declared: {current.top.plate_id}{current.collector ? ` above ${current.collector.plate_id} (covered)` : ""}; riser +{current.riser_height_mm} mm. Component placement is operator-declared.</p>}
    <div className="mt-3 flex flex-col gap-3 [&_input]:rounded [&_input]:border [&_input]:p-1 [&_select]:rounded [&_select]:border [&_select]:p-1">
      <p>Fixed stack for one session. Only the top plate is accessible. End the session before changing or removing a loaded stack.</p>
      <fieldset disabled={disabled || busy} className="flex flex-col gap-3">
        <label>Arrangement <select value={kind} onChange={e => { invalidate(); setKind(e.target.value as PlateAssembly["kind"]); }}>
          <option value="plate_on_riser">Well plate on 5 mm riser</option>
          <option value="filter_stack">96-well filter above collection plate</option>
        </select></label>
        {component("top")}
        {kind === "filter_stack" && <>
          {component("collector")}
          <label><input type="checkbox" checked={riser} onChange={e => { invalidate(); setRiser(e.target.checked); }} /> Add 5 mm riser underneath collector</label>
          <label>Measured nesting overlap (mm) <input type="number" min="0" step="0.01" value={overlap}
            onChange={e => { invalidate(); setOverlap(e.target.value); }} /></label>
          <p>Overlap = collector height + filter height − measured height of the seated pair. Enter 0 only if they do not overlap. Matching A1–H12 well centres are required.</p>
        </>}
        <button type="button" className="rounded border p-2 disabled:opacity-50" disabled={!top.definition || !top.plate_id || (kind === "filter_stack" && (!collector.definition || !collector.plate_id || overlap === ""))}
          onClick={() => void action(async () => {
            setPreview(null); setConfirmed(false);
            setPreview(await previewAssembly({ schema_version: 1, kind,
              riser_height_mm: kind === "plate_on_riser" || riser ? 5 : 0, top,
              collector: kind === "filter_stack" ? collector : null,
              nesting_overlap_mm: kind === "filter_stack" ? Number(overlap) : 0 }));
          })}>Validate and preview assembly</button>
      </fieldset>
      {preview && <>
        <AssemblyElevation preview={preview} />
        <p>Top plate base: {preview.top_origin_z_mm.toFixed(2)} mm above deck. Total height: {preview.total_height_mm.toFixed(2)} mm.</p>
        <label><input type="checkbox" disabled={disabled || busy} checked={confirmed} onChange={e => setConfirmed(e.target.checked)} /> I checked the physical stack, plate definitions, membrane depth, seating and total height. The riser fits within the plate footprint.</label>
        <button type="button" disabled={disabled || busy || !confirmed} className="rounded border p-2 disabled:opacity-50"
          onClick={() => void action(() => onDeclare(preview.assembly))}>Declare assembly in selected slot</button>
      </>}
      {error && <p role="alert" className="text-red-700 dark:text-red-400">{error}</p>}
    </div>
  </details>;
}

function AssemblyElevation({ preview }: { preview: AssemblyPreview }) {
  const { assembly, total_height_mm: height, top_origin_z_mm: origin } = preview;
  const scale = 135 / height;
  const top = geometryFromDefinition(assembly.top.definition)!;
  const collector = assembly.collector && geometryFromDefinition(assembly.collector.definition);
  return <svg viewBox="0 0 350 180" role="img" aria-label="Assembly side view with seated plate heights" className="max-h-48 w-full">
    <line x1="10" x2="340" y1="155" y2="155" stroke="currentColor" />
    {assembly.riser_height_mm > 0 && <rect x="25" y={155 - 5 * scale} width="155" height={5 * scale} fill="#64748b" />}
    {collector && <rect x="25" y={155 - (assembly.riser_height_mm + collector.footprintZ) * scale} width="155" height={collector.footprintZ * scale} fill="#93c5fd" fillOpacity="0.7" stroke="#2563eb" />}
    <rect x="25" y="20" width="155" height={top.footprintZ * scale} fill="#a7f3d0" fillOpacity="0.7" stroke="#059669" />
    <text x="190" y="32" fill="currentColor" fontSize="11">{height.toFixed(2)} mm top</text>
    <text x="190" y={155 - origin * scale} fill="currentColor" fontSize="11">{origin.toFixed(2)} mm plate base</text>
    <text x="25" y="173" fill="currentColor" fontSize="11">Deck · overlapping outlines show nesting</text>
  </svg>;
}
