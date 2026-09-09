import { useMemo, useState } from "react";

import {
  catalogEntryForDeclare,
  groupedCatalog,
  type CatalogEntry,
} from "../lib/ot2-catalog";

/**
 * Declare-intent picker (searchable, grouped, exact load names). Ported from
 * the dashboard's Ot2ControlPanel; the auth lock is replaced by the claim
 * gate — controls unlock when this browser session holds the device claim.
 *
 * A slot holding a declaration cannot be re-declared in place: the catalog and
 * the free-text field go inert and only **Clear slot** stays live, so replacing
 * a declaration is always two deliberate acts. `ControlPanel.declare` enforces
 * the same rule on the request itself.
 */
export function DeclarePicker({
  selectedSlot,
  currentDeclare,
  locked,
  onDeclare,
  customEntries = [],
}: {
  selectedSlot: number | string | null;
  /** The declare string currently held by the selected slot (or null). */
  currentDeclare: string | null;
  locked: boolean;
  onDeclare: (entry: CatalogEntry | null) => void;
  /** Runtime entries (GET /labware standard summaries), merged as a group. */
  customEntries?: CatalogEntry[];
}) {
  const [query, setQuery] = useState("");
  const [freeText, setFreeText] = useState("");
  const groups = useMemo(() => groupedCatalog(query, customEntries), [query, customEntries]);
  const disabled = locked || selectedSlot == null;
  const currentEntry = catalogEntryForDeclare(currentDeclare, customEntries);
  // A slot that already declares something is not re-declared in place:
  // overwriting on a single click is how a slot gets changed by accident, and
  // the gateway auto-loads labware from the declaration, so a wrong one reaches
  // the robot. Clearing is the deliberate first half of a replacement.
  const held = currentDeclare != null;
  const declareDisabled = disabled || held;
  // The gateway parses a bare declare string as a load_name only when it
  // contains "_" — anything else would be misread as a legacy kind.
  const freeTextValid = freeText.includes("_") && /^[a-z0-9._]+$/.test(freeText);

  function declareFreeText() {
    if (declareDisabled || !freeTextValid) return;
    onDeclare({
      key: `freetext-${freeText}`,
      label: freeText,
      category: "custom",
      declare: freeText,
    });
    setFreeText("");
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search plates, tip racks, modules…"
          aria-label="Search the labware catalog"
          className="min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-ink placeholder:text-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
        />
        {selectedSlot != null ? (
          <span className="text-xs text-ink-subtle dark:text-slate-400">
            → slot {selectedSlot}
            {currentDeclare && (
              <>
                {" "}
                · currently <span className="font-mono">{currentDeclare}</span>
              </>
            )}
          </span>
        ) : (
          <span className="text-xs text-ink-subtle dark:text-slate-500">Select a deck slot first</span>
        )}
      </div>

      {locked && (
        <p className="text-xs text-amber-700 dark:text-amber-400">
          Take control (claim the device) to declare deck intent.
        </p>
      )}

      {held && !locked && (
        <p className="rounded bg-slate-50 px-2 py-1 text-xs text-ink-subtle dark:bg-slate-800/60 dark:text-slate-400">
          Slot {selectedSlot} declares <span className="font-mono">{currentDeclare}</span>. Use{" "}
          <strong className="font-semibold">Clear slot</strong> below before declaring something
          else — replacing it in one click is too easy to do by accident.
        </p>
      )}

      <div className="max-h-64 overflow-y-auto rounded-md border border-slate-200 dark:border-slate-800">
        {groups.length === 0 && (
          <p className="p-3 text-xs text-ink-subtle dark:text-slate-500">
            No catalog match for “{query}”.
          </p>
        )}
        {groups.map((g) => (
          <div key={g.category}>
            <div className="sticky top-0 bg-slate-50 px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-ink-subtle dark:bg-slate-800 dark:text-slate-400">
              {g.label}
            </div>
            <ul>
              {g.entries.map((e) => {
                const active = currentEntry?.key === e.key;
                return (
                  <li key={e.key}>
                    <button
                      type="button"
                      disabled={declareDisabled}
                      onClick={() => onDeclare(e)}
                      title={
                        held
                          ? `Slot ${selectedSlot} declares ${currentDeclare} — clear it first`
                          : `Declares ${e.declare}${e.compat ? ` — ${e.compat}` : ""}`
                      }
                      className={[
                        "flex w-full items-baseline justify-between gap-3 px-2 py-1.5 text-left text-xs transition-colors",
                        active
                          ? "bg-sky-50 text-sky-900 dark:bg-sky-950/40 dark:text-sky-200"
                          : "text-ink hover:bg-slate-50 dark:text-slate-200 dark:hover:bg-slate-800/60",
                        declareDisabled ? "cursor-not-allowed opacity-50" : "",
                      ].join(" ")}
                    >
                      <span className="min-w-0 truncate">{e.label}</span>
                      <span className="shrink-0 font-mono text-[10px] text-ink-subtle dark:text-slate-500">
                        {e.declare}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>

      {/* Free-text declare: any exact Opentrons load_name — including one not
          (yet) in any catalog. Must contain "_" or the gateway would parse it
          as a legacy kind string. */}
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="text"
          value={freeText}
          onChange={(e) => setFreeText(e.target.value.trim())}
          onKeyDown={(e) => {
            if (e.key === "Enter") declareFreeText();
          }}
          disabled={declareDisabled}
          placeholder="…or type an exact load_name (e.g. matterlab_54_vialplate_2ml)"
          aria-label="Declare a custom load name"
          className="min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-2 py-1 font-mono text-xs text-ink placeholder:font-sans placeholder:text-slate-400 disabled:bg-slate-50 disabled:text-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100 dark:disabled:bg-slate-900"
        />
        <button
          type="button"
          disabled={declareDisabled || !freeTextValid}
          onClick={declareFreeText}
          title={
            held
              ? `Slot ${selectedSlot} declares ${currentDeclare} — clear it first`
              : freeText && !freeTextValid
                ? "Load names are lowercase letters/digits/dot/underscore and must contain an underscore"
                : "Declare this exact load name on the selected slot"
          }
          className="rounded-md border border-slate-300 px-2 py-1 text-xs text-ink hover:border-slate-400 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:text-slate-200 dark:hover:border-slate-500"
        >
          Declare custom
        </button>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={disabled || !held}
          onClick={() => onDeclare(null)}
          title={held ? `Clears the declaration on slot ${selectedSlot}` : undefined}
          className={[
            "rounded-md border px-2 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50",
            // Accented while a slot holds a declaration: it is the only way
            // forward to a different one, so it should read as the next step.
            held
              ? "border-sky-500 font-semibold text-sky-700 hover:bg-sky-50 dark:border-sky-500 dark:text-sky-300 dark:hover:bg-sky-950/40"
              : "border-slate-300 text-ink hover:border-slate-400 dark:border-slate-700 dark:text-slate-200 dark:hover:border-slate-500",
          ].join(" ")}
        >
          Clear slot
        </button>
        <p className="text-[10px] leading-tight text-ink-subtle dark:text-slate-500">
          Declaring records operator intent only — it does not load labware on the robot or run
          protocol setup.
        </p>
      </div>
    </div>
  );
}
