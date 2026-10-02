import { startPositionPolling } from "../lib/position-polling";
import { TileButton } from "./TileButton";
import { PANEL_STYLE, PANEL_HEADING_STYLE } from "./panel-styles";
import { useEffect, useId, useRef, useState } from "react";
import { postHome, postHomePipetteZ, jogPipette, readPipettePosition } from "../lib/api";
import { copyText, jogLimitReason, JOG_SPEEDS, JOG_STEPS, validJogSettings, xyzText } from "../lib/manual-motion";
import type { MountedTip } from "../lib/ot2-deck";
import type { ComponentStatus, EquipmentState, PipettePosition } from "../lib/types";

export function ManualPipettePanel({ equipmentState, pipetteComponents = {}, mountedTips = [], token, locked, allowedActions, offline, busyElsewhere = false, refetch, onBusy, onError, onStop, stopping }: {
  equipmentState?: EquipmentState;
  pipetteComponents?: Partial<Record<"left" | "right", ComponentStatus>>; mountedTips?: MountedTip[];
  token: string | null; locked: boolean; allowedActions: string[]; offline: boolean;
  busyElsewhere?: boolean;
  refetch: () => void;
  onBusy: (value: boolean) => void; onError: (error: unknown, action: string) => void;
  onStop: () => void; stopping: boolean;
}) {
  const tabId = useId();
  const [drive, setDrive] = useState<"Z" | "XY">("XY");
  const [expanded, setExpanded] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [pipette, setPipette] = useState("");
  const [step, setStep] = useState("1");
  const [speed, setSpeed] = useState("10");
  const [zStep, setZStep] = useState("1");
  const [zSpeed, setZSpeed] = useState("10");
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const backgroundRead = useRef<Promise<void> | null>(null);
  const foregroundPending = useRef(false);
  const backgroundFailed = useRef(false);
  const selectionReadRequested = useRef(false);
  const epoch = useRef(0);
  const [position, setPosition] = useState<PipettePosition | null>(null);
  const [mountPositions, setMountPositions] = useState<Record<string, PipettePosition>>({});
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [copyValue, setCopyValue] = useState("");

  useEffect(() => {
    epoch.current += 1;
    setPosition(null); setCopied(false); setCopyValue("");
    backgroundFailed.current = false;
    if (locked || offline) setEnabled(false);
  }, [pipette, token, locked, offline]);
  useEffect(() => { setMountPositions({}); }, [token, locked, offline]);
  useEffect(() => () => { epoch.current += 1; }, []);

  // Historical values are display-only; jog bounds continue to use position.
  const shownPosition = !locked && !offline
    ? (position?.pipette === pipette ? position : mountPositions[pipette] ?? null)
    : null;

  function selectPipette(mount: string) {
    setPosition(null); setCopied(false); setCopyValue("");
    selectionReadRequested.current = mount !== pipette;
    setPipette(current => current === mount ? "" : mount);
  }

  const readActive = !!pipette && !!token && !locked && !offline && !busyElsewhere;
  const active = enabled && readActive;
  const canRead = readActive && !busy && allowedActions.includes("pipette_position");
  useEffect(() => {
    if (selectionReadRequested.current && canRead && equipmentState === "ready") {
      // One operator-requested read after selection, including selection while
      // busy. Never clears a gateway fault or retries a movement.
      selectionReadRequested.current = false;
      setError(null); backgroundFailed.current = false;
    }
  }, [canRead, equipmentState, pipette]);
  const canJogAxis = (axis: "x" | "y" | "z") => drive === (axis === "z" ? "Z" : "XY") && active && !busy && allowedActions.includes("jog")
    && validJogSettings(Number(axis === "z" ? zStep : step), Number(axis === "z" ? zSpeed : speed));
  const inputStyle = "min-w-0 rounded border border-slate-300 bg-white px-2 py-1 text-ink disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100";
  const buttonStyle = "rounded border border-slate-300 px-3 py-2 text-xs font-medium hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-600 dark:hover:bg-slate-800";

  const axisColors = {
    XY: "border-sky-300 bg-sky-50 text-sky-800 hover:bg-sky-100 active:bg-sky-200 focus-visible:outline-sky-500 dark:border-sky-700 dark:bg-sky-950 dark:text-sky-200 dark:hover:bg-sky-900",
    Z: "border-violet-300 bg-violet-50 text-violet-800 hover:bg-violet-100 active:bg-violet-200 focus-visible:outline-violet-500 dark:border-violet-700 dark:bg-violet-950 dark:text-violet-200 dark:hover:bg-violet-900",
  };
  const axisSelection = {
    XY: "aria-pressed:border-sky-600 aria-pressed:bg-sky-600 aria-pressed:text-white dark:aria-pressed:border-sky-400 dark:aria-pressed:bg-sky-600 dark:aria-pressed:text-white",
    Z: "aria-pressed:border-violet-600 aria-pressed:bg-violet-600 aria-pressed:text-white dark:aria-pressed:border-violet-400 dark:aria-pressed:bg-violet-600 dark:aria-pressed:text-white",
  };
  const presetStyle = "rounded border px-2 py-2 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2";
  const padButtonStyle = "flex h-14 w-14 items-center justify-center rounded-lg border text-xs font-semibold shadow-sm transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 disabled:cursor-not-allowed disabled:border-slate-200 disabled:bg-slate-100 disabled:text-slate-400 disabled:shadow-none disabled:hover:bg-slate-100 dark:disabled:border-slate-700 dark:disabled:bg-slate-800 dark:disabled:text-slate-500 dark:disabled:hover:bg-slate-800";


  async function run(action: string, operation: () => Promise<PipettePosition>, copy = false, background = false) {
    const permitted = action === "pipette_position" ? canRead : active;
    if (!permitted || foregroundPending.current || (inFlight.current && !backgroundRead.current)) return;
    const generation = epoch.current;
    if (!background) {
      foregroundPending.current = true;
      setBusy(true); onBusy(true);
      // Let the one current read finish; never drop a click or queue two moves.
      if (backgroundRead.current) await backgroundRead.current;
      if (generation !== epoch.current || backgroundFailed.current) {
        foregroundPending.current = false; setBusy(false); onBusy(false);
        return;
      }
    }
    inFlight.current = true;
    if (background) backgroundFailed.current = false;
    setError(null);
    if (!background) setCopied(false);
    // Retain timestamped readback and limit layout until the command returns.
    // Busy guards prevent using the prior reading for another move.
    let failed = false;
    try {
      const result = await operation();
      if (generation !== epoch.current) return;
      setPosition(result);
      setMountPositions(previous => ({ ...previous, [pipette]: result }));
      if (copy) {
        const text = xyzText(result);
        // Clipboard failure must not be mistaken for a failed robot command.
        try { await copyText(text); setCopied(true); setCopyValue(""); }
        catch (e) { setCopyValue(text); setError(e instanceof Error ? e.message : String(e)); }
      }
    } catch (e) {
      failed = true;
      if (background) backgroundFailed.current = true;
      if (generation === epoch.current) { setPosition(null); setError(e instanceof Error ? e.message : String(e)); }
      onError(e, action);
    } finally {
      inFlight.current = false;
      if (!background) {
        foregroundPending.current = false; setBusy(false); onBusy(false);
        if (failed || (action !== "jog" && action !== "pipette_position")) refetch();
      }
    }
  }

  const polling = useRef({ canRead, error, run, token, pipette });
  polling.current = { canRead, error, run, token, pipette };
  useEffect(() => {
    if (!pipette || !token || locked || offline) return;
    return startPositionPolling(
      () => polling.current.canRead && !polling.current.error && !inFlight.current && !document.hidden,
      async () => {
        const current = polling.current;
        const request = current.run("pipette_position", () => readPipettePosition(current.token, current.pipette), false, true);
        backgroundRead.current = request;
        try { await request; }
        finally { if (backgroundRead.current === request) backgroundRead.current = null; }
      },
    );
  }, [pipette, token, locked, offline]);

  function settings(group: "XY" | "Z") {
    const distance = group === "Z" ? zStep : step;
    const rate = group === "Z" ? zSpeed : speed;
    const setRate = group === "Z" ? setZSpeed : setSpeed;
    return <div className="mt-3 flex flex-col gap-2">
      <div className="flex items-center gap-2">
      <p className="w-20 shrink-0 text-slate-500">Speed(mm/s)</p>
      <div role="group" aria-label={`${group} speed presets`} className="flex flex-wrap gap-1">{JOG_SPEEDS.map(v => <button type="button" key={v} aria-pressed={Number(rate) === v} disabled={busy} className={`${presetStyle} ${axisColors[group]} ${axisSelection[group]}`} onClick={() => setRate(String(v))}>{v}</button>)}</div>
      </div>
      {!validJogSettings(Number(distance), Number(rate)) && <p role="alert">{group}: step &gt;0–10 mm; speed &gt;0–100 mm/s.</p>}
    </div>;
  }

  function jog(axis: "x" | "y" | "z", sign: number) {
    const distance = sign * Number(axis === "z" ? zStep : step);
    const rate = Number(axis === "z" ? zSpeed : speed);
    if (!canJogAxis(axis) || jogLimitReason(position, axis, distance)) return;
    void run("jog", () => jogPipette(token, pipette, axis, distance, rate));
  }

  return <section aria-label="Manual pipette control" aria-busy={busy} className={`${PANEL_STYLE} text-xs text-ink dark:text-slate-200`}>
    <div className="flex flex-col gap-3">
      <button type="button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)} className={`flex shrink-0 items-center gap-1 cursor-pointer ${PANEL_HEADING_STYLE}`}>
        <span aria-hidden="true">{expanded ? "▾" : "▸"}</span>DIRECT DRIVE
      </button>
    </div>
    <div hidden={!expanded} className="mt-3">
      <div className="flex w-full min-w-0 items-center justify-between gap-3">
      <div className="flex flex-wrap items-center gap-2">
      <label className="flex items-center gap-2">
        <span>Manual</span>
        <button type="button" role="switch" aria-label="Manual" aria-checked={enabled}
          disabled={busy || (locked || offline) && !enabled} onClick={() => setEnabled(!enabled)}
          className={`relative h-5 w-9 rounded-full transition-colors disabled:opacity-40 ${enabled ? "bg-sky-600" : "bg-slate-400 dark:bg-slate-600"}`}>
          <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${enabled ? "left-0.5 translate-x-4" : "left-0.5"}`} />
        </button>
      </label>
        {(locked || !pipette) && <span className="text-amber-700 dark:text-amber-400">{locked ? "Take control" : "Select a pipette"}</span>}
      </div>
      <div className="flex items-center gap-2">
      <TileButton variant="stop" disabled={locked || offline || stopping || !allowedActions.includes("stop")} onClick={onStop} ariaLabel="Stop this robot run">{stopping ? "STOPPING…" : "STOP"}</TileButton>
      </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2" role="group" aria-label="Selected pipette">
        {(["left", "right"] as const).map(mount => {
          const tip = mountedTips.find(t => t.pipette === mount);
          const lastPosition = mountPositions[mount];
          return <div key={mount} className="flex min-w-0 flex-col gap-2">
            <button type="button" aria-label={`Select ${mount} pipette`} aria-pressed={pipette === mount} disabled={busy} onClick={() => selectPipette(mount)}
              className="rounded border border-slate-200 p-2 text-left hover:bg-slate-50 disabled:opacity-40 aria-pressed:border-sky-500 aria-pressed:bg-sky-50 dark:border-slate-700 dark:hover:bg-slate-800 dark:aria-pressed:bg-sky-950">
              <span className="block break-words">{offline ? "Unknown" : pipetteComponents[mount]?.state ?? "Unknown"}</span>
              <span className="block">{offline ? "Unknown" : tip ? `${tip.uncertain ? "Uncertain" : "Mounted"}${tip.rack ? ` · rack ${tip.rack}` : ""}${tip.well ? ` / ${tip.well}` : ""}` : "No tip recorded"}</span>
              <span className="block" title="Last controller reading for this pipette; not live while another pipette is selected">Z: {lastPosition?.coordinates ? `${lastPosition.coordinates.z.toFixed(3)} mm${lastPosition.source !== "robot" ? " (simulation)" : ""}` : "—"}</span>
              {lastPosition?.coordinates && <span className="block text-slate-500">Last read {new Date(lastPosition.observed_at).toLocaleTimeString()}</span>}
            </button>
          </div>;
        })}
      </div>
      <div className="mt-3 flex justify-center overflow-x-auto border-b border-slate-200 dark:border-slate-700">
      <div role="tablist" aria-label="Drive axis" className="grid shrink-0 grid-cols-[3.5rem_11.5rem] gap-6">
        {(["Z", "XY"] as const).map(axis => <button key={axis} type="button" role="tab"
          id={`${tabId}-tab-${axis}`} aria-controls={`${tabId}-panel-${axis}`} aria-selected={drive === axis} tabIndex={drive === axis ? 0 : -1}
          onClick={() => setDrive(axis)} onKeyDown={event => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === "Home" ? "Z" : event.key === "End" ? "XY" : axis === "Z" ? "XY" : "Z";
            setDrive(next); document.getElementById(`${tabId}-tab-${next}`)?.focus();
          }}
          className={`rounded-t border-b-2 border-transparent px-2 py-2 font-medium ${axisColors[axis]} ${axis === "XY" ? "aria-selected:border-sky-600 dark:aria-selected:border-sky-400" : "aria-selected:border-violet-600 dark:aria-selected:border-violet-400"}`}>Move {axis}</button>)}
      </div>
      </div>
      <div className="mt-3 flex items-center gap-2 overflow-x-auto">
      <span className="w-20 shrink-0 whitespace-nowrap text-slate-500">Step(mm)</span>
      {(["Z", "XY"] as const).map(axis => <div key={axis} hidden={drive !== axis}>
        <div role="group" aria-label={`${axis} distance presets`} className="flex gap-1">
          {JOG_STEPS.map(value => <button type="button" key={value} disabled={busy}
            aria-pressed={Number(axis === "Z" ? zStep : step) === value}
            className={`${presetStyle} ${axisColors[axis]} ${axisSelection[axis]}`}
            onClick={() => (axis === "Z" ? setZStep : setStep)(String(value))}>{value}</button>)}
        </div>
      </div>)}
      </div>
      <div className="mt-3 flex justify-center gap-6" aria-label="Jog controls">
        <div className="grid grid-cols-[3.5rem] grid-rows-[repeat(3,3.5rem)] gap-2" aria-label="Z controls">
          {([1, -1] as const).map(sign => <button key={sign} type="button" className={`${sign > 0 ? "row-start-1" : "row-start-3"} ${padButtonStyle} ${axisColors.Z}`}
            title={jogLimitReason(position, "z", sign * Number(zStep)) ?? undefined}
            disabled={!canJogAxis("z") || !!jogLimitReason(position, "z", sign * Number(zStep))}
            onKeyDown={e => { if (e.repeat) e.preventDefault(); }} onClick={() => jog("z", sign)}>{sign > 0 ? "Z+" : "Z−"}</button>)}
          <button type="button" aria-label="Home Z selected pipette" className={`col-start-1 row-start-2 ${padButtonStyle} ${axisColors.Z}`}
            disabled={drive !== "Z" || !active || busy || !allowedActions.includes("home_pipette_z")}
            onClick={() => void run("home_pipette_z", async () => {
              await postHomePipetteZ(token, pipette);
              return readPipettePosition(token, pipette);
            })}>Z Home</button>
        </div>
        <div className="grid grid-cols-[repeat(3,3.5rem)] grid-rows-[repeat(3,3.5rem)] gap-2" aria-label="XY movement">
            {([ ["y", 1, "col-start-2 row-start-1"], ["x", -1, "col-start-1 row-start-2"], ["x", 1, "col-start-3 row-start-2"], ["y", -1, "col-start-2 row-start-3"] ] as const).map(([axis, sign, placement]) => <button key={`${axis}${sign}`} type="button"
              className={`${placement} ${padButtonStyle} ${axisColors.XY}`}
              title={jogLimitReason(position, axis, sign * Number(step)) ?? undefined}
              disabled={!canJogAxis(axis) || !!jogLimitReason(position, axis, sign * Number(step))}
              onKeyDown={e => { if (e.repeat) e.preventDefault(); }} onClick={() => jog(axis, sign)}>{axis.toUpperCase()}{sign > 0 ? "+" : "−"}</button>)}
          <button type="button" aria-label="Home robot" title="Home robot (all axes)" className={`col-start-2 row-start-2 ${padButtonStyle} ${axisColors.XY}`}
            disabled={drive !== "XY" || !active || busy || !allowedActions.includes("home")}
            onClick={() => void run("home", async () => {
              setMountPositions({}); setPosition(null);
              await postHome(token);
              return readPipettePosition(token, pipette);
            })}>HOME</button>
        </div>
      </div>
      {(["Z", "XY"] as const).map(axis => <div key={axis} role="tabpanel" id={`${tabId}-panel-${axis}`} aria-labelledby={`${tabId}-tab-${axis}`} hidden={drive !== axis}>
        {settings(axis)}
      </div>)}
    {offline && <p className="mt-2 text-amber-700">Gateway status unavailable.</p>}
    {active && !busy && !inFlight.current && equipmentState !== "busy" && !allowedActions.includes("jog") && <p className="mt-2 text-amber-700">Manual jog is unavailable{equipmentState ? ` (${equipmentState})` : " in the current robot state"}.</p>}
        {position?.coordinate_limits && <p className="mt-3 text-[10px] leading-4 text-slate-500">Gateway limits (mm): {Object.entries(position.coordinate_limits).map(([axis, range]) => `${axis.toUpperCase()} ${range[0]}–${range[1]}`).join(" · ")}</p>}
    <div className="mt-3 flex flex-col gap-1 border-t border-slate-200 pt-3 dark:border-slate-800">
        <div className="flex items-center gap-2 overflow-x-auto">
        <div aria-label="XYZ position in millimeters" className="flex items-center gap-2 whitespace-nowrap font-mono text-xs tabular-nums">
          {(["x", "y", "z"] as const).map(axis => <div key={axis} className="flex items-center gap-1">{axis.toUpperCase()} <span className="inline-block w-[8ch]">{shownPosition?.coordinates?.[axis]?.toFixed(3) ?? "—"}</span></div>)}
        </div>

        <div className="shrink-0">
          <button type="button" className={buttonStyle} disabled={!canRead} onClick={() => void run("pipette_position", () => readPipettePosition(token, pipette), true)}>{copied ? "Copied" : "Copy XYZ"}</button>
        </div>
        </div>
        <div className="flex min-h-4 items-baseline justify-between gap-2 text-[10px] leading-4 text-slate-500">
        <p>{shownPosition ? shownPosition.source === "dry_run" ? "Dry run: no physical position reported." : shownPosition.source === "simulation" ? "Simulation coordinates; not a physical position." : `Last read ${new Date(shownPosition.observed_at).toLocaleTimeString()} · ${pipette}${!position ? " · awaiting fresh read" : ""}` : pipette ? "Waiting for position read" : ""}</p>
        <span className="shrink-0" role="status">{busy ? "Moving / reading…" : ""}</span>
        </div>
        {copyValue && <textarea aria-label="XYZ for workflow" className={`${inputStyle} h-24 font-mono`} readOnly value={copyValue} onFocus={e => e.target.select()} />}
      </div>
    {error && <div className="mt-3 rounded border border-red-400 bg-red-50 p-3 text-red-800 dark:bg-red-950 dark:text-red-200" role="alert"><p className="font-semibold">Manual control error</p><p className="whitespace-pre-wrap break-words">{error}</p><p className="mt-1">Check the robot state before another move. Clear the message to resume position updates.</p><button type="button" className={`${buttonStyle} mt-2`} onClick={() => { backgroundFailed.current = false; setError(null); }}>Clear error</button></div>}
    </div>
  </section>;
}
