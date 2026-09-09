/**
 * Expanded view of the deck slot selected in the DeckPanel — the top of the
 * right column.
 *
 * Two drawings of the same labware, from the same per-well model
 * (`lib/plate-wells.ts`):
 *
 * - **Plan** (top-down): every well, with row/column labels, coloured by real
 *   state — a tip rack's fresh / touched / empty from `details.tip_racks`, a
 *   plate's tracked samples from the slot's own wells. Wells whose tips are on
 *   a pipette right now are ringed.
 * - **Elevation** (side cross-section): the labware's true profile in
 *   millimetres, in the manner of the dashboard's `utils/labware_builder`,
 *   drawn as an outline with one cavity per *column*. Only a tip rack's tips
 *   are shaded — by that column's aggregate state — since they are the solid
 *   the section cuts; bodies and plate wells stay hollow. Columns are the
 *   right unit here: an 8-channel head takes a whole column, so "partly
 *   consumed" is the fact worth seeing.
 *
 * Honesty rules this component exists to keep:
 * - A rack the gateway has never registered renders `unknown`, never as 96
 *   fresh tips. "Not tracked" and "full" are different answers.
 * - The elevation is drawn **only** from a real definition. Without one it says
 *   so, rather than inventing a footprint and well depth to scale against.
 */

import { useEffect, useMemo, useState } from "react";

import {
  ApiError,
  getLabStoreDefinition,
  getLabwareDefinition,
  labStoreAvailable,
} from "../lib/api";
import {
  elevationProfile,
  geometryFromDefinition,
  wellHalfX,
  wellHalfY,
  type LabwareGeometry,
} from "../lib/labware-geometry";
import type { MountedTip, SlotView, TipRackSummary } from "../lib/ot2-deck";
import {
  buildWellModel,
  type PlateWellModel,
  type WellCell,
  type WellKind,
} from "../lib/plate-wells";

// ---------------------------------------------------------------------------
// Palette — one source for both drawings and the legend
// ---------------------------------------------------------------------------

const WELL_FILL: Record<WellKind, string> = {
  fresh: "fill-sky-300 stroke-sky-600 dark:fill-sky-800 dark:stroke-sky-400",
  touched: "fill-amber-300 stroke-amber-600 dark:fill-amber-700 dark:stroke-amber-400",
  // An empty well is a hole: no fill, dashed edge.
  empty: "fill-transparent stroke-slate-400 dark:stroke-slate-500",
  // Also a hole — but one whose tip is on a head and may come back. Hollow
  // like `empty` so presence reads correctly at a glance, in the ring colour
  // that marks a mounted tip everywhere else in this panel.
  mounted: "fill-transparent stroke-violet-500 dark:stroke-violet-400",
  sample: "fill-sky-300 stroke-sky-600 dark:fill-sky-800 dark:stroke-sky-400",
  vacant: "fill-slate-100 stroke-slate-300 dark:fill-slate-800 dark:stroke-slate-600",
  unknown: "fill-slate-200 stroke-slate-300 dark:fill-slate-700 dark:stroke-slate-600",
};

/**
 * Tip tint for the elevation's filled portion. Sky for tips that are fresh
 * (or reusable for their sample); amber as soon as the column carries a used
 * tip; slate for a rack the tracker has no record of. A column with no tips
 * is drawn hollow like the rack around it — an absent tip should not read as
 * a solid.
 */
const ELEVATION_FILL = {
  fresh: "fill-sky-300 dark:fill-sky-800",
  touched: "fill-amber-300 dark:fill-amber-700",
  unknown: "fill-slate-200 dark:fill-slate-700",
} as const;

const KIND_LABEL: Record<WellKind, string> = {
  fresh: "fresh",
  touched: "used",
  mounted: "on a pipette",
  empty: "empty",
  sample: "sample",
  vacant: "empty",
  unknown: "not tracked",
};

/** Legend swatch — a div, so it matches the SVG fills without duplicating them. */
function Swatch({ kind }: { kind: WellKind }) {
  const dashed = kind === "empty" || kind === "mounted";
  return (
    <span
      className={[
        "inline-block h-2.5 w-2.5 shrink-0 rounded-full border",
        dashed ? "border-dashed" : "",
        kind === "fresh" || kind === "sample"
          ? "border-sky-600 bg-sky-300 dark:border-sky-400 dark:bg-sky-800"
          : kind === "touched"
            ? "border-amber-600 bg-amber-300 dark:border-amber-400 dark:bg-amber-700"
            : kind === "empty"
              ? "border-slate-400 bg-transparent dark:border-slate-500"
              : kind === "mounted"
                ? "border-violet-500 bg-transparent dark:border-violet-400"
              : kind === "vacant"
                ? "border-slate-300 bg-slate-100 dark:border-slate-600 dark:bg-slate-800"
                : "border-slate-300 bg-slate-200 dark:border-slate-600 dark:bg-slate-700",
      ].join(" ")}
      aria-hidden
    />
  );
}

const ROW_LETTERS = "ABCDEFGHIJKLMNOP";

function wellTitle(cell: WellCell, contents: "tiprack" | "plate"): string {
  const parts = [cell.well];
  if (cell.kind === "touched") parts.push(`tip touched ${cell.detail ?? "a sample"}`);
  else if (cell.kind === "mounted") parts.push("tip is on a pipette");
  else if (cell.kind === "sample") parts.push(cell.detail ? `sample ${cell.detail}` : "sample");
  else parts.push(contents === "tiprack" ? `tip ${KIND_LABEL[cell.kind]}` : KIND_LABEL[cell.kind]);
  if (cell.volumeUl != null) parts.push(`${cell.volumeUl} µL`);
  if (cell.mounted) parts.push("on a pipette now");
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// Plan (top-down)
// ---------------------------------------------------------------------------

/**
 * Top-down plan. Uses the definition's real well positions when we have them,
 * else a uniform grid laid out on the standard 9 mm pitch — for the regular
 * SBS grids this panel shows, a synthesized plan is geometrically the same
 * drawing, so it is safe here in a way the elevation is not.
 */
function PlanView({
  model,
  geometry,
}: {
  model: PlateWellModel;
  geometry: LabwareGeometry | null;
}) {
  const { rows, columns } = model;
  // Synthesized frame: 9 mm pitch inside a standard SBS footprint.
  const pitch = 9;
  const margin = 10;
  const frameW = geometry?.footprintX ?? columns * pitch + margin * 2;
  const frameH = geometry?.footprintY ?? rows * pitch + margin * 2;
  const labelPad = 7;

  return (
    <svg
      viewBox={`${-labelPad} ${-labelPad} ${frameW + labelPad * 2} ${frameH + labelPad * 2}`}
      className="w-full"
      role="img"
      aria-label={`Top-down view of ${columns} × ${rows} wells`}
    >
      <rect
        x={0}
        y={0}
        width={frameW}
        height={frameH}
        rx={2}
        className="fill-slate-50 stroke-slate-400 dark:fill-slate-800 dark:stroke-slate-500"
        strokeWidth={0.6}
      />
      {/* Column numbers along the top, row letters down the left. */}
      {Array.from({ length: columns }, (_, c) => {
        const well = geometry?.ordering[c]?.[0];
        const g = well ? geometry?.wells[well] : undefined;
        const cx = g ? g.x : margin + pitch / 2 + c * pitch;
        return (
          <text
            key={`col-${c}`}
            x={cx}
            y={-1.5}
            textAnchor="middle"
            className="fill-slate-500 dark:fill-slate-400"
            style={{ fontSize: 4.2 }}
          >
            {c + 1}
          </text>
        );
      })}
      {Array.from({ length: rows }, (_, r) => {
        const well = geometry?.ordering[0]?.[r];
        const g = well ? geometry?.wells[well] : undefined;
        // Definition y grows toward the back; SVG y grows downward.
        const cy = g ? frameH - g.y : margin + pitch / 2 + r * pitch;
        return (
          <text
            key={`row-${r}`}
            x={-2}
            y={cy + 1.5}
            textAnchor="end"
            className="fill-slate-500 dark:fill-slate-400"
            style={{ fontSize: 4.2 }}
          >
            {ROW_LETTERS[r] ?? "?"}
          </text>
        );
      })}
      {model.cells.map((cell) => {
        const g = geometry?.wells[cell.well];
        const cx = g ? g.x : margin + pitch / 2 + cell.column * pitch;
        const cy = g ? frameH - g.y : margin + pitch / 2 + cell.row * pitch;
        const rx = g ? wellHalfX(g) : pitch * 0.38;
        const ry = g ? wellHalfY(g) : pitch * 0.38;
        const rectangular = g?.shape === "rectangular";
        return (
          <g key={cell.well}>
            {rectangular ? (
              <rect
                x={cx - rx}
                y={cy - ry}
                width={rx * 2}
                height={ry * 2}
                className={WELL_FILL[cell.kind]}
                strokeWidth={0.4}
                strokeDasharray={cell.kind === "empty" || cell.kind === "mounted" ? "1 1" : undefined}
              />
            ) : (
              <ellipse
                cx={cx}
                cy={cy}
                rx={rx}
                ry={ry}
                className={WELL_FILL[cell.kind]}
                strokeWidth={0.4}
                strokeDasharray={cell.kind === "empty" || cell.kind === "mounted" ? "1 1" : undefined}
              />
            )}
            {cell.mounted && (
              <ellipse
                cx={cx}
                cy={cy}
                rx={rx + 1}
                ry={ry + 1}
                className="fill-none stroke-violet-500 dark:stroke-violet-400"
                strokeWidth={0.6}
              />
            )}
            <title>{wellTitle(cell, model.contents)}</title>
          </g>
        );
      })}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Elevation (side cross-section)
// ---------------------------------------------------------------------------

/**
 * Front elevation, to scale, from the real definition only. The body's top edge
 * comes from `elevationProfile` — `footprintZ` flat for ordinary labware, the
 * true steps for a stepped block such as the calibration block. Each column's
 * cavity hangs from its well mouth (`z + depth`) down to its floor (`z`), so a
 * tip rack's tips correctly stand proud of the rack body while a plate's wells
 * sit inside it. Tip-rack cavities taper (a tip is conical); plate wells are
 * drawn straight-sided.
 *
 * A column's 8 wells sit behind one another in this projection, so each
 * column is one shape — filled **proportionally, bottom-up**, to the tips it
 * still holds (7/8 present draws 7/8 full, like a level gauge). It used to
 * collapse any partial column to a solid amber "mixed", which read as "this
 * whole column is used" when a single tip was missing. Amber still appears,
 * but only when the column actually carries a used tip; the tooltip gives
 * the exact count.
 */
function ElevationView({
  geometry,
  model,
}: {
  geometry: LabwareGeometry;
  model: PlateWellModel;
}) {
  const { footprintX, footprintZ } = geometry;
  const shadeTips = model.contents === "tiprack" && geometry.isTiprack;
  const profile = elevationProfile(geometry);
  const cavities = geometry.ordering.map((column, index) => {
    // A front elevation intersects the well nearest the operator, not merely
    // the first name in definition ordering. They coincide for ordinary
    // plates, but differ for heterogeneous tube racks whose rows have
    // different diameters and depths.
    const g = column
      .map((well) => geometry.wells[well])
      .filter((well): well is NonNullable<typeof well> => well != null)
      .reduce<NonNullable<(typeof geometry.wells)[string]> | null>(
        (front, well) => (front == null || well.y < front.y ? well : front),
        null,
      );
    // A zero-depth well is a top face, not a cavity — it is part of the body
    // silhouette below, and drawing it here would only be a line along it.
    if (!g || g.depth === 0) return null;
    const halfX = wellHalfX(g);
    const mouthY = Math.max(0, footprintZ - (g.z + g.depth)); // SVG y of the well mouth
    const floorY = Math.min(footprintZ, footprintZ - g.z); // SVG y of the well floor
    // Half-width of the cavity at depth fraction t (0 = mouth, 1 = floor):
    // tapered to ~25% for a tip rack, straight-sided for a plate.
    const taper = (t: number) => (geometry.isTiprack ? halfX * (1 - 0.75 * t) : halfX);
    const outline = `${g.x - taper(0)},${mouthY} ${g.x + taper(0)},${mouthY} ${
      g.x + taper(1)
    },${floorY} ${g.x - taper(1)},${floorY}`;

    let level: React.ReactNode = null;
    let label = `Column ${index + 1}`;
    if (shadeTips) {
      const cells = model.cells.filter((w) => w.column === index);
      const total = cells.length || 1;
      const present = cells.filter(
        (w) => w.kind === "fresh" || w.kind === "sample" || w.kind === "touched",
      ).length;
      const untracked = cells.length > 0 && cells.every((w) => w.kind === "unknown");
      const anyTouched = cells.some((w) => w.kind === "touched");
      // A hole whose tip is on a head is still a hole (it is not in `present`),
      // but calling it "empty" would send an operator looking for a tip that is
      // on the robot, not in the bin.
      const upCount = cells.filter((w) => w.kind === "mounted").length;
      const fraction = untracked ? 1 : present / total;
      const fill = untracked
        ? ELEVATION_FILL.unknown
        : anyTouched
          ? ELEVATION_FILL.touched
          : ELEVATION_FILL.fresh;
      label = untracked
        ? `Column ${index + 1} — not tracked`
        : `Column ${index + 1} — ${present}/${total} tips` +
          (total - present - upCount > 0
            ? ` (${total - present - upCount} empty)`
            : "") +
          (upCount > 0 ? ` (${upCount} on a pipette)` : "") +
          (anyTouched ? ", incl. used" : "");
      if (fraction > 0) {
        const tCut = 1 - fraction;
        const yCut = mouthY + (floorY - mouthY) * tCut;
        level = (
          <polygon
            points={`${g.x - taper(tCut)},${yCut} ${g.x + taper(tCut)},${yCut} ${
              g.x + taper(1)
            },${floorY} ${g.x - taper(1)},${floorY}`}
            className={fill}
          />
        );
      }
    }
    return (
      <g key={column[0]}>
        {level}
        <polygon
          points={outline}
          className="fill-none stroke-slate-500 dark:stroke-slate-400"
          strokeWidth={0.3}
        />
        <title>{label}</title>
      </g>
    );
  });

  return (
    <svg
      viewBox={`-2 -2 ${footprintX + 4} ${footprintZ + 4}`}
      className="w-full"
      role="img"
      aria-label="Side-view labware cross-section"
    >
      {/* The body is an outline only; a section drawing shades the solids it
          cuts, and here that is just a tip rack's tips. Its top edge follows
          the profile, so a stepped block shows its step. */}
      <polygon
        points={[
          `0,${footprintZ}`,
          ...profile.flatMap((step) => [
            `${step.x0},${footprintZ - step.top}`,
            `${step.x1},${footprintZ - step.top}`,
          ]),
          `${footprintX},${footprintZ}`,
        ].join(" ")}
        className="fill-none stroke-slate-400 dark:stroke-slate-500"
        strokeWidth={0.6}
      />
      {cavities}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export interface PlateInspectorProps {
  slot: number | string | null;
  view: SlotView | null;
  /** Tracker summaries from `details.tip_racks`, joined by slot nickname. */
  tipRacks: TipRackSummary[];
  mountedTips: MountedTip[];
}

/**
 * Resolve preview geometry. A custom definition carried by the live deck wins:
 * it is the exact object the operator declared, whereas `/labware/{name}` only
 * knows the gateway's installed standard library. Standard definitions are
 * fetched and cached by load name as before.
 */
function useLabwareGeometry(loadName: string | undefined, declaredDefinition: unknown | null | undefined): {
  geometry: LabwareGeometry | null;
  state: "idle" | "loading" | "ready" | "unavailable";
} {
  const [cache, setCache] = useState<Record<string, LabwareGeometry | null>>({});
  const [loading, setLoading] = useState<string | null>(null);
  const hasDeclaredDefinition = declaredDefinition != null;
  const declaredGeometry = useMemo(
    () => (hasDeclaredDefinition ? geometryFromDefinition(declaredDefinition) : null),
    [declaredDefinition, hasDeclaredDefinition],
  );

  useEffect(() => {
    if (hasDeclaredDefinition || !loadName || loadName in cache) return;
    let cancelled = false;
    setLoading(loadName);
    getLabwareDefinition(loadName)
      .catch((err: unknown) => {
        // 404 = not a standard definition (or shared-data absent). Before
        // giving up, try the dashboard's labware store — lab-custom
        // definitions live there, not in opentrons-shared-data. The store is
        // only reachable when the SPA is served through the edge.
        if (err instanceof ApiError && err.status === 404 && labStoreAvailable) {
          return getLabStoreDefinition(loadName);
        }
        throw err;
      })
      .then((defn) => {
        if (!cancelled) setCache((c) => ({ ...c, [loadName]: geometryFromDefinition(defn) }));
      })
      .catch((err: unknown) => {
        // 404 on both = unknown load_name; either way, no elevation.
        // Anything else is also non-fatal: this is a drawing.
        if (!cancelled) {
          if (!(err instanceof ApiError)) console.warn("labware definition fetch failed", err);
          setCache((c) => ({ ...c, [loadName]: null }));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(null);
      });
    return () => {
      cancelled = true;
    };
  }, [loadName, cache, hasDeclaredDefinition]);

  if (!loadName) return { geometry: null, state: "idle" };
  if (hasDeclaredDefinition) {
    return { geometry: declaredGeometry, state: declaredGeometry ? "ready" : "unavailable" };
  }
  if (loading === loadName) return { geometry: null, state: "loading" };
  if (!(loadName in cache)) return { geometry: null, state: "loading" };
  const geometry = cache[loadName];
  return { geometry, state: geometry ? "ready" : "unavailable" };
}

export function PlateInspector({ slot, view, tipRacks, mountedTips }: PlateInspectorProps) {
  const nickname = view?.nickname ?? null;
  const isTiprack = view?.isTiprack ?? false;
  const loadName = view?.loadName;
  const { geometry, state: geometryState } = useLabwareGeometry(loadName, view?.definition);

  if (slot == null || view == null) {
    return (
      <p className="text-xs text-ink-subtle dark:text-slate-500">
        Select a deck slot to inspect its labware.
      </p>
    );
  }
  // A selected-but-empty slot is a different answer from "nothing selected".
  // The two shared a message while these lived in separate panels; merged
  // under a "Slot 5" heading, "select a deck slot" would contradict the
  // heading directly above it.
  if (view.state === "empty") {
    return (
      <p className="text-xs text-ink-subtle dark:text-slate-500">
        Slot {slot} is empty — nothing to inspect.
      </p>
    );
  }
  if (view.isTrash) {
    return (
      <p className="text-xs text-ink-subtle dark:text-slate-500">
        Slot {slot} is the waste chute — no wells to show.
      </p>
    );
  }

  const rows = geometry?.rows ?? view.rows;
  const columns = geometry?.columns ?? view.columns;
  if (rows <= 0 || columns <= 0) {
    return (
      <p className="text-xs text-ink-subtle dark:text-slate-500">
        Slot {slot} holds <span className="font-medium">{view.label || view.kind}</span>, which has
        no well grid to draw.
      </p>
    );
  }

  // Joined by slot: a rack's identity is where it sits, so this resolves even
  // on a deck that was declared rather than loaded by a protocol setup.
  const tipRack = slot != null ? (tipRacks.find((r) => r.slot === String(slot)) ?? null) : null;
  const model = buildWellModel({
    isTiprack,
    rows,
    columns,
    geometry,
    tipRack,
    samples: view.wells ?? null,
    mountedTips,
    slot,
    nickname,
  });

  const legendKinds = (
    model.tracked
      ? model.contents === "tiprack"
        ? (["fresh", "touched", "mounted", "empty"] as WellKind[])
        : (["sample", "vacant"] as WellKind[])
      : (["unknown"] as WellKind[])
  ).filter((k) => (model.counts[k] ?? 0) > 0 || k === "unknown");

  const availableLabel =
    model.contents === "tiprack" && model.tracked
      ? `${model.counts.fresh ?? 0}/${model.total} available`
      : model.contents === "plate" && model.tracked
        ? `${model.counts.sample ?? 0}/${model.total} with sample`
        : `${model.total} wells`;

  return (
    <div className="flex flex-col gap-2">
      {/* Identity */}
      <div className="flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate text-xs font-semibold text-ink dark:text-slate-200">
          Slot {slot}
          {nickname && <span className="font-mono font-normal"> · {nickname}</span>}
        </span>
        <span className="shrink-0 text-xs tabular-nums text-ink-subtle dark:text-slate-400">
          {availableLabel}
        </span>
      </div>
      {loadName && (
        <span className="-mt-1 truncate font-mono text-[10px] text-ink-subtle dark:text-slate-500">
          {loadName}
        </span>
      )}

      {!model.tracked && (
        <p className="rounded border border-slate-200 bg-slate-50 px-2 py-1 text-[10px] text-ink-subtle dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-400">
          {model.contents === "tiprack"
            ? "This rack is not registered with the tip tracker — its tips are unknown, which is not the same as full."
            : "No plate samples are tracked for this slot."}
        </p>
      )}

      {/* Plan */}
      <div className="rounded border border-slate-200 bg-white p-1.5 dark:border-slate-700 dark:bg-slate-900">
        <PlanView model={model} geometry={geometry} />
      </div>

      {/* Legend + counts */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-ink-subtle dark:text-slate-400">
        {legendKinds.map((kind) => (
          <span key={kind} className="flex items-center gap-1">
            <Swatch kind={kind} />
            {KIND_LABEL[kind]}
            {model.counts[kind] != null && (
              <span className="tabular-nums">{model.counts[kind]}</span>
            )}
          </span>
        ))}
        {/* Only when a mount is known but the rack has not been marked — an
            older gateway, or a tip whose origin the tracker never saw. The
            `mounted` kind above already carries the normal case. */}
        {model.cells.some((c) => c.mounted && c.kind !== "mounted") && (
          <span className="flex items-center gap-1">
            <span
              className="inline-block h-2.5 w-2.5 shrink-0 rounded-full border-2 border-violet-500 dark:border-violet-400"
              aria-hidden
            />
            on a pipette
          </span>
        )}
      </div>

      {/* Elevation */}
      <div className="mt-1 border-t border-slate-100 pt-2 dark:border-slate-800">
        <p className="mb-1 text-[10px] uppercase tracking-wider text-ink-subtle dark:text-slate-500">
          Side view {geometryState === "ready" && "(front elevation, to scale)"}
        </p>
        {geometryState === "ready" && geometry ? (
          <>
            <div className="rounded border border-slate-200 bg-white p-1.5 dark:border-slate-700 dark:bg-slate-900">
              <ElevationView geometry={geometry} model={model} />
            </div>
            <p className="mt-1 text-[10px] tabular-nums text-ink-subtle dark:text-slate-500">
              {geometry.footprintX.toFixed(1)} × {geometry.footprintY.toFixed(1)} ×{" "}
              {geometry.footprintZ.toFixed(1)} mm
              {geometry.tipLength != null && ` · tip ${geometry.tipLength.toFixed(1)} mm`}
              {geometry.wellVolumeUl != null && ` · well ${geometry.wellVolumeUl} µL`}
              {" · columns tinted by aggregate state"}
            </p>
          </>
        ) : geometryState === "loading" ? (
          <p className="text-[10px] text-ink-subtle dark:text-slate-500">Loading definition…</p>
        ) : (
          <p className="text-[10px] text-ink-subtle dark:text-slate-500">
            No definition available for{" "}
            <span className="font-mono">{loadName || view.kind || "this labware"}</span>, so the
            elevation would have to invent its dimensions. Omitted.
          </p>
        )}
      </div>
    </div>
  );
}
