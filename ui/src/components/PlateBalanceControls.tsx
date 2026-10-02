import { useState } from "react";
import type { PlateBalanceStatus } from "../lib/types";

export function PlateBalanceControls({ balance, disabled, allowedActions, onAction }: {
  balance: PlateBalanceStatus;
  disabled: boolean;
  allowedActions: string[];
  onAction: (action: "read" | "tare" | "zero", waitUntilStable: boolean) => void;
}) {
  const [waitUntilStable, setWaitUntilStable] = useState(true);
  const reading = balance.reading;
  return <div className="flex flex-col gap-1.5 text-xs">
    <div className="flex items-center gap-2">
      <div aria-label="Last measured weight" className="inline-flex items-baseline gap-2 rounded border border-slate-300 bg-slate-50 px-3 py-2 font-mono text-sm tabular-nums dark:border-slate-600 dark:bg-slate-900">
        <span className="inline-block w-[9ch] whitespace-pre text-right">{reading ? reading.value.toFixed(4).padStart(8, " ") : "       —"}</span>
        <span>g</span>
      </div>
      <span className="text-ink-subtle">{reading ? (reading.stable ? "Stable" : "Unstable") : "No reading"}</span>
    </div>
    <label className="flex items-center gap-2 text-ink-subtle">
      <input type="checkbox" checked={waitUntilStable} disabled={disabled}
        onChange={event => setWaitUntilStable(event.target.checked)} />
      Weight read: wait until stable · 10 s max
    </label>
    <p className="text-ink-subtle">Tare waits up to 30 s for a stable zero baseline.</p>
    <div className="flex gap-2">
      {(["read", "tare", "zero"] as const).map(action => <button key={action} type="button"
        disabled={disabled || !balance.configured || !allowedActions.includes(`platebalance.${action}`)}
        onClick={() => onAction(action, action === "read" && waitUntilStable)}
        className="rounded border border-slate-300 px-3 py-1 text-xs hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-600 dark:hover:bg-slate-800">
        {{ read: "Weight", tare: "Tare", zero: "Zero" }[action]}
      </button>)}
    </div>
    {balance.last_operation?.outcome === "sent_unconfirmed" && <p className="text-ink-subtle">Command sent · Weight to check.</p>}
    {balance.last_operation?.outcome === "baseline_observed" && <p className="text-emerald-700 dark:text-emerald-400">Stable zero baseline observed after tare.</p>}
    {balance.last_operation?.outcome === "baseline_unconfirmed" && <p className="text-red-600 dark:text-red-400">Tare sent · Stable zero was not observed.</p>}
    {balance.last_error && <p role="alert" className="text-red-600 dark:text-red-400">{balance.last_error}</p>}
  </div>;
}
