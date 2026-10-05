import { useEffect, useRef, useState } from "react";

import { elnOutcome, getRunRecords, plateReportUrl, runRecordUrl, type RunRecord } from "../lib/api";

/**
 * Run records — every plan this gateway ran, read from the records it saves
 * on disk as the plan runs (after each step starts and ends). A running plan
 * shows its progress and readings live; a record survives a gateway restart,
 * and a run cut off by one is shown as interrupted, never as finished.
 *
 * Read-only: nothing here acts on the robot.
 */

const STATUS_TONE: Record<string, string> = {
  executing: "bg-violet-100 text-violet-800 dark:bg-violet-900/40 dark:text-violet-200",
  executed: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300",
  failed: "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300",
  aborted: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  interrupted: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
};

const ELN_TONE: Record<string, string> = {
  ok: "text-emerald-700 dark:text-emerald-400",
  warn: "text-amber-700 dark:text-amber-400",
  bad: "text-rose-700 dark:text-rose-400",
  muted: "text-ink-subtle dark:text-slate-400",
};

function when(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function RunRecords() {
  const [records, setRecords] = useState<RunRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Poll fast while a run is going or its ELN outcome is still unknown.
  const running = records?.some((r) => r.status === "executing" ||
    r.delivery.state === "pending" || (r.delivery.state === "delivered" &&
      !["filed", "held"].includes(r.delivery.eln?.state ?? ""))) ?? false;
  // One request at a time: a slow gateway must not let an older response
  // land after a newer one.
  const inFlight = useRef(false);

  useEffect(() => {
    let active = true;
    const load = () => {
      if (inFlight.current) return;
      inFlight.current = true;
      getRunRecords(10)
        .then((rows) => {
          if (!active) return;
          setRecords(rows);
          setError(null);
        })
        .catch((e: unknown) => active && setError(e instanceof Error ? e.message : String(e)))
        .finally(() => {
          inFlight.current = false;
        });
    };
    void load();
    // Fast while a run is going, so steps and readings appear as they land.
    const timer = setInterval(load, running ? 3000 : 15000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [running]);

  if (error && !records) {
    return <p className="text-xs text-rose-700 dark:text-rose-400">Run records unavailable: {error}</p>;
  }
  if (!records) return <p className="text-xs text-ink-subtle dark:text-slate-400">Loading run records…</p>;
  if (records.length === 0) {
    return error ? (
      <p role="alert" className="text-xs text-rose-700 dark:text-rose-400">Could not refresh run records ({error}).</p>
    ) : (
      <p className="text-xs text-ink-subtle dark:text-slate-400">No plans have run on this gateway yet.</p>
    );
  }

  return (
    <>
    {error && (
      <p role="alert" className="mb-2 text-xs text-rose-700 dark:text-rose-400">
        Could not refresh run records ({error}); what is shown may be out of date.
      </p>
    )}
    <ul className="flex flex-col gap-2">
      {records.map((r) => {
        const pct = r.steps_total ? Math.round((100 * r.steps_done) / r.steps_total) : 0;
        const link =
          "rounded px-1.5 py-0.5 text-[10px] font-medium text-purple-700 underline-offset-2 hover:bg-purple-100 hover:underline dark:text-purple-300 dark:hover:bg-purple-900/40";
        return (
          <li key={r.plan_id} className="rounded border border-slate-200 p-2 text-xs dark:border-slate-800">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className={`rounded px-1 py-px text-[10px] font-semibold ${STATUS_TONE[r.status] ?? STATUS_TONE.aborted}`}>
                {r.status === "executing" ? "running" : r.status}
              </span>
              <span className="font-mono text-[11px]">{r.plan_id.slice(0, 8)}</span>
              <span className="text-ink-subtle dark:text-slate-400">
                {when(r.started_at)} · {r.approved_by ?? "unknown approver"}
              </span>
              <span className="ml-auto flex gap-0.5">
                {(r.readings ?? 0) > 0 && (
                  <>
                    <a href={plateReportUrl([r.plan_id])} target="_blank" rel="noopener" className={link}
                      title="Per-well balance results as a 96-well heatmap (refresh to update while running)">
                      Plate report
                    </a>
                    <a href={plateReportUrl([r.plan_id], "xlsx")} download className={link}>Spreadsheet</a>
                  </>
                )}
                <a href={runRecordUrl(r.plan_id)} target="_blank" rel="noopener" className={link}
                  title="The full saved record: every step, its outcome, reading and timestamps">
                  Record
                </a>
              </span>
            </div>
            <div className="mt-1 flex items-center gap-2">
              <div className="h-1.5 flex-1 overflow-hidden rounded bg-slate-200 dark:bg-slate-800" aria-hidden>
                <div
                  className={`h-full ${r.steps_failed || r.steps_unknown ? "bg-rose-500" : "bg-emerald-500"}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <span className="tabular-nums text-ink-subtle dark:text-slate-400">
                {r.steps_done}/{r.steps_total} steps
                {(r.readings ?? 0) > 0 && ` · ${r.readings} readings`}
                {r.steps_failed > 0 && ` · ${r.steps_failed} failed`}
                {(r.steps_unknown ?? 0) > 0 && ` · ${r.steps_unknown} unknown`}
              </span>
            </div>
            <p className={`mt-1 text-[11px] ${ELN_TONE[elnOutcome(r).tone]}`}>
              {r.eln_project ? `${r.eln_project} — ` : ""}
              {elnOutcome(r).text}
            </p>
            {r.halt_reason && (
              <p className="mt-0.5 text-[11px] text-rose-700 dark:text-rose-400">{r.halt_reason}</p>
            )}
          </li>
        );
      })}
    </ul>
    </>
  );
}
