export type Corner = "nw" | "ne" | "sw" | "se";

export interface WindowRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

const MARGIN = 16;

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

export function moveWindow(rect: WindowRect, dx: number, dy: number,
                           viewportWidth: number, viewportHeight: number): WindowRect {
  const width = rect.right - rect.left;
  const height = rect.bottom - rect.top;
  const left = clamp(rect.left + dx, MARGIN, Math.max(MARGIN, viewportWidth - MARGIN - width));
  const top = clamp(rect.top + dy, MARGIN, Math.max(MARGIN, viewportHeight - MARGIN - height));
  return { left, top, right: left + width, bottom: top + height };
}

export function resizeWindow(rect: WindowRect, corner: Corner, dx: number, dy: number,
                             viewportWidth: number, viewportHeight: number): WindowRect {
  const minWidth = Math.min(320, viewportWidth - 2 * MARGIN);
  const minHeight = Math.min(360, viewportHeight - 2 * MARGIN);
  const west = corner.endsWith("w");
  const north = corner.startsWith("n");
  const left = west
    ? clamp(rect.left + dx, MARGIN, rect.right - minWidth)
    : rect.left;
  const right = west
    ? rect.right
    : clamp(rect.right + dx, rect.left + minWidth, viewportWidth - MARGIN);
  const top = north
    ? clamp(rect.top + dy, MARGIN, rect.bottom - minHeight)
    : rect.top;
  const bottom = north
    ? rect.bottom
    : clamp(rect.bottom + dy, rect.top + minHeight, viewportHeight - MARGIN);
  return { left, top, right, bottom };
}
