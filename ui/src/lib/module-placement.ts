import type { DeckDeclareValue } from "./api";
import type { DeviceDeck, RobotModule } from "./types";
import { computeModuleFootprints, moduleFamily, pairModuleSlots } from "./ot2-deck";

export function unassignedModules(deck: DeviceDeck | null, modules: RobotModule[]): RobotModule[] {
  const assigned = new Set([...pairModuleSlots(deck, modules).values()].map(m => m.live));
  return modules.filter(m => !assigned.has(m));
}

/** Preserve complete operator declarations during the full-layout replacement. */
export function declarationPayload(deck: DeviceDeck): Record<string, DeckDeclareValue> {
  const result: Record<string, DeckDeclareValue> = {};
  for (const [slot, entry] of Object.entries(deck.slots)) {
    const module = entry.declared_module ?? (entry.source === "declared" ? entry.module : null);
    const labware = entry.declared ?? (entry.source === "declared" ? entry.labware : null);
    if (module) result[slot] = { ...module };
    else if (labware) result[slot] = labware.load_name ? { ...labware } : labware.kind;
  }
  return result;
}

export function modulePlacementIssue(deck: DeviceDeck, from: string | null, to: string | null, name: string): string | null {
  if (from) {
    const source = deck.slots[from];
    if (!source?.module) return "The module assignment changed. Refresh before editing it.";
    if (source.source !== "declared") return "This module is loaded in the robot session. End or update that session before changing its slot.";
    if (source.labware || source.declared) return "Clear the labware on this module before changing its slot.";
  }
  if (!to || to === from) return null;
  if (to === "12" || !deck.slots[to]) return "Choose an available deck slot.";
  const family = moduleFamily(name);
  if (family === "thermocycler" && to !== "7") return "The OT-2 thermocycler must be assigned to slot 7.";
  const targets = family === "thermocycler" ? ["7", "8", "10", "11"] : [to];
  const footprints = computeModuleFootprints(deck);
  for (const target of targets) {
    const occupant = deck.slots[target];
    const footprint = footprints.get(Number(target));
    if (target !== from && (occupant?.slot_state !== "empty" || occupant?.declared || occupant?.declared_module ||
      (footprint && String(footprint.anchorSlot) !== from))) return `Slot ${target} is occupied. Clear it before assigning a module.`;
  }
  return null;
}

export function placeModule(deck: DeviceDeck, from: string | null, to: string | null,
  module: { module_name: string; serial_number?: string | null }): Record<string, DeckDeclareValue> {
  const issue = modulePlacementIssue(deck, from, to, module.module_name);
  if (issue) throw new Error(issue);
  const result = declarationPayload(deck);
  if (from) delete result[from];
  if (to) result[to] = { ...module };
  return result;
}
