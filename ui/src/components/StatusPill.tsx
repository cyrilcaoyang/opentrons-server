import type { EquipmentState } from "../lib/types";
import { stateClass, stateLabel } from "../lib/format";

/** `stopped` overrides the state label: after a software stop the gateway
 *  reports `error` (confirmed) or `unknown` (not confirmed) and neither word
 *  tells an operator what happened. The latch does. */
export function StatusPill({ state, stopped }: { state: EquipmentState; stopped?: "confirmed" | "unconfirmed" }) {
  const label = stopped ? "STOPPED" : stateLabel(state);
  const tone = stopped
    ? "bg-rose-100 text-rose-900 ring-rose-300 dark:bg-rose-950/50 dark:text-rose-200 dark:ring-rose-800"
    : stateClass(state);
  const title = stopped === "confirmed"
    ? "Software stop confirmed by the robot. Inspect it, close this session, then start a fresh one."
    : stopped === "unconfirmed"
      ? "Stop requested but the robot did not confirm stopped motion. Inspect it before anything else, then close this session and start a fresh one."
      : undefined;
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
      {label}
    </span>
  );
}
