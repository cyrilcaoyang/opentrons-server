# Plan proposals by pattern — `for_each_well`

**Status:** implemented 2026-10-05 after Codex review (`gateway/plan_patterns.py`);
review changes folded in below: server-derived summary, typed whole-token
substitution with unresolved placeholders refused, overrides keyed by step id
with deep merge, single-channel only, explicit lists keep order, provenance
cleared on revision, one compile path for API / assistant / Claude / Hermes.

## Problem

An assistant proposal is the full step list, written out by the model as one
structured answer. A plate-wide plan (96 wells × aspirate, dispense, blow out,
balance read ≈ 580 steps) is ~20k output tokens: minutes per attempt, and a
cut-off answer is an invalid draft, so the attempt is refused and repeated.
Observed 2026-10-05 on Complexation: three refused `propose_plan` calls in a
row for a blow-out-per-well plan.

The same shape exists elsewhere in the lab:

- the dashboard assistant's `propose_plan` also takes a flat list and caps it
  at 256 steps (`assistant_control.MAX_PLAN_STEPS`) — such a plan is refused
  outright there;
- Bitácora's protocol compiler already solves this for formal protocols: an
  action may declare `for_each_well`, and the compiler walks the plate map in
  authored order and emits the step template once per well
  (`bitacora/app/src/bitacora/compile.py`).

## Proposal

Let a proposer send **a pattern**; the gateway expands it into the flat step
list it already knows how to validate, store, hash, approve and run. The
approved artifact is unchanged: the expanded steps, hashed as today.

```json
POST /plans
{
  "created_by": "assistant",
  "prelude":  [{"action": "pick_up_tip", "args": {"pipette": "left"}}],
  "for_each_well": {
    "labware_nickname": "2",            // the plate the wells belong to
    "wells": "A1:H12",                  // or an explicit list ["A1","B1",...]
    "order": "column",                  // column (OT-2 traversal) | row
    "steps": [
      {"action": "aspirate", "args": {"pipette": "left", "volume_ul": 100,
         "location": {"labware_nickname": "1", "position": "A1"}}},
      {"action": "dispense", "args": {"pipette": "left", "volume_ul": 100,
         "location": {"labware_nickname": "2", "position": "{well}"}}},
      {"action": "blow_out", "args": {"pipette": "left",
         "location": {"labware_nickname": "2", "position": "{well}"}}},
      {"action": "platebalance.read", "args": {}}
    ],
    "overrides": {"H12": {"aspirate": {"volume_ul": 50}}}   // optional, per well
  },
  "epilogue": [{"action": "drop_tip", "args": {"pipette": "left"}}]
}
```

- `{well}` is substituted into any string argument, at any depth; `{index}`
  (1-based) and `{column}` / `{row}` are also available.
- `overrides[well][action]` merges into that action's args for that well only
  (shallow merge on the top level of `args`).
- A plain `steps` list keeps working unchanged; `steps` and `for_each_well`
  are mutually exclusive.
- Expansion happens in `PlanStore.create`'s caller (`api.create_plan`,
  `assistant._propose`), before validation — every expanded step goes through
  the same `validated_args()` as today, so a bad volume in the template is
  refused once with the well that failed named (`step 7 (dispense, well B1):
  …`).
- The `Plan` keeps the pattern as provenance (`pattern`), shown on the card as
  a one-paragraph summary above the expanded list ("for each of 96 wells of
  plate 2, column order: aspirate 100 µL from 1/A1 → dispense → blow out →
  read balance"), so a reviewer can check the intent and spot-check the
  expansion rather than read 580 lines.
- A cap on the expanded size (`MAX_EXPANDED_STEPS`, 2000) refuses runaway
  patterns at proposal time.
- Assistant tool schema (`propose_plan`) and the Claude reply schema gain the
  same fields; the prompt tells the model to use the pattern for any
  per-well work.

## What stays the same

Approval = the hash of the expanded steps, exactly as displayed. Execution,
run records, claims and the ELN path do not change. Hermes' MCP
(`tools/ot2_agent_mcp.py`) gains the pattern form as a thin pass-through.

## Open questions for review

1. Should `wells` accept Bitácora-style plate-map references later, so an
   approved design's well list can be reused verbatim? (Out of scope now; the
   field is a list or range so that can be added.)
2. `overrides` vs. per-well lists of args: is one mechanism enough?
3. Review UX: pattern summary + collapsed expansion — is spot-checking
   acceptable for the approval gate, given the hash is still over the
   expansion?
