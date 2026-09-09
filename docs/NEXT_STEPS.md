# Next Steps — OT-2 gateway work tracker

## Package review — 2026-09-09

- [x] Fix claim ownership/release, assistant caller authorization, per-step
  claim checks, plan cancellation/concurrency, and legacy command state gates.
- [x] Validate gateway references and quote SSH setup data; update assistant
  robot guidance, provider configuration diagnostics and client cleanup.
- [x] Verify 624 offline unit tests and UI typecheck; packaged UI build included.
- [ ] Resolve the legacy SSH manual-labware relocation workflow. Its gripper
  alias is invalid for OT-2; HTTP relocation already records manual moves.
- [ ] Update the central SDK/dashboard using the
  [server handoff](DASHBOARD_UPDATE_PROMPT.md). This gateway commit is not a
  deployment to either robot.

See [review findings and limits](PACKAGE_REVIEW.md).

## Implementation — 2026-09-08

- [x] Expose shared OT-2 pipetting/module operations with typed HTTP routes,
  matching proposal schemas, state/claim gates and agent documentation.
- [x] Add independent HTTP software-stop API/button, confirmation readback,
  persistent recovery latch and late-completion protection.
- [x] Add an opt-in Flex profile, alphabetic deck and full 1/8-channel support,
  managed gripper moves with offsets and native manual gripper commands.
- [x] Preserve direct versus arced motion explicitly, including gripper XY
  motion without a Z-retract waypoint.
- [ ] Operator-planned physical acceptance, including stopping an in-flight
  setup command and observing module states. Develop/test with fakes only;
  OT-2 acceptance is Complexation only. No deployment performed.
- [ ] Migrate central `lab-skills` action definitions and sample-prep callers
  in their own repositories after reviewing the proposed external changes.
- [ ] Future features: 96-channel/partial-layout tracking, moving tracked tip
  racks, sensing/liquid classes, complex transfer compilation, relative pipette
  helpers, uploaded protocol lifecycle and optional Flex modules. See
  [coverage](HTTP_API_COVERAGE.md) and [Flex mapping](FLEX_HTTP_SUPPORT.md).

## Earlier work and history

The sections below retain the dated project history. Original snapshot:
2026-07-19 (the `develop-http-drive` tracker; that
branch merged to `main`, and the 2026-07-18/19 items land on
`feature-http-ssh-parity`). Tracks the remaining work on the HTTP run-engine
transport and the complexation bring-up. Companion to `HTTP_TRANSPORT.md`
and `DECK_STATE.md` (the feature docs that absorbed the retired
`HTTP_DRIVE_PLAN.md` / `DECK_STATE_PLAN.md`), `HTTP_SSH_PARITY.md`,
`HTTP_DRIVE_VALIDATION.md`, and `DEVICE_BRINGUP.md`.

## Done (on this branch)

- Opt-in HTTP run-engine transport (`OT2_TRANSPORT=http`), SSH path unchanged.
- `execute()` raises `CommandNotCompleted` (OSError) on a wait-timeout →
  non-idempotent actions land in `unknown_outcome`, not a false success.
- Per-call `flow_rate` (µL/s) on `LiquidMoveRequest`, threaded through both
  control backends (HTTP falls back to `OT2_HTTP_*_FLOW_UL_S`).
- HTTP deck-snapshot parity via `_last_run_labware` → `normalize_run_slots`.
- Test-isolation fix for the repo-root-anchored state stores.
- Complexation kit: `demo/complexation_dispense_test.py`,
  `deploy/ot2_complexation.env.example`, `docs/DEVICE_BRINGUP.md`.
- Full offline suite green (`178 passed`); `--mode plan` validated offline.

**HTTP transport VALIDATED on real hardware (2026-07-14, `ot2cytation`)** — full
cycle incl. step 12 plate-out, idle-persistence, custom labware, and transport-loss
→ `unknown_outcome`; one live bug found + fixed (off-deck `/status` 500, `ea58a5b`).
Only wait-timeout not force-triggered (shared `unknown_outcome` path + unit-tested).
See `HTTP_DRIVE_VALIDATION.md`. Complexation bring-up still not run.

## At the machine — remaining physical-access work

1. **HTTP-drive validation — DONE** (functional). Optional leftovers: force the
   wait-timeout box live (tiny `OT2_HTTP_COMMAND_TIMEOUT` + slow move), and a wet
   custom-labware run (today's custom-labware test was bookkeeping-only). Deploy
   note: set `OT2_HTTP_BASE_URL` to the robot's reachable **tailnet IP** (bare host
   alias didn't reach `:31950`); on the host use Git Bash + `python`, or SSH into WSL
   and use the tailnet name + `/mnt/c` paths.
2. **Complexation gateway bring-up — DONE** (2026-07-14). Gateway was already
   deployed on :8021 (`ot2_complexation` → `ot2training`); dispense test
   `plan → dry → wet` all passed (34 steps, wet = real motion, no unplanned
   stop); `equipment.yaml` already `adapter: http`. Flushed out 3 bugs, all
   fixed: off-deck `/status` 500 (`ea58a5b`), demo-script cp1252 encoding +
   30→180 s timeout (`f28cc87`), `tip_length`-on-non-tiprack SSH snapshot 500
   (`68c0803`). Optional follow-up: tune the p300 volumes so A12 doesn't deplete
   (source-refill), for a liquid-accurate — not just motion — run.

3. **HTE tip counts — DONE** (2026-08-07). Both racks on `ot2_hte` were
   reconciled against the physical deck after the restart-and-declare work:
   slot 7's 1000 µL filter rack confirmed full by eye (its `96/96` had come
   from auto-registration alone — no pick/drop events, no reset — so it was an
   assumption until checked), and slot 8 reset to `96/96` by the operator after
   swapping in a fresh 300 µL rack. No leftover work; the general rule this
   produced is in [`DECK_STATE.md`](DECK_STATE.md) ("Registration asserts a
   *full* rack").

## Desk work — unblocked (can do remotely now)

- ✅ **Aspirate flow default lowered** 150 → 90 µL/s (`e0566bf`). Per-call override
  and dispense/blow-out unchanged.
- ◐ **`drop_tip` → trash — mechanism done** (`e0566bf`): `/control/drop-tip` now
  honors an explicit `labware_nickname`+`position`, so HTTP drops into a named
  loaded trash; no location → `dropTipInPlace` (SSH already auto-trashes).
  **Update (2026-07-18, `feature-http-ssh-parity`):** `OT2HttpControl.load_trash_bin()`
  now registers the OT-2 fixed trash (slot 12) and tokenless `drop_tip` defaults to
  it when registered (still bench-unverified). **Update (2026-08-11):**
  `setup_protocol` now calls `load_trash_bin()` automatically when the recipe
  leaves slot 12 free — nobody ever called it, so live drops still landed in
  place (observed on the bench 2026-08-11: the operator had to home first).
  **Left:** verify drop-to-trash on the bench (Complexation).
- ✅ **Full SSH↔HTTP control parity** (2026-07-18, branch `feature-http-ssh-parity`):
  `OT2HttpControl` now mirrors the entire `OT2Control` method surface — protocol
  controls (comment/delay/lights), absolute-coordinate liquid handling
  (`moveToCoordinates` + `*InPlace`), emulated `mix`/`air_gap`/`return_tip`,
  module verbs (heater-shaker, tempmod, magmod, thermocycler) with live
  `GET /modules` readbacks, definition-derived well geometry, and explicit
  `NotImplementedError`s for REPL-only methods. Method-by-method table +
  bench-verification status: [`HTTP_SSH_PARITY.md`](HTTP_SSH_PARITY.md).
- ✅ **Multi-channel addressing** — no code change needed: the complexation test
  already uses row-A column addressing for the p20 multi; the `A1→B1` hazard was
  runbook-only and is fixed there. (New finding from the 2026-07-14 run.)
- ✅ **`blow_out` endpoint + flow rate.** `/control/blow-out` and the matching
  plan action are implemented; `set_flow_rate` configures blow-out flow.
- ✅ **OT-2 protocol-action SkillDefs** — shipped in `ac-organic-lab`
  2026-07-12 (16 SkillDefs with typed args); `move_to` added 2026-07-18
  alongside the gateway's `POST /control/move-to` (18 SkillDefs total).
- ◐ **`loadModule` adapter support — done** (2026-07-18, parity work):
  `OT2HttpControl.load_module` loads a configured adapter onto the module via
  `loadLabware` and registers it as `<nickname>_adapter`. **Left:**
  modules-before-labware ordering stays a caller responsibility (labware on a
  module needs the module loaded first); bench-unverified like the rest of
  the parity surface.
- **Test-script enhancements** (`demo/complexation_dispense_test.py`):
  source-refill handling for the wet run (the p300 depletes column 12),
  CLI/JSON-driven volume maps instead of hard-coded `PIPETTES`, and an optional
  per-call `flow_rate`.
- **Low-pri cleanup:** `tests/test_ssh_methods.py` / `tests/test_states.py` emit
  `PytestReturnNotNoneWarning` (tests `return` instead of `assert`).

## Housekeeping

- ✅ `develop-http-drive` merged to `main` (PR #1). Current work rides
  `feature-http-ssh-parity` (full SSH parity, `/control/move-to`,
  transport-honest `/status`, docs consolidation).
- Plan docs retired into feature docs once complete: `HTTP_DRIVE_PLAN.md` →
  `HTTP_TRANSPORT.md` (2026-07-18), `DECK_STATE_PLAN.md` → `DECK_STATE.md`
  (2026-07-19). Apply the same treatment to future plan docs.
- The `lab-status-contract` shared-package extraction and MCP milestone (v0.4)
  are `ac-organic-lab` concerns, tracked in that repo's `ROADMAP.md` — not here.
