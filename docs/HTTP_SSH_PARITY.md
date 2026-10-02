# HTTP ↔ SSH Transport Parity — `OT2HttpControl` vs `OT2Control`

**Status:** implemented 2026-07-18 (branch `feature-http-ssh-parity`); offline
test coverage in `tests/unit/test_http_control_parity.py`. The parity surface
added on this branch is **bench-unverified** — the 2026-07-14 hardware
validation (`HTTP_DRIVE_VALIDATION.md`) covered the original core cycle only.
**Scope:** the control layer (`control/http_control.py` vs
`control/ot2_control.py`). The gateway REST surface is transport-agnostic and
unchanged.

`OT2HttpControl` now mirrors the **full public method surface** of the SSH
REPL transport, so `gateway/service.py` — and any direct Python caller — can
treat the two transports interchangeably. Where the run engine has no
equivalent, the method raises `NotImplementedError` with the reason instead of
silently diverging.

## Parity classes

| Class | Meaning |
|---|---|
| **native** | One (or two) run-engine commands with the same effect. |
| **emulated** | Composed client-side from run-engine primitives; same observable behavior, different command trace. |
| **client-tracked** | Protocol-API *session state* the run engine does not hold; kept inside the adapter. Readbacks reflect what this adapter did, **not** independently-observed hardware state. |
| **unsupported** | REPL-only or no run-engine equivalent; raises `NotImplementedError`. |

## Method-by-method table

### Session lifecycle

| `OT2Control` method | HTTP status | Mechanism / notes |
|---|---|---|
| `initialize_protocol(simulation)` | native | `POST /runs` (protocol-less run). `simulation` is decided by the target robot-server, accepted and ignored. |
| `shutdown()` | native | best-effort `stop` action + close session. |
| `close_session()` | native | `home` then `shutdown()` — mirrors SSH (home before disconnect). |
| `invoke(code)` | **unsupported** | Executes Python in the robot REPL; the run engine only accepts typed commands. |

### Setup / loading

| Method | HTTP status | Mechanism / notes |
|---|---|---|
| `setup_protocol(labware, instruments, modules)` | native | Same ordering as SSH (labware → instruments → modules). Labware that sits **on** a module still needs the module loaded first — pass modules in an earlier `setup_protocol` call or use a dict location `{"moduleId": ...}`. |
| `load_labware` / `_load_custom_labware` | native | Custom definitions registered via `POST .../labware_definitions` first. Built-ins assume `namespace="opentrons"`, `version=1` unless the config carries `namespace`/`version`. |
| `load_instrument` | native | `loadPipette`; mount recorded for `home_pipette`/`home_plunger`. Custom instruments: `NotImplementedError` on both transports. |
| `load_module` | native | `loadModule` (+ `loadLabware` of the adapter onto the module when the config carries `adapter`, registered as `<nickname>_adapter`). Hardware serial captured from the command result for live readbacks. |
| `load_trash_bin(nickname, location)` | native (re-purposed) | On SSH this is Flex-only. Over HTTP it loads the OT-2 fixed-trash labware (`opentrons_1_trash_1100ml_fixed`, default slot 12) and registers it as the default `drop_tip` target. **Bench-unverified.** |
| `remove_labware` | emulated | `moveLabware` to `offDeck` (the engine's way to clear a slot). The id stays registered so the labware can move back on-deck. |

### Protocol-level controls

| Method | HTTP status | Mechanism / notes |
|---|---|---|
| `home()` | native | `home` (all axes). |
| `comment(message)` | native | `comment`. |
| `delay(seconds, minutes)` | native | `waitForDuration`. |
| `set_rail_lights(on)` / `get_rail_lights()` | native | `POST/GET /robot/lights` (robot-level endpoint, not a run command). Same endpoint the gateway's `/control/lights` uses. |
| `set_max_speed(axis, v)` / `clear_max_speed(axis)` | **unsupported** | `protocol.max_speeds` has no run-engine equivalent. Per-move speed is available via `move_to_pip(speed=...)` / `set_speed`. |
| `pause()` / `resume()` | no-op (by design) | In the never-played setup model each command already blocks to completion; there is no run queue to pause. See `HTTP_TRANSPORT.md`. |

### Locations & motion

| Method | HTTP status | Mechanism / notes |
|---|---|---|
| `get_location_from_labware(labware, well, top/bottom/center)` | native | Stashes a pending **well** location (same precedence: top > bottom > center > top(0)). |
| `get_location_absolute(x, y, z, reference)` | native | Stashes a pending **coordinate** location. `reference` is label-only on both transports. |
| `move_to_pip(pip, speed, force_direct, minimum_z_height)` | native | `moveToWell` / `moveToCoordinates`. **Peeks** (does not consume) the pending location so move-then-aspirate targets the same spot, like the REPL's persistent `location` variable. |

**Pending-location semantics (deliberate difference):** the SSH REPL's
`location` variable persists until overwritten; over HTTP every *consuming*
action (aspirate, dispense, blow_out, mix, pick_up_tip, drop_tip) takes the
pending location and clears it. Issue `get_location_*` before each consuming
call. Only `move_to_pip` peeks.

### Tips

| Method | HTTP status | Mechanism / notes |
|---|---|---|
| `pick_up_tip(pip)` | native | `pickUpTip` at the pending well (explicit location required — no protocol-API next-tip tracking; the gateway's tip store auto-picks above this layer). `presses`/`increment`/`prep_after` → `NotImplementedError` (no engine params; deprecated in the protocol API too). |
| `drop_tip(pip, home_after)` | native + default | Precedence: pending location > registered trash labware > registered trash **area** (`moveToAddressableAreaForDropTip` + `dropTipInPlace`, the protocol-API ≥ 2.16 sequence) > `dropTipInPlace`. SSH auto-routes to the fixed trash; registration happens when the run is created — adopting a preloaded trash labware when the server provides one, else the `fixedTrash` addressable area (modern servers reserve slot 12; loading there raises `AreaNotInDeckConfigurationError`). `home_after` threaded through. |
| `return_tip(pip, home_after)` | emulated | No `returnTip` command; drops into the client-tracked pick-up origin well. Raises if no pick went through this adapter. |
| `has_tip(pip)` | client-tracked | True between pick and drop/return **through this adapter**; blind to other clients and to state before a gateway restart. SSH reads the live protocol object. |
| `set_starting_tip` / `reset_tipracks` | **unsupported** | Protocol-API tip tracking doesn't exist in the run engine. Equivalent functionality lives in the gateway tip store (`gateway/tip_state.py`, `POST /control/tips/reset`). |

### Liquid handling

| Method | HTTP status | Mechanism / notes |
|---|---|---|
| `aspirate(pip, vol, rate, flow_rate)` | native / emulated | Well pending → `aspirate`. Coordinate pending → `moveToCoordinates` + `aspirateInPlace` (**bench-unverified**; may need `prepare_aspirate` first if the plunger isn't prepared). |
| `dispense(pip, vol, push_out, rate, flow_rate)` | native / emulated | Same split; `pushOut` supported in both forms. |
| `blow_out(pip)` | native / emulated | Well → `blowout`; coordinates → move + `blowOutInPlace`. |
| `blow_out_in_place(pip)` | native | `blowOutInPlace`. |
| `prepare_aspirate(pip)` | native | `prepareToAspirate`. |
| `mix(pip, reps, volume, rate)` | emulated | N × (`aspirate` + `dispense`) at the pending well. **`volume` is required** (the engine can't default to the pipette's max volume). |
| `air_gap(pip, volume, height)` | emulated | `moveToWell` at `top + height` (default 5 mm, protocol-API default) above the **last-touched well**, then `aspirateInPlace`. Requires a preceding well op on that pipette. |
| `touch_tip(pip, labware, well, radius, v_offset, speed)` | native | `touchTip`; `v_offset` maps to `wellLocation {origin: top, offset.z}`. |

**Flow-rate model:** the run engine *requires* `flowRate` on every liquid
command; the SSH path inherits protocol-API defaults. Effective rate per call:

Qualified plate-balance dispenses use the same raised schema-2 labware
definition on both transports. The gateway issues an arced well move with a
minimum Z above the measured rim before dispensing. HTTP receives a capped
absolute `flowRate`; SSH receives a per-command `rate` multiplier computed from
the current pipette flow rate, so ordinary subsequent dispenses retain their
previous setting. Both paths cap the balance dispense at half the documented
single-channel GEN2 default. This path is offline-tested and awaits
Complexation operator acceptance.

```
explicit flow_rate  >  set_flow_rate(pip, ...) override  >  constructor/env or model default
                                        × rate multiplier (default 1.0)
```

HTTP aspirate and blow-out defaults remain 90 and 100 µL/s. If
`OT2_HTTP_DISPENSE_FLOW_UL_S` is unset, ordinary HTTP dispense uses the official
OT-2 GEN2 model rate: P20 single 7.56, P300 single 92.86, P1000 single 274.7,
P20 multi 7.6, and P300 multi 94 µL/s (Protocol API ≥2.6). Unknown models
require an explicit flow rate. An explicit deployment override still applies
to every pipette. Flex retains its existing HTTP fallback; this table is OT-2
only. Source: [Opentrons pipette characteristics](https://docs.opentrons.com/python-api/pipettes/characteristics/#ot-2-pipette-flow-rates).

### Pipette configuration & readbacks

| Method | HTTP status | Mechanism / notes |
|---|---|---|
| `set_flow_rate` / `get_flow_rate` | client-tracked | Per-pipette overrides consulted whenever a call omits `flow_rate` — functional parity with `pipette.flow_rate.*`. |
| `set_speed(pip, v)` | client-tracked (partial) | Applies to `move_to_pip` only; the engine doesn't take a speed on aspirate/dispense's implicit moves (SSH's `default_speed` affects all moves). |
| `set_well_bottom_clearance` / `get_well_bottom_clearance` | client-tracked (inert) | Stored for readback parity only. Inert **on both transports**: every aspirate/dispense through these wrappers passes an explicit location, and the protocol API only applies clearances to bare-well targets. |
| `current_volume(pip)` | client-tracked | Ledger: aspirates/air-gaps add, dispenses subtract, blow-outs zero. SSH reads the live plunger state. |
| `home_pipette(pip)` | native | `home` with the mount's axes (`rightZ`+`rightPlunger` etc.). |
| `home_plunger(pip)` | native | `home` with the mount's plunger axis. |

### Labware geometry readbacks

| Method | HTTP status | Mechanism / notes |
|---|---|---|
| `well_diameter(labware, well)` | native (definition-derived) | From `GET /runs/{id}/loaded_labware_definitions`, matched by the run's `definitionUri`. Raises for rectangular wells (they define `xDimension`/`yDimension`), same as the SSH readback would. |
| `well_depth(labware, well)` | native (definition-derived) | Same source. |
| `tip_length(labware, well)` | native (definition-derived) | `parameters.tipLength` for tip racks, `None` otherwise (mirrors SSH's `well.length` behavior). Per-rack uniform. |

### Labware movement

| Method | HTTP status | Mechanism / notes |
|---|---|---|
| `move_labware(labware, new_location)` | native | `moveLabware` with `manualMoveWithoutPause` (records the move; the OT-2 has no gripper). |
| `move_labware_w_gripper(...)` | alias | Same as `move_labware` — the SSH implementation's `use_gripper=True` is wrong for an OT-2 anyway. |

### Heater-shaker module

| Method | HTTP status | Mechanism |
|---|---|---|
| `hs_latch_open` / `hs_latch_close` | native | `heaterShaker/openLabwareLatch` / `closeLabwareLatch` |
| `hs_set_and_wait_shake_speed` | native | `heaterShaker/setAndWaitForShakeSpeed` |
| `hs_deactivate_shaker` / `hs_deactivate_heater` | native | matching commands |
| `hs_set_target_temperature` / `hs_wait_for_temperature` | native | matching commands |
| `hs_set_and_wait_temperature` | native (2 cmds) | set target + wait |
| `hs_deactivate` | emulated | deactivateShaker + deactivateHeater (no combined command; the SSH `.deactivate()` call isn't a real protocol-API method either) |
| `set_rpm` / `set_temp` | native | same 200–3000 rpm / 27–95 °C bands as SSH; out-of-band deactivates |
| `get_rpm` / `get_temp` | native (live) | `GET /modules`, matched by the hardware serial captured at `loadModule`. Genuinely live hardware readback, not client state. |

### Temperature / magnetic / thermocycler modules

| Method | HTTP status | Mechanism |
|---|---|---|
| `tempmod_start_set_temperature` | native | `temperatureModule/setTargetTemperature` only (no wait) — gateway operator/assistant path |
| `tempmod_set_temperature` | native (2 cmds) | `temperatureModule/setTargetTemperature` + `waitForTemperature` (SSH blocks too) |
| `tempmod_await_temperature` | native | `waitForTemperature` |
| `tempmod_deactivate` | native | `deactivate` |
| `magmod_engage(height_from_base)` | native | `magneticModule/engage {height}`. `offset`-from-default → `NotImplementedError`; omitting the height → `ValueError` (the engine can't default to the labware's engage height). |
| `magmod_disengage` | native | `disengage` |
| `thermocycler_open_lid` / `close_lid` | native | `thermocycler/openLid` / `closeLid` |
| `thermocycler_set_block_temperature` | native (2 cmds) | `setTargetBlockTemperature {celsius, holdTimeSeconds?, blockMaxVolumeUl?}` + `waitForBlockTemperature`. `ramp_rate` → `NotImplementedError` (no engine param). |
| `thermocycler_set_lid_temperature` | native (2 cmds) | set + wait |
| `thermocycler_deactivate_block` / `_lid` | native | matching commands |
| `thermocycler_deactivate` | emulated | deactivateBlock + deactivateLid |
| `thermocycler_open/close_labware_latch` | **unsupported** | Thermocyclers have no labware latch; the SSH method would fail on-robot too. |

## What the gateway itself calls

`gateway/service.py` uses only: `initialize_protocol`, `setup_protocol`,
`get_location_from_labware`, `get_location_absolute` + `move_to_pip` (the
`POST /control/move-to` endpoint), `aspirate`, `dispense`, `pick_up_tip`,
`drop_tip`, `move_labware`, `home`, `pause`, `resume`, `run_snapshot`,
`shutdown`, and (SSH only) `invoke` for the REPL deck snapshot. Everything
beyond that subset serves direct-Python callers (demos, workflow repos using
`OT2Control` today) so they can switch transports without code changes.

### `/status` snapshot shapes across transports

`details.snapshot.deck` is fully normalized (both transports feed the same
`build_deck`), and the raw sibling fields have **container-shape parity**:
`pipettes` is a dict keyed by mount, `labwares`/`modules` dicts keyed by deck
slot (engine-id fallback for off-deck or colliding entries), matching the SSH
`state_readers.get_all_states` shape. The per-item **value schemas still
differ by transport** — SSH publishes protocol-API readbacks (`has_tip`,
volumes, well geometry), HTTP publishes raw run-engine entries
(`definitionUri`, `loadName`, …) plus a top-level `run_id` (HTTP-only).
Consumers should treat the values as transport-specific and prefer
`snapshot.deck` / `components.*` / `details.robot`, which are
transport-neutral.

The `components.ssh` key predates the HTTP transport and means "control
backend session" (renaming would break dashboards, STATUS_SPEC #14); its
`message` names the actual transport (`control via HTTP run engine (no SSH
session)` / `control via SSH REPL` / `dry run (no robot connection)`).

### REST exposure of absolute moves

`POST /control/move-to` (added 2026-07-18) exposes pipette motion through the
gateway: well-addressed (`location`) or absolute deck coordinates
(`coordinates`, mm), optional `speed` / `force_direct` / `minimum_z_height`.
It is advertised as `move_to` in `allowed_actions` when `ready`, claim-gated
like every `/control/*` action, and classified **idempotent** (transport loss
→ `error`, not `unknown_outcome` — re-issuing the same move is safe). It works
on both transports: SSH via `Location(Point(x,y,z))` + `pipette.move_to`,
HTTP via `moveToWell` / `moveToCoordinates`.

## Bench-verification status

Validated on real hardware 2026-07-14 (see `HTTP_DRIVE_VALIDATION.md`): the
core cycle (startup → home → setup → pick-up → aspirate → dispense → drop-tip
→ move-labware), custom labware, idle persistence, transport-loss →
`unknown_outcome`.

**Not yet bench-verified** (added on this branch, offline-tested only):

- fixed-trash auto-registration + drop-to-trash default (`load_trash_bin`)
- absolute-coordinate liquid handling (`moveToCoordinates` + `*InPlace`) —
  watch for plunger-not-prepared refusals; `prepare_aspirate` is exposed
- `touch_tip`, `mix`, `air_gap`, `return_tip`
- module verbs (no module was attached during the 2026-07-14 run)
- geometry readbacks (`loaded_labware_definitions` endpoint shape)
- rail-light readback through `RunEngineClient` (the gateway's own
  `/control/lights` path is already verified)

## See also

- [`TRANSPORT_TRADEOFFS.md`](TRANSPORT_TRADEOFFS.md) — pros and cons of the
  two transports and the current default/recommendation.
- [`HTTP_TRANSPORT.md`](HTTP_TRANSPORT.md) — why the run engine, the
  never-played setup-run model, enable/revert switches.
- [`HTTP_DRIVE_VALIDATION.md`](HTTP_DRIVE_VALIDATION.md) — 2026-07-14 hardware
  validation record.
- [`NEXT_STEPS.md`](NEXT_STEPS.md) — remaining transport work.
- `tests/unit/test_http_control_parity.py` — offline coverage for this table.
