import type { PipettePosition } from "./types";

export const JOG_STEPS = [0.1, 0.5, 1, 5, 10] as const;
export const JOG_SPEEDS = [1, 5, 10, 25, 50, 100] as const;

export function validJogSettings(step: number, speed: number): boolean {
  return Number.isFinite(step) && step > 0 && step <= 10 && Number.isFinite(speed) && speed > 0 && speed <= 100;
}

export function xyzText(readback: PipettePosition): string {
  const xyz = readback.coordinates;
  if (readback.source !== "robot" || !xyz || ![xyz.x, xyz.y, xyz.z].every(Number.isFinite)) {
    throw new Error("No controller XYZ position to copy.");
  }
  // Preserve controller precision for workflow authoring.
  return JSON.stringify({ x: xyz.x, y: xyz.y, z: xyz.z }, null, 2);
}

/** Tailnet panels may run over plain HTTP, where the Clipboard API is absent. */
export async function copyText(text: string): Promise<void> {
  if (window.isSecureContext && navigator.clipboard) {
    try { await navigator.clipboard.writeText(text); return; }
    catch { /* Try the user-initiated selection path if clipboard permission is denied. */ }
  }
  const previous = document.activeElement;
  const input = document.createElement("textarea");
  input.value = text;
  input.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.appendChild(input);
  input.focus(); input.select();
  let copied = false;
  try { copied = document.execCommand("copy"); }
  finally {
    input.remove();
    if (previous instanceof HTMLElement) previous.focus();
  }
  if (!copied) throw new Error("Clipboard unavailable. Select and copy the XYZ text below.");
}

/** Advisory browser check; the gateway always re-reads and checks before motion. */
export function jogLimitReason(position: PipettePosition | null, axis: "x" | "y" | "z", delta: number): string | null {
  if (!position?.coordinates || !position.coordinate_limits) return "Waiting for position and limits.";
  for (const a of ["x", "y", "z"] as const) {
    const value = position.coordinates[a] + (a === axis ? delta : 0);
    const [min, max] = position.coordinate_limits[a];
    if (!Number.isFinite(value) || value < min || value > max) {
      return `${a.toUpperCase()} target ${value.toFixed(3)} mm exceeds the configured range ${min}–${max} mm. Reduce the step or move away from the limit.`;
    }
  }
  return null;
}
