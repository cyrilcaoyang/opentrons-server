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

/** "14:32" today, "Oct 5 14:32" otherwise — the full stamp is in the tooltip. */
function when(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString()
    ? time
    : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

/** One-word ELN state for the compact row; the full sentence is the tooltip. */
function elnShort(r: RunRecord): string {
  const d = r.delivery;
  if (d.state === "local_only") return "robot only";
  if (d.state === "running") return "ELN later";
  if (d.state === "pending") return "sending";
  if (d.state === "delivered") {
    if (d.eln?.state === "filed") return "filed";
    if (d.eln?.state === "held") return "held";
    return d.eln?.check_error ? "status ?" : "filing";
  }
  return d.state;
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
      getRunRecords(20)
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
    return <p className="text-xs text-rose-700 dark:text-rose-400">Runs unavailable: {error}</p>;
  }
  if (!records) return <p className="text-xs text-ink-subtle dark:text-slate-400">Loading runs…</p>;
  if (records.length === 0) {
    return error ? (
      <p role="alert" className="text-xs text-rose-700 dark:text-rose-400">Could not refresh runs ({error}).</p>
    ) : (
      <p className="text-xs text-ink-subtle dark:text-slate-400">No plans have run on this gateway yet.</p>
    );
  }

  const link =
    "rounded px-1 text-[10px] font-medium text-purple-700 underline-offset-2 hover:bg-purple-100 hover:underline dark:text-purple-300 dark:hover:bg-purple-900/40";
  return (
    <>
      {error && (
        <p role="alert" className="mb-1 text-[11px] text-rose-700 dark:text-rose-400">
          Could not refresh runs ({error}); what is shown may be out of date.
        </p>
      )}
      <ul className="max-h-72 divide-y divide-slate-200 overflow-y-auto pr-1 dark:divide-slate-800">
        {records.map((r) => {
          const pct = r.steps_total ? Math.round((100 * r.steps_done) / r.steps_total) : 0;
          const eln = elnOutcome(r);
          const problem = r.halt_reason ?? (r.delivery.eln?.state === "held" ? eln.text : null);
          return (
            <li key={r.plan_id} className="py-1 text-[11px] leading-tight">
              <div className="flex items-center gap-1.5 whitespace-nowrap">
                <span className={`rounded px-1 text-[10px] font-semibold ${STATUS_TONE[r.status] ?? STATUS_TONE.aborted}`}>
                  {r.status === "executing" ? "running" : r.status}
                </span>
                <span className="font-mono" title={r.plan_id}>{r.plan_id.slice(0, 8)}</span>
                <span className="tabular-nums text-ink-subtle dark:text-slate-400">
                  {r.steps_done}/{r.steps_total}
                  {(r.readings ?? 0) > 0 && ` · ${r.readings} rd`}
                  {r.steps_failed > 0 && ` · ${r.steps_failed} ✗`}
                  {(r.steps_unknown ?? 0) > 0 && ` · ${r.steps_unknown} ?`}
                </span>
                <span
                  className={`truncate font-medium ${ELN_TONE[eln.tone]}`}
                  title={`${r.eln_project ? `${r.eln_project}: ` : ""}${eln.text}`}
                >
                  {elnShort(r)}
                </span>
                <span className="ml-auto flex shrink-0">
                  {r.can_open === false ? (
                    <span
                      className="px-1 text-[10px] text-ink-subtle dark:text-slate-500"
                      title={`Only ${r.approved_by ?? "its approver"}, members of ${r.eln_project ?? "its ELN project"} and admins can open this run's data.`}
                    >
                      🔒 restricted
                    </span>
                  ) : (<>
                  {(r.readings ?? 0) > 0 && (
                    <>
                      <a href={plateReportUrl([r.plan_id])} target="_blank" rel="noopener" className={link}
                        title="Per-well balance results as a 96-well heatmap (refresh to update while running)">
                        Report
                      </a>
                      <a href={plateReportUrl([r.plan_id], "xlsx")} download className={link} title="Spreadsheet">
                        XLSX
                      </a>
                    </>
                  )}
                  <a href={runRecordUrl(r.plan_id)} target="_blank" rel="noopener" className={link}
                    title="The full saved record: every step, its outcome, reading and timestamps">
                    Record
                  </a>
                  </>)}
                </span>
              </div>
              <div className="mt-0.5 h-0.5 w-full overflow-hidden rounded bg-slate-200 dark:bg-slate-800" aria-hidden>
                <div
                  className={`h-full ${r.steps_failed || r.steps_unknown ? "bg-rose-500" : "bg-emerald-500"}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <p
                className="mt-0.5 truncate text-[10px] text-ink-subtle dark:text-slate-400"
                title={`approved by ${r.approved_by ?? "unknown"} · started ${r.started_at ?? "—"}${r.finished_at ? ` · ended ${r.finished_at}` : ""}`}
              >
                {r.approved_by ?? "unknown approver"} · {when(r.started_at)}
                {r.finished_at && r.status !== "executing" && ` – ${when(r.finished_at).split(" ").pop()}`}
              </p>
              {problem && (
                <p className="mt-0.5 truncate text-[10px] text-rose-700 dark:text-rose-400" title={problem}>
                  {problem}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </>
  );
}
