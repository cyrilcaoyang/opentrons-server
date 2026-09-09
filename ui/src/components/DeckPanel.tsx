import type { DeviceDeck, RobotModule } from "../lib/types";
import {
  deckRows,
  TEMP_FAMILIES,
  buildSlotView,
  computeModuleFootprints,
  moduleFamily,
  moduleShortLabel,
  pairModuleSlots,
  type SlotView,
  type TipRackSummary,
} from "../lib/ot2-deck";
import { buildWellModel } from "../lib/plate-wells";
import { PlanView, useLabwareGeometry } from "./PlateInspector";

function formatTemp(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1);
}

// Compact live readout for a temperature-capable module: current temp, target
// (when set), and the module's own status word. Renders gracefully with no
// telemetry (module declared but unpowered / not yet observed): "— °C · offline".
export function ModuleReadout({ live, compact }: { live: RobotModule | null; compact?: boolean }) {
  const cur = live?.current_temperature;
  const tgt = live?.target_temperature;
  const status = live?.status ?? "offline";
  const active = status === "heating" || status === "cooling";
  return (
    <div className="flex min-w-0 flex-col items-center gap-0.5">
      <span
        className={[
          compact ? "text-sm" : "text-xl",
          "font-semibold tabular-nums",
          cur == null ? "text-slate-400 dark:text-slate-500" : "text-ink dark:text-slate-100",
        ].join(" ")}
      >
        {formatTemp(cur)} °C
        {tgt != null && (
          <span className="font-medium text-amber-600 dark:text-amber-400"> → {formatTemp(tgt)} °C</span>
        )}
      </span>
      <span
        className={[
          "text-[9px] uppercase tracking-wider",
          active ? "text-amber-600 dark:text-amber-400" : "text-ink-subtle dark:text-slate-400",
        ].join(" ")}
      >
        {status}
      </span>
    </div>
  );
}

/** Use the same definition coordinates and shapes as the detailed inspector. */
function LabwareThumbnail({ view, slot, tipRacks }: {
  view: SlotView;
  slot: number | string;
  tipRacks: TipRackSummary[];
}) {
  const { geometry, state } = useLabwareGeometry(view.loadName, view.definition);
  if (!geometry && view.loadName) {
    return <div className="flex h-full items-center justify-center p-2 text-center text-[10px] text-slate-500">
      {state === "loading" ? "Loading geometry…" : "Geometry unavailable"}
    </div>;
  }
  if (!geometry && (!view.rows || !view.columns)) return null;
  const model = buildWellModel({
    isTiprack: geometry?.isTiprack ?? view.isTiprack ?? false,
    rows: geometry?.rows ?? view.rows,
    columns: geometry?.columns ?? view.columns,
    geometry,
    tipRack: tipRacks.find((rack) => rack.slot === String(slot)) ?? null,
    samples: view.wells ?? null,
    slot,
  });
  return <div className="h-full w-full p-1.5">
    <PlanView model={model} geometry={geometry} compact />
  </div>;
}

export interface DeckPanelProps {
  /** The gateway's normalized deck (details.snapshot.deck). */
  deviceDeck: DeviceDeck | null;
  /** Legacy store slots (slot -> kind); ignored when deviceDeck set. */
  legacyLabware?: Record<string, string>;
  /** Live module telemetry (details.robot.modules) for readout pairing. */
  robotModules?: RobotModule[];
  selectedSlot?: number | string | null;
  /** Omit for a read-only deck (cells render as plain, non-clickable tiles). */
  onSelectSlot?: (slot: number | string | null) => void;
  /** "tile" = fixed 160×120 cells; "page" = responsive full-width cells. */
  variant?: "tile" | "page";
  /** Tip-tracker summaries (`details.tip_racks`). When given, a tip rack's
   *  wells are tinted by real state instead of drawn uniformly full. */
  tipRacks?: TipRackSummary[];
}

/**
 * The 12-slot OT-2 deck (slot 1 bottom-left … 12 top-right, rendered top row
 * first to match the physical deck). Declared vs observed state, mismatch
 * flags, module accent, physical multi-slot module footprints, and live
 * temperature readouts all come from the shared ot2-deck lib.
 * Ported from the ac-organic-lab dashboard.
 */
export function DeckPanel({
  deviceDeck,
  legacyLabware = {},
  robotModules = [],
  selectedSlot = null,
  onSelectSlot,
  variant = "tile",
  tipRacks = [],
}: DeckPanelProps) {
  const rows = deckRows(deviceDeck);
  const columns = rows[0].length;
  const migrated = deviceDeck != null;
  const page = variant === "page";
  const interactive = onSelectSlot != null;

  const moduleSlots = pairModuleSlots(deviceDeck, robotModules);
  const moduleFootprints = computeModuleFootprints(deviceDeck);

  const grid = (
    <div
      className={
        page
          ? "grid w-full gap-x-2 gap-y-1 sm:gap-x-3 sm:gap-y-1.5"
          : "grid justify-center gap-[10px] overflow-x-auto"
      }
      style={{ gridTemplateColumns: page ? `repeat(${columns}, minmax(0, 1fr))` : `repeat(${columns}, 160px)` }}
    >
      {rows.flat().map((slot) => {
        const v = buildSlotView(slot, deviceDeck, legacyLabware);
        const selected = selectedSlot === slot;
        const mismatch = v.state === "mismatch";
        const footprint = moduleFootprints.get(slot);
        const footprintOnly = footprint != null && v.state === "empty";
        const footprintConflict =
          footprint != null && footprint.anchorSlot !== slot && v.state !== "empty" && v.moduleName == null;
        const paired = moduleSlots.get(slot);
        const inlineReadout =
          v.kind === "module" &&
          v.moduleName != null &&
          TEMP_FAMILIES.has(moduleFamily(v.moduleName) ?? "");
        const moduleAccent = footprint != null || v.moduleName != null;
        // Declared = operator intent the robot has not confirmed. Page-only,
        // matching where the "declared" wording already renders: on the compact
        // tile almost every slot is declared, so outlining them all would say
        // nothing while shouting.
        const declaredOnly = page && migrated && v.state === "declared";
        const cellTitle = footprintOnly
          ? `Slot ${slot} — occupied by the ${footprint.moduleName} anchored at slot ${footprint.anchorSlot}`
          : v.title;
        const cellClassName = [
          "relative overflow-hidden rounded border transition-colors",
          page ? "aspect-[4/3] w-full" : "h-[120px] w-[160px]",
          selected
            ? "border-sky-500 bg-sky-50 dark:border-sky-500 dark:bg-sky-950/40"
            : footprintConflict
              ? "border-rose-500 bg-rose-50 dark:border-rose-500 dark:bg-rose-950/30"
            : mismatch
              ? "border-amber-500 bg-amber-50 dark:border-amber-500 dark:bg-amber-950/30"
              : declaredOnly
                ? // Orange against a mismatch's amber. The two borders differ
                  // only in hue, which is thin on its own — the "declared"
                  // badge is what actually names the state, and the ≠ badge
                  // names the other. The border is the glanceable half.
                  "border-orange-400 bg-white dark:border-orange-500/80 dark:bg-slate-800/40"
                : interactive
                  ? "border-slate-200 bg-white hover:border-slate-400 dark:border-slate-700 dark:bg-slate-800/40 dark:hover:border-slate-500"
                  : "border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-800/40",
        ].join(" ");
        const cellBody = (
          <>
            {footprintOnly ? (
              <div className="flex h-full w-full flex-col items-center justify-center gap-1 px-1">
                <span className="text-[9px] uppercase tracking-wider text-ink-subtle dark:text-slate-400">
                  {moduleShortLabel(footprint.moduleName)}
                </span>
                <span className="text-[9px] text-ink-subtle dark:text-slate-400">
                  footprint of slot {footprint.anchorSlot}
                </span>
              </div>
            ) : v.isTrash ? (
              <div className="flex h-full w-full items-center justify-center bg-slate-300/70 dark:bg-slate-700/60">
                <span className="text-[9px] uppercase tracking-wider text-ink-subtle dark:text-slate-400">
                  waste
                </span>
              </div>
            ) : v.loadName || (v.rows > 0 && v.columns > 0) ? (
              <LabwareThumbnail view={v} slot={slot} tipRacks={tipRacks} />
            ) : v.state !== "empty" ? (
              <div className="flex h-full w-full flex-col items-center justify-center gap-1 px-1 text-center">
                <span className="text-[10px] font-medium text-ink-subtle dark:text-slate-400">
                  {v.label || v.kind}
                </span>
                {inlineReadout && <ModuleReadout live={paired?.live ?? null} compact />}
              </div>
            ) : (
              <div className="flex h-full w-full items-center justify-center">
                <span className="select-none text-4xl font-semibold text-slate-200 dark:text-slate-700">
                  {slot}
                </span>
              </div>
            )}
            {moduleAccent && (
              <span
                className="absolute inset-x-0 top-0 h-[3px] bg-amber-400/90 dark:bg-amber-500/80"
                aria-hidden
              />
            )}
            {/* Slot number in the cell's own top-left corner. Bare text, no
                pill: the badge background is what made the old corner number
                read as an overlay sitting on top of A1. An empty slot already
                draws its number large and centred, so it is skipped here. */}
            {page && v.state !== "empty" && (
              <span
                className={[
                  "pointer-events-none absolute left-1 top-0.5 text-[10px] font-semibold leading-none",
                  moduleAccent ? "top-[5px]" : "",
                  "text-ink-subtle dark:text-slate-400",
                ].join(" ")}
                aria-hidden
              >
                {slot}
              </span>
            )}
            {/* Both variants badge the cell's top-right corner. The page
                variant used to reserve a whole text row above the box for this,
                costing every row ~1.1em of height to carry a badge that only a
                slot or two ever shows. */}
            {migrated && (v.state === "in_use" || v.state === "mismatch") && (
              <span
                className={[
                  "absolute right-1 top-1 rounded px-1 text-[8px] font-semibold uppercase tracking-wide",
                  v.state === "mismatch" ? "bg-amber-500 text-white" : "bg-sky-500 text-white",
                ].join(" ")}
                aria-hidden
              >
                {v.state === "mismatch" ? "≠" : "busy"}
              </span>
            )}
            {footprintConflict && (
              <span
                className="absolute right-1 top-1 rounded bg-rose-600 px-1 text-[8px] font-semibold uppercase tracking-wide text-white"
                aria-hidden
              >
                conflict
              </span>
            )}
          </>
        );
        // On the full-width deck the slot number sits in the cell's top-left
        // corner and the labware label BELOW the box. The compact tile keeps
        // the bare box: there is no room for a text row at 160x120.
        const box = <div className={cellClassName}>{cellBody}</div>;
        const content = page ? (
          <div className="flex w-full flex-col gap-0.5">
            {box}
            {/* Reserve the row even when blank so every plate box lines up. */}
            <span
              className="min-h-[1.15em] truncate px-0.5 text-left text-[10px] font-medium leading-tight text-ink dark:text-slate-200"
              title={v.loadName || v.label || undefined}
            >
              {v.state !== "empty" && !v.isTrash
                ? v.label
                : footprintOnly
                  ? `${moduleShortLabel(footprint.moduleName)} footprint`
                  : "\u00a0"}
            </span>
          </div>
        ) : (
          box
        );
        return interactive ? (
          <button
            key={slot}
            type="button"
            onClick={() => onSelectSlot?.(selectedSlot === slot ? null : slot)}
            title={cellTitle}
            className="block w-full text-left"
          >
            {content}
          </button>
        ) : (
          <div key={slot} title={cellTitle} className="block w-full text-left">
            {content}
          </div>
        );
      })}
    </div>
  );

  // The orange outline is the only slot state carried by colour alone — every
  // other one also says its name (a "busy"/"≠" badge, the labware label). One
  // legend under the deck is cheaper than repeating the word on what is
  // usually most of the twelve slots. Tile variant renders the bare grid: it
  // never draws the outline, so it has nothing to explain.
  if (!page) return grid;
  return (
    <div className="flex w-full flex-col gap-1.5">
      <p className="px-0.5 text-center text-[10px] font-semibold uppercase tracking-wider text-ink-subtle dark:text-slate-400">
        Back of robot
      </p>
      <p className="flex items-center gap-1.5 px-0.5 text-[10px] leading-tight text-ink-subtle dark:text-slate-400">
        <span
          className="inline-block h-3 w-4 shrink-0 rounded-[2px] border border-orange-400 dark:border-orange-500/80"
          aria-hidden
        />
        Orange outline — <strong className="font-semibold">declared</strong>: operator intent, not
        yet observed on the robot.
      </p>
      {grid}
      <p className="px-0.5 text-center text-[10px] font-semibold uppercase tracking-wider text-ink-subtle dark:text-slate-400">
        Front · operator
      </p>
    </div>
  );
}
