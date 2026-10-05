import { useEffect, useMemo, useState } from "react";

import {
  ApiError,
  getLabStoreDefinition,
  getLabStoreList,
  getLabwareList,
  postDeckDeclare,
  type DeckDeclareValue,
  postHome,
  postPlateBalance,
  postPause,
  postStop,
  postResume,
  postReconcile,
  postDropTip,
  postSetLights,
  postSetTempmod,
  postDeactivateTempmod,
  postTipsMark,
  type TipSelection,
  postTipsReset,
  postShutdown,
  postStartup,
} from "../lib/api";
import { useActionErrorState } from "../lib/use-action-error";
import { declarationPayload, moduleDeclarationPayload, unassignedModules } from "../lib/module-placement";
import type { useClaim } from "../lib/use-claim";
import {
  buildSlotView,
  automationFromStatus,
  claimedByFromStatus,
  declaredMapFromDeck,
  deviceDeckFromStatus,
  mountedTipsFromStatus,
  nextDeclaration,
  pairModuleSlots,
  moduleFamily,
  robotInfoFromStatus,
  robotModulesFromStatus,
  tipRacksFromStatus,
  type TipRackSummary,
} from "../lib/ot2-deck";
import { catalogEntryFromLabware, OT2_CATALOG, type CatalogEntry } from "../lib/ot2-catalog";
import type { GatewaySnapshot, RobotModule, PlateBalanceStatus } from "../lib/types";

import { ActionErrorBadge } from "./ActionErrorBadge";
import { AssemblyPicker } from "./AssemblyPicker";
import { ManualPipettePanel } from "./ManualPipettePanel";
import { DeckPanel, ModuleReadout } from "./DeckPanel";
import { PlateInspector } from "./PlateInspector";
import { DeclarePicker } from "./DeclarePicker";
import { FetchErrorBand } from "./FetchErrorBand";
import { LastErrorBadge } from "./LastErrorBadge";
import { StalenessIndicator } from "./StalenessIndicator";
import { StatusPill } from "./StatusPill";
import { TileButton } from "./TileButton";
import { PlateBalanceControls } from "./PlateBalanceControls";
import { RunRecords } from "./RunRecords";
import { CameraControl } from "./CameraControl";
import { PANEL_STYLE, PANEL_HEADING_STYLE } from "./panel-styles";

// ---------------------------------------------------------------------------
// Small presentational helpers
// ---------------------------------------------------------------------------

/* Tape-player transport glyphs. Inline SVG rather than a unicode character
   (⏸/▶): the unicode ones render at wildly different weights and baselines
   across platforms, and several fall back to an emoji font that ignores
   `currentColor` — so a disabled or danger-variant button would keep a full
   colour glyph. `currentColor` + `aria-hidden` keeps them tinted by the
   button variant and silent to screen readers, which read the ariaLabel. */
function PlayGlyph() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="currentColor" aria-hidden>
      <path d="M1.5 0.8 A0.5 0.5 0 0 1 2.3 0.4 L9 4.6 A0.5 0.5 0 0 1 9 5.4 L2.3 9.6 A0.5 0.5 0 0 1 1.5 9.2 Z" />
    </svg>
  );
}

/**
 * One card in the panel.
 *
 * `collapsible` opts a section into a click-to-fold header. Only the long ones
 * take it: this column is a single scroll, and a rack grid or a slot's plate
 * view pushes everything below it off-screen even when the operator is done
 * with it. Fold state is component-local and defaults to open — a section that
 * hid itself on load would be a section nobody finds, and the poll cycle must
 * never reopen what someone just closed (which local state gives us, since the
 * card is not remounted by a status refresh).
 */
function Section({
  title,
  children,
  collapsible = false,
  defaultOpen = true,
}: {
  title: string;
  children: React.ReactNode;
  collapsible?: boolean;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const heading = PANEL_HEADING_STYLE;

  return (
    <section className={PANEL_STYLE}>
      {collapsible ? (
        <h3 className={open ? `mb-2 ${heading}` : heading}>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="flex w-full items-center gap-1.5 text-left hover:text-ink dark:hover:text-slate-200"
          >
            <svg
              viewBox="0 0 8 8"
              className={[
                "h-2 w-2 shrink-0 fill-current transition-transform",
                open ? "rotate-90" : "",
              ].join(" ")}
              aria-hidden
            >
              <path d="M2 0 L7 4 L2 8 Z" />
            </svg>
            {title}
          </button>
        </h3>
      ) : (
        <h3 className={`mb-2 ${heading}`}>{title}</h3>
      )}
      {(!collapsible || open) && children}
    </section>
  );
}

function KV({ k, v, mono }: { k: string; v: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-xs">
      <span className="text-ink-subtle dark:text-slate-500">{k}</span>
      <span
        className={[
          "min-w-0 truncate text-right text-ink dark:text-slate-200",
          mono ? "font-mono" : "",
        ].join(" ")}
      >
        {v}
      </span>
    </div>
  );
}

function TempModuleControls({
  slot,
  live,
  disabled,
  hint,
  onSet,
  onOff,
}: {
  slot: number | string;
  live: RobotModule | null;
  disabled: boolean;
  hint?: string;
  onSet: (celsius: number) => void;
  onOff: () => void;
}) {
  const [draft, setDraft] = useState(() =>
    String(live?.target_temperature ?? live?.current_temperature ?? 4),
  );
  const celsius = Number(draft);
  const valid = Number.isFinite(celsius) && celsius >= 4 && celsius <= 95;
  return (
    <div className="flex items-center gap-1">
      <input
        type="number"
        min={4}
        max={95}
        step={0.5}
        value={draft}
        disabled={disabled}
        onChange={(e) => setDraft(e.target.value)}
        aria-label={`Target °C for temperature module on slot ${slot}`}
        title={hint}
        className="w-14 rounded border border-slate-300 bg-white px-1 py-0.5 text-right text-xs tabular-nums text-ink disabled:opacity-50 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
      />
      <span className="text-[10px] text-ink-subtle dark:text-slate-500">°C</span>
      <button
        type="button"
        disabled={disabled || !valid}
        onClick={() => onSet(celsius)}
        title={hint ?? `Set target to ${draft} °C`}
        className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-ink disabled:opacity-50 dark:border-slate-700 dark:text-slate-200"
      >
        Set
      </button>
      <button
        type="button"
        disabled={disabled}
        onClick={onOff}
        title={hint ?? "Turn the temperature module off"}
        className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-ink disabled:opacity-50 dark:border-slate-700 dark:text-slate-200"
      >
        Off
      </button>
    </div>
  );
}

/** Per-column aggregate of a rack's tip statuses, for one column's swatch. */
type ColumnState = "fresh" | "empty" | "touched" | "mixed";

const TIP_ROWS = ["A", "B", "C", "D", "E", "F", "G", "H"];

const COLUMN_SWATCH: Record<ColumnState, string> = {
  fresh: "border-sky-500 bg-sky-100 text-sky-800 dark:bg-sky-900/60 dark:text-sky-200",
  // An empty column is a hole: hollow, dashed, like the plan view's empty wells.
  empty:
    "border-dashed border-slate-400 text-ink-subtle dark:border-slate-600 dark:text-slate-500",
  touched:
    "border-amber-500 bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200",
  mixed: "border-slate-400 bg-slate-100 text-ink dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200",
};

/** One well's state, in the same vocabulary the column swatch uses.
 *
 * `on_pipette` is deliberately folded in with `empty`: both mean the hole has
 * no tip in it, which is the only question this editor lets an operator answer.
 * The distinction (gone for good vs. riding a head) is the gateway's to make
 * and the inspector's to draw, not something a human asserts by clicking. */
function wellState(tips: Record<string, string>, well: string): ColumnState {
  const status = tips[well];
  if (status === undefined) return "fresh";
  if (status === "empty" || status === "on_pipette") return "empty";
  return "touched";
}

function columnState(tips: Record<string, string>, column: number): ColumnState {
  const statuses = TIP_ROWS.map((row) => tips[`${row}${column}`]);
  // A well absent from `tips` is fresh — the summary carries non-fresh only.
  if (statuses.every((s) => s === undefined)) return "fresh";
  if (statuses.every((s) => s === "empty")) return "empty";
  if (statuses.every((s) => s !== undefined && s !== "empty")) return "touched";
  return "mixed";
}

/**
 * Correct part of a rack, one column at a time.
 *
 * "Mark refilled" can only assert a *whole* fresh rack, so an operator whose
 * rack is genuinely half-used had to overstate it — and an overstated rack
 * sends the head onto bare holes. Columns are the unit because that is how an
 * 8-channel head consumes a rack.
 *
 * Only presence is offered. A *touched* tip carries the sample id it contacted,
 * which is evidence the gateway recorded during a real aspirate; an operator
 * cannot assert it, so amber is a colour this editor reads but never writes.
 */
function TipEditor({
  rack,
  disabled,
  hint,
  onMark,
}: {
  rack: TipRackSummary;
  disabled: boolean;
  hint?: string;
  onMark: (selection: TipSelection, status: "new" | "empty") => void;
}) {
  const [selected, setSelected] = useState<number[]>([]);
  const [wells, setWells] = useState<string[]>([]);
  const [byWell, setByWell] = useState(false);
  const columns = rack.total / TIP_ROWS.length;
  // The column model is an 8-row rack. Anything else (a partial rack from a
  // `wells`-scoped reset, a non-standard grid) gets no editor rather than a
  // grid that mislabels which wells a click would touch.
  if (!Number.isInteger(columns) || columns < 1 || columns > 12) return null;

  function toggle(column: number) {
    setSelected((prev) =>
      prev.includes(column) ? prev.filter((c) => c !== column) : [...prev, column],
    );
  }

  function toggleWell(well: string) {
    setWells((prev) =>
      prev.includes(well) ? prev.filter((w) => w !== well) : [...prev, well],
    );
  }

  function apply(status: "new" | "empty") {
    if (byWell) {
      // Column-major, matching how the rack is consumed and how the gateway
      // orders its own well list — so the audit row reads in rack order.
      const ordered = [...wells].sort(
        (a, b) =>
          Number(a.slice(1)) - Number(b.slice(1)) ||
          a.charCodeAt(0) - b.charCodeAt(0),
      );
      onMark({ wells: ordered }, status);
      setWells([]);
      return;
    }
    onMark({ columns: [...selected].sort((a, b) => a - b) }, status);
    setSelected([]);
  }

  const chosen = byWell ? wells.length : selected.length;

  return (
    <div className="mt-1.5">
      {/* Columns stay the default: they are how an 8-channel head consumes a
          rack, and the common correction. Wells are the repair unit for a
          tracker that has drifted by one or two tips — the case that used to
          need a raw API call, because this editor could only speak columns. */}
      <div className="mb-1 flex items-center gap-2">
        <span className="text-[10px] text-ink-subtle dark:text-slate-500">Correct by</span>
        {([
          [false, "column"],
          [true, "well"],
        ] as const).map(([mode, label]) => (
          <button
            key={label}
            type="button"
            disabled={disabled}
            aria-pressed={byWell === mode}
            onClick={() => {
              setByWell(mode);
              setSelected([]);
              setWells([]);
            }}
            className={[
              "rounded px-1.5 py-0.5 text-[10px] font-semibold disabled:cursor-not-allowed disabled:opacity-50",
              byWell === mode
                ? "bg-sky-100 text-sky-800 dark:bg-sky-900/60 dark:text-sky-200"
                : "text-ink-subtle hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800",
            ].join(" ")}
          >
            {label}
          </button>
        ))}
      </div>

      {byWell ? (
        <div
          className="grid w-fit gap-0.5"
          style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}
          role="group"
          aria-label={`Tip wells in slot ${rack.slot}`}
        >
          {TIP_ROWS.flatMap((row) =>
            Array.from({ length: columns }, (_, i) => i + 1).map((column) => {
              const well = `${row}${column}`;
              const state = wellState(rack.tips, well);
              const on = wells.includes(well);
              return (
                <button
                  key={well}
                  type="button"
                  disabled={disabled}
                  aria-pressed={on}
                  aria-label={`${well} — ${state}`}
                  onClick={() => toggleWell(well)}
                  title={hint ?? `${well} — ${state}`}
                  className={[
                    "h-4 w-4 rounded-full border text-[0px] disabled:cursor-not-allowed disabled:opacity-50",
                    COLUMN_SWATCH[state],
                    on ? "ring-2 ring-sky-500 ring-offset-1 dark:ring-offset-slate-900" : "",
                  ].join(" ")}
                >
                  {well}
                </button>
              );
            }),
          )}
        </div>
      ) : (
      <div className="flex flex-wrap gap-1" role="group" aria-label={`Tip columns in slot ${rack.slot}`}>
        {Array.from({ length: columns }, (_, i) => i + 1).map((column) => {
          const state = columnState(rack.tips, column);
          const on = selected.includes(column);
          return (
            <button
              key={column}
              type="button"
              disabled={disabled}
              aria-pressed={on}
              onClick={() => toggle(column)}
              title={hint ?? `Column ${column} — ${state}`}
              className={[
                "h-5 w-5 rounded border text-[9px] font-semibold tabular-nums disabled:cursor-not-allowed disabled:opacity-50",
                COLUMN_SWATCH[state],
                on ? "ring-2 ring-sky-500 ring-offset-1 dark:ring-offset-slate-900" : "",
              ].join(" ")}
            >
              {column}
            </button>
          );
        })}
      </div>
      )}
      {chosen > 0 && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <span className="text-[10px] text-ink-subtle dark:text-slate-400">
            {byWell
              ? `${wells.length === 1 ? "Well" : "Wells"} ${[...wells]
                  .sort(
                    (a, b) =>
                      Number(a.slice(1)) - Number(b.slice(1)) ||
                      a.charCodeAt(0) - b.charCodeAt(0),
                  )
                  .join(", ")} —`
              : `${selected.length === 1 ? "Column" : "Columns"} ${[...selected]
                  .sort((a, b) => a - b)
                  .join(", ")} —`}
          </span>
          <button
            type="button"
            disabled={disabled}
            onClick={() => apply("new")}
            className="rounded border border-sky-500 px-1.5 py-0.5 text-[10px] font-semibold text-sky-700 hover:bg-sky-50 disabled:cursor-not-allowed disabled:opacity-50 dark:text-sky-300 dark:hover:bg-sky-950/40"
          >
            tips present
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() => apply("empty")}
            className="rounded border border-slate-400 px-1.5 py-0.5 text-[10px] font-semibold text-ink-subtle hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            no tips
          </button>
          <button
            type="button"
            onClick={() => {
              setSelected([]);
              setWells([]);
            }}
            className="text-[10px] text-ink-subtle underline dark:text-slate-400"
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The full-page OT-2 interface (ported from the dashboard's Ot2ControlPanel;
// the CONTROL_PASSWORD lock is replaced by the cooperative-claim gate).
// ---------------------------------------------------------------------------

export function ControlPanel({
  snapshot,
  refetch,
  claim,
}: {
  snapshot: GatewaySnapshot;
  refetch: () => void;
  claim: ReturnType<typeof useClaim>;
}) {
  const { status } = snapshot;
  const { actionError, setActionError, reportError } = useActionErrorState();
  const [selectedSlot, setSelectedSlot] = useState<number | string | null>(null);
  const [declaring, setDeclaring] = useState(false);
  const [pending, setPending] = useState(false);
  const [stopping, setStopping] = useState(false);
  // Which rack is awaiting a refill confirmation (nickname), if any.
  const [refillConfirm, setRefillConfirm] = useState<string | null>(null);
  useEffect(() => { setRefillConfirm(null); }, [selectedSlot]);
  // Whether "clear all declared intent" is awaiting its confirmation.
  const [clearAllConfirm, setClearAllConfirm] = useState(false);
  // The mount whose drop the gateway refused because the robot's run has no
  // record of a tip — offered a force drop once the operator has looked.
  const [forceDropOffer, setForceDropOffer] = useState<"left" | "right" | null>(null);

  // Controls unlock when this browser session holds the device claim.
  const locked = !claim.held;
  const token = claim.token;

  // Runtime picker entries from two sources: the gateway's own /labware
  // (standard Opentrons summaries — immutable for the gateway process
  // lifetime) and the dashboard's labware store at the edge root
  // (lab-custom definitions from the labware builder — mutable, empty when
  // the SPA isn't served through the edge). Fetched on mount and refetched
  // when the tab regains focus, so a plate just saved in the dashboard
  // builder appears without a reload. Each source fails soft independently.
  const [labwareEntries, setLabwareEntries] = useState<CatalogEntry[]>([]);
  useEffect(() => {
    let cancelled = false;
    const authored = new Set(OT2_CATALOG.map((e) => e.declare));
    const load = () => {
      Promise.all([
        getLabwareList().catch(() => ({ definitions: [] })),
        getLabStoreList().catch(() => ({ definitions: [] })),
      ]).then(([standard, labStore]) => {
        if (cancelled) return;
        const seen = new Set(authored);
        const entries: CatalogEntry[] = [];
        // Lab-custom first so a store definition shadowing a standard
        // load_name keeps its "lab custom" identity in the picker.
        for (const d of [...labStore.definitions, ...standard.definitions]) {
          if (seen.has(d.load_name)) continue;
          seen.add(d.load_name);
          entries.push(catalogEntryFromLabware(d));
        }
        setLabwareEntries(entries);
      });
    };
    load();
    window.addEventListener("focus", load);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", load);
    };
  }, []);

  const deviceDeck = deviceDeckFromStatus(status);
  const robotModules = robotModulesFromStatus(status);
  const moduleSlots = pairModuleSlots(deviceDeck, robotModules);
  const unattachedToSlots = unassignedModules(deviceDeck, robotModules);
  const declaredMap = useMemo(
    () => (deviceDeck ? declaredMapFromDeck(deviceDeck) : {}),
    [deviceDeck],
  );
  const declaredCount = Object.keys(declaredMap).filter(slot => !deviceDeck?.slots[slot]?.declared_module && !deviceDeck?.slots[slot]?.module).length;
  const tipRacks = tipRacksFromStatus(status);
  const mountedTips = mountedTipsFromStatus(status);
  const claimedBy = claimedByFromStatus(status);
  const robot = robotInfoFromStatus(status);
  const claimedByMe = claimedBy != null && claimedBy.session_id === claim.sessionId;
  // An approved plan holding the device. Nobody holds its claim token, so
  // the gateway opens stop / pause / resume to anyone signed in meanwhile.
  const automation = automationFromStatus(status);
  const canInterrupt = !locked || automation != null;

  const components = status.components ?? {};
  const pipLeft = components["pipette_left"];
  const pipRight = components["pipette_right"];
  const protocol = components["protocol"];

  // Drive the transport buttons off the device's own `allowed_actions` rather
  // than off local guesses. The gateway offers `pause` only in ready/busy and
  // `resume` only in paused, so mirroring the list means a button is live
  // exactly when pressing it would work — the §6.2 "allowed_actions and the
  // endpoint must never disagree" rule, applied to the UI.
  const allowedActions = status.allowed_actions ?? [];
  const isPaused = protocol?.state === "paused";
  const pausePending = status.details?.pause_requested === true;

  const lightsRaw = components["lights"]?.state;
  const lightsOn = lightsRaw === "on";
  const lightsKnown = lightsRaw === "on" || lightsRaw === "off";
  // Gateway session state for the CONNECTED toggle. Follow what the gateway
  // advertises (§6.2), not equipment_status: after a software stop the status
  // is `unknown` while the session still exists and only `shutdown` is
  // allowed — deriving "disconnected" from the status offered a startup the
  // gateway refused and hid the one action that gets out of that state.
  const stopLatched = status.details?.stop_latched === true;
  const stopConfirmed = status.details?.stop_confirmed === true;
  const canShutdown = allowedActions.includes("shutdown");
  const canStartup = allowedActions.includes("startup");
  const deviceOn = canShutdown || (!canStartup && status.details?.service_state !== "requires_init");

  const selectedView = selectedSlot != null ? buildSlotView(selectedSlot, deviceDeck, {}) : null;
  const selectedDeclare = selectedSlot != null ? (declaredMap[String(selectedSlot)] ?? null) : null;
  const selectedModule = selectedSlot != null && !!(deviceDeck?.slots[String(selectedSlot)]?.module || deviceDeck?.slots[String(selectedSlot)]?.declared_module);

  const selectedBalance = selectedSlot != null && deviceDeck?.slots[String(selectedSlot)]?.module?.module_name === "platebalanceV1";

  const mismatchSlots = deviceDeck
    ? Object.entries(deviceDeck.slots)
        .filter(([, s]) => s.slot_state === "mismatch")
        .map(([slot]) => slot)
        .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    : [];

  /** Attach each labstore-backed slot's full definition before POSTing a
   *  declare body. Fails soft per slot: a lookup failure (store unreachable,
   *  definition deleted since the picker fetched its summary) falls back to
   *  the bare load_name — the pre-existing, "unknown"-grid behavior — rather
   *  than blocking the whole declare over one bad entry. Fetches are deduped
   *  and run in parallel; there are at most 12 slots. */
  function withLabwareDefinitions(
    next: Record<string, string>,
    entries: CatalogEntry[],
  ): Promise<Record<string, DeckDeclareValue>> {
    const labstoreNames = new Set(
      entries.filter((e) => e.category === "labstore").map((e) => e.declare),
    );
    const uniqueLoadNames = [...new Set(Object.values(next))].filter((v) =>
      labstoreNames.has(v),
    );
    return Promise.all(
      uniqueLoadNames.map((loadName) =>
        getLabStoreDefinition(loadName).then(
          (definition) => [loadName, definition] as const,
          () => [loadName, null] as const,
        ),
      ),
    ).then((pairs) => {
      const definitions = new Map(pairs.filter(([, d]) => d != null));
      const resolved: Record<string, DeckDeclareValue> = {};
      for (const [slot, value] of Object.entries(next)) {
        const definition = definitions.get(value);
        resolved[slot] = definition ? { load_name: value, definition } : value;
      }
      // Preserve serial identity and embedded geometry on unchanged slots.
      if (deviceDeck) {
        for (const [slot, value] of Object.entries(declarationPayload(deviceDeck))) {
          if (next[slot] === declaredMap[slot] && value && typeof value === "object" &&
            ("module_name" in value || "assembly" in value || ("definition" in value && value.definition))) resolved[slot] = value;
        }
      }
      return resolved;
    });
  }

  function declare(entry: CatalogEntry | null) {
    if (locked || selectedSlot == null || declaring || (selectedModule && !selectedBalance) || (entry?.category === "module" || OT2_CATALOG.some(e => e.category === "module" && e.declare === entry?.declare))) return;
    // Declaring over a slot that already holds a declaration is refused —
    // clearing it is the deliberate first half of a replacement. The gateway
    // auto-loads labware from the declaration, so a slot changed by a stray
    // click reaches the robot. Clearing (a null entry) is always allowed.
    if (entry != null && declaredMap[String(selectedSlot)] != null) return;
    setActionError(null);
    setDeclaring(true);
    // Full-layout replace: re-send every currently-declared slot (exact
    // load_names preserved by declaredMapFromDeck) with this slot updated.
    const next = nextDeclaration(declaredMap, selectedSlot, entry?.declare ?? null);
    // A bare load_name is only complete for a standard Opentrons definition
    // (the gateway's classify_labware guesses geometry from the name). Any
    // slot whose value matches a "labstore" entry (a lab-custom definition
    // from the dashboard's labware store) needs its full definition attached
    // instead, or it silently resolves to kind "unknown" with no grid on the
    // gateway — the same bug this fetch closes for every declared slot, not
    // just the one being changed right now (declare is a full-layout
    // replace, so an unrelated edit would otherwise re-send every other
    // custom slot as a bare, now-degraded name).
    withLabwareDefinitions(next, labwareEntries)
      .then(async (resolved) => {
        if (selectedBalance && entry) {
          const definition = entry.category === "labstore" ? await getLabStoreDefinition(entry.declare) : undefined;
          resolved[String(selectedSlot)] = { load_name: entry.declare, definition, support_module: "platebalanceV1" };
        }
        return postDeckDeclare(token, resolved);
      })
      .then(() => refetch())
      .catch((e: unknown) => reportError(e, "deck.declare"))
      .finally(() => setDeclaring(false));
  }

  function runControl(name: string, fn: () => Promise<unknown>) {
    if (locked || pending) return;
    setActionError(null);
    setPending(true);
    fn()
      .then(() => refetch())
      .catch((e: unknown) => reportError(e, name))
      .finally(() => setPending(false));
  }

  function dropTip(mount: "left" | "right") {
    if (locked || pending) return;
    setActionError(null);
    setForceDropOffer(null);
    setPending(true);
    postDropTip(token, mount)
      .then(() => refetch())
      .catch((e: unknown) => {
        // 412 with robot_reports_tip=false: the run believes the head bare
        // (a run started after a stop does). Only the operator can say a tip
        // is there, so the force drop is offered, never taken.
        if (e instanceof ApiError && e.status === 412
          && (e.body as { robot_reports_tip?: unknown } | null)?.robot_reports_tip === false) {
          setForceDropOffer(mount);
        }
        reportError(e, "drop_tip");
      })
      .finally(() => setPending(false));
  }

  function forceDrop(mount: "left" | "right") {
    setForceDropOffer(null);
    runControl("drop_tip", () => postDropTip(token, mount, true));
  }

  /** stop / pause / resume: also open, without the claim, while a plan runs
   *  unattended. */
  function runInterrupt(name: string, fn: () => Promise<unknown>) {
    if (!canInterrupt || pending) return;
    setActionError(null);
    setPending(true);
    fn()
      .then(() => refetch())
      .catch((e: unknown) => reportError(e, name))
      .finally(() => setPending(false));
  }

  function stopRun() {
    if (!canInterrupt || stopping) return;
    setStopping(true);
    postStop(token)
      .then(() => refetch())
      .catch((e: unknown) => reportError(e, "stop"))
      .finally(() => setStopping(false));
  }

  function refillRack(slot: string) {
    setRefillConfirm(null);
    runControl("tips.reset", () => postTipsReset(token, slot));
  }

  function markTips(slot: string, selection: TipSelection, status: "new" | "empty") {
    runControl("tips.mark", () => postTipsMark(token, slot, selection, status));
  }

  /** The labware name for a tracked slot, read off the deck — the tracker
   *  stores only the slot, since a rack has no identity beyond where it is. */
  function rackLabel(slot: string): string | undefined {
    const lw = deviceDeck?.slots?.[slot]?.labware;
    return lw?.load_name || lw?.display_name || undefined;
  }

  function clearAll() {
    if (locked || declaring || !deviceDeck) return;
    setClearAllConfirm(false);
    setActionError(null);
    setDeclaring(true);
    postDeckDeclare(token, moduleDeclarationPayload(deviceDeck))
      .then(() => refetch())
      .catch((e: unknown) => reportError(e, "deck.declare"))
      .finally(() => setDeclaring(false));
  }

  const controlHint = locked ? "Take control first (claim the device)" : undefined;
  const interruptHint = canInterrupt ? undefined : controlHint;

  return (
    <div className="flex flex-col gap-4">
      {/* Header strip */}
      <header className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-lg font-semibold text-ink dark:text-slate-100">
            {snapshot.name}
          </h2>
          <p className="truncate text-xs text-ink-subtle dark:text-slate-500">
            <span className="uppercase">{snapshot.kind}</span> ·{" "}
            <span className="font-mono">{snapshot.id}</span>
            {robot?.robot_name && (
              <>
                {" "}
                · robot <span className="font-mono">{robot.robot_name}</span>
              </>
            )}
            {robot?.api_version && (
              <>
                {" "}
                · API <span className="font-mono">{robot.api_version}</span>
              </>
            )}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <ActionErrorBadge error={actionError} />
          <LastErrorBadge error={status.last_error} />
          {/* Offered whenever the gateway itself asks for reconciliation, not
              only in `error`: an unknown outcome (a command whose fate the
              gateway could not observe) also reports `manual_reconcile`, and
              hiding the button there left the operator with no way out of
              the panel (seen live 2026-10-02 after a balance tare). */}
          {(status.equipment_status === "error" ||
            (status.required_actions ?? []).includes("manual_reconcile")) && (
            <TileButton
              onClick={() => runControl("reconcile", () => postReconcile(token))}
              disabled={locked || pending}
              variant="danger"
              title={
                controlHint ??
                (status.equipment_status === "error"
                  ? "Acknowledge the failed command and return the gateway to ready — check the robot first; this clears the error, it does not fix anything"
                  : "The last command's outcome is unknown. Inspect the robot (and balance) yourself, then click to acknowledge and return the gateway to ready")
              }
            >
              {status.equipment_status === "error" ? "CLEAR ERROR" : "RECONCILE"}
            </TileButton>
          )}
          <TileButton
            onClick={() => (claim.held ? void claim.release() : void claim.acquire())}
            disabled={claim.pending}
            variant={claim.held ? "primary" : "default"}
            title={
              claim.held
                ? "You hold the device claim — click to release control"
                : "Acquire the cooperative claim (STATUS_SPEC v1.1) to unlock the controls"
            }
          >
            {claim.held ? "RELEASE CONTROL" : "TAKE CONTROL"}
          </TileButton>
          {/* The session toggle changes the gateway connection. Keep it beside
              the claim that unlocks it, away from the motion/run controls, so
              an operator does not mistake it for part of a protocol action. */}
          <TileButton
            onClick={() =>
              deviceOn
                ? runControl("shutdown", () => postShutdown(token))
                : runControl("startup", () => postStartup(token))
            }
            disabled={locked || pending || !(canShutdown || canStartup)}
            variant={deviceOn ? "primary" : "default"}
            title={
              controlHint ??
              (deviceOn
                ? canShutdown
                  ? stopLatched
                    ? "Stopped — inspect the robot, then click to close this session; start a fresh one afterwards"
                    : "Gateway session connected — click to disconnect (does NOT power off the robot)"
                  : "Session busy — shutdown is not available right now"
                : canStartup
                  ? stopLatched
                    ? "Stopped and closed — click to start a fresh session (clears the stop)"
                    : "Click to connect & initialize the gateway session"
                  : "Startup is not available right now")
            }
          >
            <span
              className={[
                "mr-1 inline-block h-2 w-2 rounded-full",
                deviceOn ? "bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.7)]" : "bg-slate-400",
              ].join(" ")}
              aria-hidden
            />
            {stopLatched
              ? deviceOn ? "CLOSE SESSION" : "START SESSION"
              : deviceOn ? "CONNECTED" : "DISCONNECTED"}
          </TileButton>
          <StatusPill state={status.equipment_status}
            stopped={stopLatched ? (stopConfirmed ? "confirmed" : "unconfirmed") : undefined} />
        </div>
      </header>

      {claim.error && (
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-amber-300 bg-amber-50 px-4 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
          <span>{claim.error}</span>
          {/* Offered only when the holder is this same owner (another tab of
              yours, or one you reloaded). Never for an agent's claim. */}
          {claim.canTakeover && (
            <TileButton
              onClick={() => void claim.acquire(true)}
              disabled={claim.pending}
              title="Supersede your other session's claim; its page will re-lock its controls"
            >
              TAKE OVER
            </TileButton>
          )}
        </p>
      )}


      {automation ? (
        <div
          role="status"
          className="flex flex-col gap-1.5 rounded-md border-2 border-violet-500 bg-violet-50 px-4 py-2.5 text-sm text-violet-950 dark:border-violet-500 dark:bg-violet-950/40 dark:text-violet-100"
        >
          <p>
            <span className="font-semibold">Running unattended — held by automation.</span>{" "}
            Plan <span className="font-mono">{automation.plan_id.slice(0, 8)}</span>, approved by{" "}
            <span className="font-semibold">{automation.approved_by}</span>
            {automation.started_at && (
              <>, started {new Date(automation.started_at).toLocaleTimeString()}</>
            )}
            . Step {automation.step} of {automation.total_steps}
            {automation.action && <> (<span className="font-mono">{automation.action}</span>)</>}.
          </p>
          {automation.total_steps > 0 && (
            <div className="h-1.5 w-full overflow-hidden rounded bg-violet-200 dark:bg-violet-900" aria-hidden>
              <div
                className="h-full bg-violet-600 dark:bg-violet-400"
                style={{ width: `${Math.min(100, (100 * automation.step) / automation.total_steps)}%` }}
              />
            </div>
          )}
          <p className="text-xs">
            It runs until it finishes, fails or is stopped — closing a browser does not stop it.
            Anyone signed in can PAUSE or STOP it below; no one can take control until it ends.
          </p>
        </div>
      ) : claimedBy && !claimedByMe && (
        <p className="rounded-md border border-sky-200 bg-sky-50 px-4 py-2 text-xs text-sky-900 dark:border-sky-900/50 dark:bg-sky-950/30 dark:text-sky-200">
          Controlled by <span className="font-semibold">{claimedBy.owner}</span>
          {claimedBy.expires_at && (
            <>
              {" "}
              — claim expires <span className="font-mono">{claimedBy.expires_at}</span>
            </>
          )}
          . Control writes will be refused (423) while the claim is held.
        </p>
      )}

      {mismatchSlots.length > 0 && (
        <p className="rounded-md border border-amber-300 bg-amber-50 px-4 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
          Declared intent disagrees with the observed deck at slot
          {mismatchSlots.length > 1 ? "s" : ""} {mismatchSlots.join(", ")} — click the flagged slot
          for details.
        </p>
      )}

      {snapshot.fetch_error && <FetchErrorBand error={snapshot.fetch_error} />}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,7fr)_minmax(0,4fr)]">
        {/* Left column: deck + declare.

            Capped below `lg`, on the COLUMN rather than on the deck inside it.
            The deck's cells are aspect-[4/3] at w-full in a 3-column grid, so
            its height tracks its width — roughly square. Once the two-column
            layout collapses, an uncapped deck takes the full page width and
            becomes a screen-tall wall of slots. Capping the deck alone fixed
            that but left the tile itself full-bleed, so a small deck floated in
            a very wide card; the border has to move with the content.
            max-w-xl is about what the 3fr column gives it at `lg`, so the
            column looks the same at every breakpoint rather than inflating at
            the narrow one. */}
        <div className="flex w-full max-w-xl flex-col gap-4 lg:max-w-none">
          <Section title="Deck — declared intent vs observed hardware">
            <DeckPanel
              deviceDeck={deviceDeck}
              robotModules={robotModules}
              selectedSlot={selectedSlot}
              onSelectSlot={setSelectedSlot}
              variant="page"
              tipRacks={tipRacks}
            />
            {!deviceDeck && (
              <p className="mt-2 text-xs text-ink-subtle dark:text-slate-500">
                This gateway doesn&apos;t publish a normalized deck on /status yet — deck view
                unavailable.
              </p>
            )}
          </Section>

          <Section title="Declare deck intent">
            <DeclarePicker
              selectedSlot={selectedSlot}
              currentDeclare={selectedDeclare}
              locked={locked || (selectedModule && !selectedBalance)}
              balanceOnly={selectedBalance}
              onDeclare={declare}
              customEntries={labwareEntries}
            />
            <AssemblyPicker
              key={String(selectedSlot)}
              entries={labwareEntries}
              disabled={locked || declaring || selectedSlot == null || selectedDeclare != null || selectedModule}
              current={selectedSlot == null ? null : deviceDeck?.slots[String(selectedSlot)]?.declared?.assembly}
              onDeclare={async assembly => {
                if (locked || declaring || selectedSlot == null || selectedDeclare != null || selectedModule || !deviceDeck) return;
                setDeclaring(true);
                try {
                  await postDeckDeclare(token, { ...declarationPayload(deviceDeck), [String(selectedSlot)]: { assembly } });
                  await refetch();
                } finally { setDeclaring(false); }
              }}
            />
            {/* Clearing every slot at once is the one declare action with no
                per-slot undo, so it confirms first — same shape as the tip
                refill confirm. */}
            <div className="mt-2 border-t border-slate-100 pt-2 dark:border-slate-800">
              {clearAllConfirm && declaredCount > 0 ? (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[11px] text-ink-subtle dark:text-slate-400">
                    Clear all {declaredCount} labware declarations?
                  </span>
                  <button
                    type="button"
                    disabled={locked || declaring}
                    onClick={clearAll}
                    className="rounded-md border border-rose-500 px-2 py-1 text-xs font-semibold text-rose-700 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-rose-500 dark:text-rose-300 dark:hover:bg-rose-950/40"
                  >
                    Yes, clear all
                  </button>
                  <button
                    type="button"
                    onClick={() => setClearAllConfirm(false)}
                    className="text-xs text-ink-subtle underline dark:text-slate-400"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  disabled={locked || declaring || declaredCount === 0}
                  onClick={() => setClearAllConfirm(true)}
                  className="rounded-md border border-rose-300 px-2 py-1 text-xs text-rose-700 hover:border-rose-400 disabled:cursor-not-allowed disabled:opacity-50 dark:border-rose-900 dark:text-rose-300"
                  title="Clears labware declarations; module placements are retained"
                >
                  Clear labware declarations
                </button>
              )}
            </div>
          </Section>
        </div>

        {/* Right column: selected slot / robot / pipettes / modules / tips / claim.
            Capped to match the left column — stacked, the two columns render as
            one sequence, so capping only one would step the page width
            mid-scroll. */}
        <div className="flex w-full max-w-xl flex-col gap-4 lg:max-w-none">
          {/* Session controls. PAUSE applies between commands. STOP requests
              a software stop of the owned HTTP run. The gateway-session toggle
              sits next to the claim control above, away from this action strip.

              Sits at the top of the right column rather than spanning the page:
              as a full-width banner it was the widest thing on screen while
              holding small buttons, pushing the deck below the fold. Tighter
              padding and gaps than a Section since it is a control strip, not
              a panel. */}
          <div className="flex flex-wrap items-center gap-1.5 rounded-xl border border-slate-200 bg-surface-raised p-2 shadow-sm dark:border-slate-800 dark:bg-slate-900">
            <TileButton
              onClick={() => runControl("home", () => postHome(token))}
              disabled={locked || pending}
              title={controlHint ?? "Home the gantry (requires a connected session)"}
            >
              HOME
            </TileButton>
            {/* Pause is a word, play a glyph: "||" read as two bars, not as
                pause, to operators on the bench. Play carries an ariaLabel —
                a title alone is not exposed to a screen reader as an
                accessible name.

                Neither is tinted at rest. Pause was `danger` red, which read as
                a warning on an idle robot and made the strip look alarmed when
                nothing was wrong; red here should mean "something happened",
                not "this button exists". Instead the pair behaves like a tape
                player: exactly one is live at a time, and once paused the play
                button goes primary so the way out is the only thing lit. */}
            <TileButton
              onClick={stopRun}
              disabled={!canInterrupt || stopping || !allowedActions.includes("stop")}
              variant="stop"
              ariaLabel="Stop this robot run"
              title="Software stop over HTTP. Requires inspection and a fresh session; cannot replace a physical emergency stop."
            >
              {stopping ? "STOPPING…" : "STOP"}
            </TileButton>
            <TileButton
              onClick={() => runInterrupt("pause", () => postPause(token))}
              disabled={!canInterrupt || pending || pausePending || !allowedActions.includes("pause")}
              ariaLabel="Pause the running protocol"
              title={
                interruptHint ??
                (pausePending
                  ? "Pausing: the running command finishes first"
                  : allowedActions.includes("pause")
                    ? "Pause after the current command finishes; a running plan waits at its next step until you press play. Use STOP to interrupt motion."
                    : isPaused
                      ? "Already paused"
                      : "Nothing to pause")
              }
            >
              PAUSE
            </TileButton>
            <TileButton
              onClick={() => runInterrupt("resume", () => postResume(token))}
              disabled={!canInterrupt || pending || !allowedActions.includes("resume")}
              variant={isPaused ? "primary" : "default"}
              ariaLabel="Resume the paused protocol"
              title={
                interruptHint ??
                (isPaused
                  ? "Paused — click to resume"
                  : pausePending
                    ? "Pausing: the running command finishes first, then play resumes"
                    : "Nothing is paused")
              }
            >
              <PlayGlyph />
            </TileButton>
            {(isPaused || pausePending) && (
              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-900 dark:bg-amber-950/50 dark:text-amber-200">
                {isPaused ? "Paused" : "Pausing…"}
              </span>
            )}
            <TileButton
              onClick={() => runControl("lights.set", () => postSetLights(token, !lightsOn))}
              disabled={locked || pending}
              title={
                controlHint ??
                (lightsKnown
                  ? lightsOn
                    ? "Lights on — click to turn off"
                    : "Lights off — click to turn on"
                  : "Lights state not reported — click to turn on")
              }
            >
              <span
                className={[
                  "mr-1.5 inline-block h-2.5 w-2.5 rounded-full",
                  lightsOn
                    ? "bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.7)]"
                    : "bg-slate-900 dark:bg-black",
                ].join(" ")}
                aria-hidden
              />
              Light
            </TileButton>
            {/* Second line: camera and clearing a head. The light stays on
                the first line with the run controls, where it is reached for
                most often. */}
            <div className="h-0 basis-full" aria-hidden />
            <CameraControl stream={status.equipment_id === "ot2_complexation" ? "cam_echem_tapo_c100_main" : undefined} />
            {/* One per attached pipette. Drops into the fixed trash with no
                location, which is also the recovery when the robot's run
                engine believes a tip is on a head the operator sees bare: the
                engine's record clears with the drop. Gated on the gateway's
                own allowed_actions, like every other robot action here. */}
            {(["left", "right"] as const)
              .filter((mount) => components[`pipette_${mount}`]?.connected)
              .map((mount, _i, attached) => (
                <TileButton
                  key={mount}
                  onClick={() => dropTip(mount)}
                  disabled={locked || pending || !allowedActions.includes("drop_tip")}
                  title={
                    controlHint ??
                    (allowedActions.includes("drop_tip")
                      ? `Drop the ${mount} pipette's tip into the fixed trash. Also clears the robot's own tip record if it believes a tip is on when the head is bare.`
                      : "Drop tip is not available in the current state")
                  }
                >
                  {attached.length > 1 ? `DROP TIP ${mount === "left" ? "L" : "R"}` : "DROP TIP"}
                </TileButton>
              ))}
            {forceDropOffer && (
              <div className="flex basis-full flex-wrap items-center gap-2 rounded border border-amber-400 bg-amber-50 px-2 py-1 text-xs text-amber-900 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-200">
                <span>
                  The robot&apos;s run has no record of a tip on {forceDropOffer}. Is a tip on the head?
                  Force drop homes Z, travels high to the trash and ejects; the head is then recorded bare.
                </span>
                <button
                  type="button"
                  disabled={locked || pending || !allowedActions.includes("drop_tip")}
                  onClick={() => forceDrop(forceDropOffer)}
                  className="rounded border border-amber-600 px-1.5 py-0.5 font-semibold hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-50 dark:hover:bg-amber-900/40"
                >
                  Yes, force drop
                </button>
                <button
                  type="button"
                  onClick={() => setForceDropOffer(null)}
                  className="text-ink-subtle underline dark:text-slate-400"
                >
                  Cancel
                </button>
              </div>
            )}
          </div>


          {/* Directly under the control strip it belongs to: the strip acts on
              the robot, and the answers to "did that work" — control state,
              protocol state, what is on the heads — are right here rather than
              below a slot card that answers a different question. */}
          <ManualPipettePanel equipmentState={status.equipment_status} pipetteComponents={{ left: pipLeft, right: pipRight }} mountedTips={mountedTips} token={token} locked={locked} allowedActions={allowedActions}
            offline={snapshot.fetch_error != null} busyElsewhere={pending}
            refetch={refetch} onBusy={setPending} onError={reportError}
            onStop={stopRun} stopping={stopping} />



          {/* Slot metadata and the plate view are one thing: both answer "what
              is on the slot I clicked". They used to sit in opposite columns,
              so reading a mismatch meant looking left for the declared-vs-
              observed line and right for the wells it applied to. */}
          <Section
            title="SLOT"
            collapsible
          >
            {selectedView && selectedSlot != null && (
              <div className="mb-3 flex flex-col gap-1">
                <KV k="State" v={selectedView.state} />
                {selectedView.moduleName && <KV k="Module" v={selectedView.moduleName} />}
                {selectedView.label && <KV k="Labware" v={selectedView.label} />}
                {selectedView.loadName && (
                  <KV k="Load name (observed)" v={selectedView.loadName} mono />
                )}
                {selectedDeclare && <KV k="Declared as" v={selectedDeclare} mono />}
                {selectedView.state === "mismatch" && selectedView.declared && (
                  <p className="mt-1 rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
                    Mismatch: declared{" "}
                    <span className="font-mono">
                      {selectedView.declared.load_name || selectedView.declared.kind}
                    </span>{" "}
                    but observed{" "}
                    <span className="font-mono">
                      {selectedView.loadName || selectedView.kind || "?"}
                    </span>
                    .
                  </p>
                )}
              </div>
            )}
            <div
              className={
                selectedView && selectedSlot != null
                  ? "border-t border-slate-100 pt-3 dark:border-slate-800"
                  : ""
              }
            >
              <PlateInspector
                slot={selectedSlot}
                view={selectedView}
                tipRacks={tipRacks}
                mountedTips={mountedTips}
              />
            </div>
          {selectedView?.isTiprack && selectedSlot != null && <div className="mt-3 border-t border-slate-100 pt-3 dark:border-slate-800">
            <h3 className="mb-2 text-xs font-semibold">Update tips</h3>
            {!tipRacks.some(r => r.slot === String(selectedSlot)) ? (
              <p className="text-xs text-ink-subtle dark:text-slate-500">
                This rack is not tracked yet. Declare it or load it in a session to enable updates.
              </p>
            ) : (
              <ul className="flex flex-col gap-2">
                {tipRacks.filter(r => r.slot === String(selectedSlot)).map((r) => (
                  <li
                    key={r.slot}
                    className="rounded-md border border-slate-200 px-2 py-1.5 dark:border-slate-800"
                  >
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="min-w-0 truncate text-xs text-ink dark:text-slate-200">
                        Slot {r.slot}
                        {rackLabel(r.slot) && (
                          <span className="ml-1 font-mono text-[11px] text-ink-subtle dark:text-slate-400">
                            {rackLabel(r.slot)}
                          </span>
                        )}
                      </span>
                      <span className="shrink-0 text-xs tabular-nums text-ink-subtle dark:text-slate-400">
                        {r.available}/{r.total} available
                      </span>
                    </div>
                    {(r.empty > 0 || r.touched > 0 || (r.on_pipette ?? 0) > 0) && (
                      <p className="mt-0.5 text-[10px] text-ink-subtle dark:text-slate-500">
                        {r.empty} empty · {r.touched} used
                        {/* Neither used nor available: on the head right now.
                            Named separately so the arithmetic adds up on screen
                            instead of looking like a missing tip. */}
                        {(r.on_pipette ?? 0) > 0 && ` · ${r.on_pipette} on a pipette`}
                      </p>
                    )}
                    {/* Refill is always an explicit operator act: the gateway
                        cannot see new tips going in, and a wrong "full" sends
                        the head onto bare holes. Hence the confirm step. */}
                    {r.available < r.total && (
                      <div className="mt-1.5">
                        {refillConfirm === r.slot ? (
                          <div className="flex items-center gap-2">
                            <span className="text-[10px] text-ink-subtle dark:text-slate-400">
                              All {r.total} tips present in slot {r.slot}?
                            </span>
                            <button
                              type="button"
                              disabled={locked || pending || selectedView.state === "mismatch" || !allowedActions.includes("tips.reset")}
                              onClick={() => refillRack(r.slot)}
                              className="rounded border border-sky-500 px-1.5 py-0.5 text-[10px] font-semibold text-sky-700 hover:bg-sky-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-sky-500 dark:text-sky-300 dark:hover:bg-sky-950/40"
                            >
                              Yes, refilled
                            </button>
                            <button
                              type="button"
                              onClick={() => setRefillConfirm(null)}
                              className="text-[10px] text-ink-subtle underline dark:text-slate-400"
                            >
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <button
                            type="button"
                            disabled={locked || pending || selectedView.state === "mismatch" || !allowedActions.includes("tips.reset")}
                            onClick={() => setRefillConfirm(r.slot)}
                            title={controlHint ?? "Mark every tip in this rack fresh again"}
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-ink-subtle hover:border-slate-400 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:text-slate-400"
                          >
                            Refill rack
                          </button>
                        )}
                      </div>
                    )}
                    {/* The partial counterpart to a refill, for the common case
                        the all-or-nothing reset cannot express: a rack that is
                        genuinely used in some columns and full in others. */}
                    <TipEditor
                      rack={r}
                      disabled={locked || pending || selectedView.state === "mismatch" || !allowedActions.includes("tips.mark")}
                      hint={controlHint}
                      onMark={(selection, status) => markTips(r.slot, selection, status)}
                    />
                  </li>
                ))}
              </ul>
            )}
          </div>}
          </Section>

          <Section title="MODULES">
            {!!(status.details?.platebalance as PlateBalanceStatus | undefined)?.placement_error &&
              <p role="alert" className="mb-2 text-xs text-amber-700 dark:text-amber-400">
                platebalanceV1 · {(status.details!.platebalance as PlateBalanceStatus).placement_error}
              </p>}

            <p className="mb-2 text-xs text-ink-subtle dark:text-slate-400">
              Connected modules without an assigned slot are off deck.
            </p>
            {moduleSlots.size === 0 && robotModules.length === 0 ? (
              <p className="text-xs text-ink-subtle dark:text-slate-500">
                No modules on the deck or attached.
              </p>
            ) : (
              <ul className="flex flex-col gap-2">
                {Array.from(moduleSlots.entries())
                  .sort(([a], [b]) => String(a).localeCompare(String(b), undefined, { numeric: true }))
                  .map(([slot, m]) => (
                    <li
                      key={slot}
                      className="flex flex-col gap-1.5 rounded-md border border-slate-200 px-2 py-1.5 dark:border-slate-800"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="min-w-0 truncate text-xs text-ink dark:text-slate-200">
                          <span className="font-semibold">Slot {slot}</span> · {m.name}
                        </span>
                        {m.name !== "platebalanceV1" && <ModuleReadout live={m.live} compact />}
                      </div>
                      {m.name === "platebalanceV1" && status.details?.platebalance ? (
                        <PlateBalanceControls balance={status.details.platebalance as PlateBalanceStatus}
                          disabled={locked || pending} allowedActions={allowedActions}
                          onAction={(action, waitUntilStable) => { void runControl(`platebalance.${action}`, () => postPlateBalance(token, action, waitUntilStable)); }} />
                      ) : null}
                      {moduleFamily(m.name) === "temperature" && (
                        <TempModuleControls
                          slot={slot}
                          live={m.live}
                          disabled={
                            locked ||
                            pending ||
                            !allowedActions.includes("tempmod.set")
                          }
                          hint={controlHint}
                          onSet={(celsius) =>
                            runControl("tempmod.set", () =>
                              postSetTempmod(token, celsius, String(slot)),
                            )
                          }
                          onOff={() =>
                            runControl("tempmod.deactivate", () =>
                              postDeactivateTempmod(token, String(slot)),
                            )
                          }
                        />
                      )}
                    </li>
                  ))}
                {unattachedToSlots.map((module, index) => (
                  <li key={`unassigned-${module.id ?? module.serial ?? index}`}
                    className="flex flex-col gap-1.5 rounded-md border border-slate-200 px-2 py-1.5 dark:border-slate-800">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs"><strong>Unassigned · off deck</strong> · {module.model || module.type}
                        {module.serial && <span className="ml-1 font-mono">{module.serial}</span>}</span>
                      <ModuleReadout live={module} compact />
                    </div>

                  </li>
                ))}
              </ul>
            )}
          </Section>

          {/* What every plan run here did, from the records saved as it ran:
              live progress and readings, still there after a restart. */}
          <Section title="RUNS" collapsible>
            <RunRecords />
          </Section>


          <Section title="Claim">
            {claimedBy ? (
              <div className="flex flex-col gap-1">
                <KV k="Holder" v={claimedByMe ? `${claimedBy.owner} (you)` : claimedBy.owner} mono />
                <KV k="Session" v={claimedBy.session_id || "—"} mono />
                <KV k="Expires" v={claimedBy.expires_at || "—"} mono />
              </div>
            ) : (
              <p className="text-xs text-ink-subtle dark:text-slate-500">
                No claim held — click <span className="font-semibold">Take control</span> to
                acquire one and unlock the controls.
              </p>
            )}
          </Section>
        </div>
      </div>

      {/* Footer strip */}
      <footer className="flex items-end justify-between gap-2 border-t border-slate-100 pt-2 text-xs text-ink-subtle dark:border-slate-800 dark:text-slate-400">
        <div className="min-w-0 flex-1 space-y-0.5">
          {status.message && (
            <div className="truncate" title={status.message}>
              {status.message}
            </div>
          )}
          {(status.required_actions?.length ?? 0) > 0 && (
            <div className="truncate">
              <span className="font-semibold text-amber-700 dark:text-amber-400">
                Action needed:
              </span>{" "}
              <span className="font-mono">{status.required_actions?.join(", ")}</span>
            </div>
          )}
          <a
            href="/docs"
            className="text-sky-700 underline-offset-2 hover:underline dark:text-sky-400"
          >
            API docs (Swagger) ↗
          </a>
        </div>
        <div className="flex shrink-0 items-center gap-2 tabular-nums">
          {snapshot.latency_ms != null && <span>{snapshot.latency_ms} ms</span>}
          <StalenessIndicator fetchedAt={snapshot.fetched_at} />
        </div>
      </footer>
    </div>
  );
}
