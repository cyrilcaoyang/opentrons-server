import type { ReactNode } from "react";

/**
 * Shared button vocabulary, ported from the dashboard's TileButton so the
 * panel keeps its exact look. Two sizes (default 28 px, small 20 px), four
 * variants (default neutral, primary emerald, danger rose, stop solid red).
 */

export type TileButtonSize = "default" | "small";
export type TileButtonVariant = "default" | "primary" | "danger" | "stop";

const SIZE_CLASSES: Record<TileButtonSize, string> = {
  default: "h-7 px-2.5 text-xs",
  small: "h-5 px-1.5 text-[11px]",
};

const VARIANT_CLASSES: Record<TileButtonVariant, string> = {
  default:
    "border-slate-200 bg-white text-ink-muted hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700",
  primary:
    "border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100 dark:border-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-200 dark:hover:bg-emerald-900/60",
  danger:
    "border-rose-300 bg-rose-50 text-rose-700 hover:bg-rose-100 dark:border-rose-800 dark:bg-rose-950/40 dark:text-rose-300 dark:hover:bg-rose-900/60",
  stop:
    "border-red-700 bg-red-600 text-white hover:bg-red-700 dark:border-red-500 dark:bg-red-600 dark:text-white dark:hover:bg-red-700",
};

export interface TileButtonProps {
  onClick?: () => void;
  disabled?: boolean;
  type?: "button" | "submit";
  size?: TileButtonSize;
  variant?: TileButtonVariant;
  ariaLabel?: string;
  title?: string;
  children: ReactNode;
}

export function TileButton({
  onClick,
  disabled = false,
  type = "button",
  size = "default",
  variant = "default",
  ariaLabel,
  title,
  children,
}: TileButtonProps) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      title={title}
      className={[
        "inline-flex shrink-0 items-center justify-center rounded-md border font-semibold transition-colors",
        "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500",
        "disabled:cursor-not-allowed disabled:opacity-40",
        SIZE_CLASSES[size],
        VARIANT_CLASSES[variant],
      ].join(" ")}
    >
      {children}
    </button>
  );
}
