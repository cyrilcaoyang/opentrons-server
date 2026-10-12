# Observations for external followers

**Status: Phase 1 implemented and deployed on both robots (2026-10-11);
Phase 2 is design only.** Code: `gateway/observations.py`, `GET /observations`;
fixtures: `tests/fixtures/observations/` (§8.2); Phase 2 examples:
`docs/observations_examples/phase2_events_draft.json` (hand-written draft). Requested by the digital-twin
team (Astra). Their twin targets **HTE** (`ot2_hte`: left `p300_multi_gen2`,
right `p1000_single_gen2`); Complexation is for development and motion
acceptance (§8.1). Phase 1 is the
agreed first release; Phase 2 is a sketch for the move-event feed they prefer
for following protocol execution. Field names are provisional until aligned
with the central lab contract (`ac-organic-lab`, §9). Revised after a Codex
design review (14 findings, all addressed below).

The aim is one documented, side-effect-free view of what this gateway knows
about pipette position, tip state and run association, where every value says
where it came from, when, and whether it is still current. It must never
present a command target, an estimate, a placeholder or an old value as a
fresh measurement. When the evidence for a field is missing, the field says
`unknown`; it is never filled from a default.

## 1. Non-goals

- **No live motion stream.** The OT-2 cannot produce one (§2.2). Positions
  exist at command boundaries only. Interpolation is the client's business and
  must be labelled as such there.
- **No new robot I/O per request.** `GET /observations` serves a cache, like
  `/status`. It never moves, homes, loads an instrument, takes a claim, takes
  the command lock or waits for a protocol.
- **No extra robot reads to keep positions fresh.** Positions come only from
  commands the gateway already sends and reads it already makes.
- **No lab-world frame.** Positions are in the OT-2 deck frame. Deck-to-world
  registration, USD prims and xArm transforms stay with the twin.
- **No external-inventory reconciliation.** A plate removed by the xArm is
  invisible to the OT-2; the twin reconciles that.
- **Flex is out of scope.** Its hardware differs (it has encoders); this design
  is for the OT-2 profile.

## 2. What the hardware and transports can honestly provide

Verified 2026-10-11 against the Opentrons v8.7.0 package (the version both
robots run), robot-server v8.7.0 source on GitHub, and read-only GETs on
Complexation's robot. Robot-server facts come from its source and those GETs,
not from the package. Paths below are relative to the `opentrons` package.

1. **No encoders.** OT-2 steppers run open-loop. The Smoothie driver's own
   comment: a belt slip or stall is invisible to it
   (`drivers/smoothie_drivers/driver_3_0.py:471`). The gateway can never report
   position feedback.
2. **No in-motion samples.** The hardware API holds `_motion_lock` for each
   hardware movement, and position reads take the same lock
   (`hardware_control/api.py:715`, `move_to`). One engine command can contain
   several movements (waypoints, pickup strokes, retracts;
   `protocol_engine/execution/gantry_mover.py:313`), so a read can land between
   two movements of the same command. HTTP commands also queue behind the
   running one. Nothing here yields a trajectory.
3. **What a position read returns differs by transport.**
   - HTTP `savePosition` calls `gantry_position` without `refresh`, so it
     returns the hardware controller's *stored* position: set to each
     successful movement's target, cleared by a failed one (`api.py` `move_to`).
     It does not query the motor controller.
   - SSH `gantry_position(refresh=True)` sends `M114.2` to the Smoothie and
     returns its step-count position (`driver_3_0.py:229`).
4. **A movement command's result position is a planned waypoint, and only some
   commands end there.** The engine returns `waypoints[-1].position` of the
   command's approach move (`execution/gantry_mover.py:313`), in the critical
   point that move used. Command by command:

   | command | result position | after it |
   |---|---|---|
   | `moveToWell`, `moveToCoordinates`, `moveToAddressableArea`, `moveToAddressableAreaForDropTip` | the move's final waypoint | nothing |
   | `aspirate`, `dispense`, `blowout` | the approach move's final waypoint | plunger only (`commands/aspirate.py`, `dispense.py`, `blow_out.py`) |
   | `touchTip` | the final point after the edge touches (`commands/touch_tip.py`) | nothing |
   | `pickUpTip` | the approach position (`commands/pick_up_tip.py:133`, `:213`) | pickup strokes and a Z retract (`hardware_control/api.py:1193`) |
   | `dropTip` | the approach position (`commands/drop_tip.py:201`) | the drop motion |
   | anything else | not used | pose unknown |

   Only the first three rows give a usable endpoint ("terminal"). Pickup and
   drop leave the pose unknown until a later move or read. `pickUpTip` does
   return the effective `tipLength` it applied (`pick_up_tip.py:211`).
5. **The critical point is chosen by the engine.** Default: the configured
   primary nozzle, nozzle end without a tip and tip end with one; on GEN2
   8-channels with the full layout, the primary nozzle is A1, the back nozzle.
   After a well of `centerMultichannelOnWells` labware, `savePosition` and the
   moves into it use `Y_CENTER` (12-well reservoirs) or `XY_CENTER` (1-well
   reservoirs) (`protocol_engine/state/motion.py`, `get_pipette_location`).
   These are centres of the *configured nozzle set*
   (`hardware_control/nozzle_manager.py:405`), which is the whole column only
   for the full layout. The gateway never sends `configureNozzleLayout`, so on
   these robots the layout is always full; the design still treats any other
   layout as unknown. `moveToCoordinates` always targets the default.
6. **The two mounts are not independent.** X and Y are one gantry; a move by
   either mount moves both mounts' XY, and the hardware may retract the idle
   mount's Z before a mount switch.
7. **The robot keeps a per-run command log.** `GET /runs/{id}/commands`
   (cursor, `pageLength`, default 20) returns summaries with `id`, `key`,
   `commandType`, `intent`, `status`, `params`, `error`, and robot-clock
   `createdAt` / `startedAt` / `completedAt`. `result` needs
   `GET /runs/{id}/commands/{commandId}`. A command's status changes in place
   under the same id (`protocol_runner/legacy_command_mapper.py:127`). Reading
   the log does not affect the run; stored runs are served too. Every command
   accepts a client `key`, unique within the run. Not yet observed live during
   a protocol: every stored run on Complexation had an empty log.
8. **Older protocols are partly opaque, and their structured results lie.**
   For API levels below 2.14, `move_to` and other calls become `custom`
   commands carrying only text, and the legacy mapper writes a literal
   `(0, 0, 0)` result position into successful pickup, drop, aspirate, dispense
   and blow-out commands (`legacy_command_mapper.py:193`–`:243`). Positions
   from legacy-mapped commands are never usable; zero cannot be used to detect
   them, because zero is a legitimate coordinate.
9. **Calibration metadata is readable, but is not proof of applied geometry.**
   `GET /calibration/status`, `/calibration/pipette_offset` and
   `/calibration/tip_length` return `lastModified` and pipette serial; tip
   lengths are keyed by pipette serial and tip-rack hash, with no mount. The
   controller loads pipette offsets when it caches instruments
   (`hardware_control/api.py:470`) and tip length at pickup
   (`protocol_engine/execution/tip_handler.py:380`), with a nominal fallback
   when no calibration matches (`tip_handler.py:310`). Newer metadata on disk
   does not show what the controller is using.
10. **No robot boot identifier.** `/health` has none (`robot_serial`,
    `api_version`, `fw_version`, `system_version` only). A missing or
    non-current run shows the gateway's session was lost; it does not prove
    the robot rebooted.
11. **Two clocks.** Command timestamps are robot clock; everything else is
    gateway clock. The robot's `Date` header has 1 s resolution. §4.7 defines
    how a blocking command bounds the offset.
12. **The probe used to report defaults on partial failure.** Until
    2026-10-11 `probe_robot` kept `run_active: False` and `instruments: []`
    when `/runs` or `/instruments` failed. That is fixed for `/status` too:
    each failed read is now `null` and named in `details.robot.probe_errors`,
    each successful one is stamped `<name>_read_at`, and `instruments` keeps
    the last good list with its original read time (README *Robot
    reachability*). Observations build on that.

## 3. Phase 1: what changes in the gateway

### 3.1 Route behaviour

- `GET /observations`, same read access as `/status` (no claim; same edge
  rules). Open question in §10.
- Served from an immutable snapshot (§3.5). Never takes the command lock, so a
  response is bounded by serialization time whatever the robot is doing.
- Not a control action: no `control_action` event, no activity span, no
  `cycles_total` change.
- Proposed polling ceiling 5 Hz, to be confirmed by a concurrency test. Values
  change only at command boundaries, explicit reads and probe updates.

### 3.2 Feeds

| feed | when | robot traffic | yields |
|---|---|---|---|
| pre-send hook | before every posted HTTP command | none | motion epoch, supersession, in-flight record |
| outcome hook | after every posted HTTP command, on every path | none | terminal endpoints, effective tip length, outcome, robot timestamps |
| explicit reads | `pipette-position`, jog readbacks | as today | stored (HTTP) or step-count (SSH) positions |
| service-level motion hook | every SSH action that can move | none | supersession on the SSH transport |
| probe | existing cadence, changed to report each sub-request separately | none extra | reachability, instruments with serials, runs with ids and statuses |
| calibration read | session start, then every 10 min | 3 GETs | calibration metadata (not applied geometry) |
| ledgers | on change | none | tip accounting, deck, plate revisions |

**Hooks in `RunEngineClient.execute`.** It is the single funnel every HTTP
command passes through. Two hooks, not one:

- **Pre-send**, before any I/O: assign a gateway `move_id` (Phase 1 keeps
  it gateway-side; sending it to the robot as the command `key` waits until
  that is validated on Complexation, so a wrong field can never stop
  commands), capture plan context (plan id, step, action), and, unless the
  command is on the explicit non-motion list (§4.3), advance the motion epoch
  and mark both mounts' positions `superseded` with reason `move_in_flight`.
- **Outcome**, on every exit path: `succeeded`, `failed`, `not_completed`
  (the server-side wait expired with the command still queued or running),
  `transport_unknown` (exception after send) and `interrupted` (stop requested
  during the command). Only `succeeded` can publish an endpoint, and only for
  terminal commands. Every other outcome leaves the pose unknown, and
  `transport_unknown` or `interrupted` also leaves the motion outcome unknown.
  The hook runs after the outcome is decided and must not change it. A
  failure anywhere in observation code (these hooks, or any service-side
  observation call, each contained by `_observe_safely`) is logged and
  *degrades* observations: every position becomes `invalid`
  (`observation_degraded`), motion `motion_unknown`, and nothing is committed
  until the next session. It never replaces a command's result or exception,
  and a stop invalidates observations only after its physical stop attempt.
- **Stale outcomes:** an outcome for a command during which anything
  invalidated observations (a session change, reachability loss, an external
  run, an instrument change, degradation, a stop) is kept as `last_command`
  with `stale: true` and changes nothing else: no tip geometry, mappings,
  location, clock samples or motion state.
- **Stops:** the stop latch blocks every commit and outcome at once; while a
  stop is under way, published positions read `superseded` /
  `stop_in_progress`, and the session is invalidated after the physical stop
  attempt.

**SSH.** The REPL transport has no command funnel. At the `_run_action`
boundary, before any SSH action that can move, the service does what the HTTP
pre-send hook does: advance the shared motion epoch, mark both mounts
superseded and record the action as in flight. On completion it records the
outcome the same way (success clears `in_flight`; any failure leaves motion
`motion_unknown` until a later read). SSH positions come only from explicit
reads.

**Dry run** skips the command function entirely (`gateway/service.py:3810`), so
no hook fires. The observation state for a dry-run service is built directly:
`qualification: "dry_run"`, positions `unavailable` with reason `dry_run`, no
calibration I/O.

**Probe.** Already reports each read separately (§2.12). Phase 1 adds
instrument serials and run ids and statuses (distinguishing the gateway's own
run from others). Observations use only reads that succeeded. A mount is
reported absent only when an instruments read that succeeded omits it; a
carried-forward instrument list counts as last-known, not a fresh read.

## 4. Field rules

### 4.1 Envelope and identity

- `boot_id`: a UUID chosen at gateway process start. It scopes every sequence,
  epoch, generation and revision below.
- `session_generation`: increments whenever the gateway's control session is
  created, lost, shut down, stopped or rebuilt.
- `validity_generation`: increments whenever positions are invalidated or
  superseded by anything other than a command of our own: reachability lost,
  external run seen, instrument changed, session events.
- `observation_seq`: increments when a response's content differs from the
  previous response's, and only then (implemented as a content digest).
  Equal `boot_id` and `observation_seq` mean identical content, apart from
  `response_generated_at`.
- `qualification`: `hardware`, `simulation` or `dry_run`, at envelope level so
  every component inherits it, with `qualification_basis`: `configured` (dry
  run; SSH, whose simulation flag the gateway itself applies) or
  `configured_robot_url` (HTTP: the robot-server decides and does not say
  whether it simulates, `control/http_control.py:209`; reported `hardware`
  because the gateway is configured with a real robot's address).
- `degraded`: true with a reason when observation processing itself failed.

### 4.2 Common provenance

Every component that holds a value (position, instrument, tip, run, motion,
calibration) carries the same four fields:

- `source`: where the value came from (vocabulary per component).
- `acquired_at`: gateway clock, when the gateway obtained this value.
- `effective_at`: when the value became true, in the clock named by
  `effective_at_clock` (`robot` or `gateway`), or null when unknown. For a
  stored endpoint read now, `acquired_at` is now and `effective_at` is the
  completion of the movement that produced it, often much earlier.
- `valid` and `reason`: see §4.4.

Old values are never re-stamped. No `age` is sent; clients compute it from
`acquired_at` and `response_generated_at`, and must not read it as the age of
the physical pose.

### 4.3 Motion

- `epoch` advances at pre-send for every command not on the explicit
  non-motion list: `savePosition`, `loadPipette`, `loadLabware`, `loadModule`,
  `comment`, `waitForDuration`, temperature-module setpoint and wait commands.
  The list is short and explicit; an unlisted command counts as motion.
- `in_flight`: the submitted command, `state: "submitted"`, with `move_id`,
  `command_type`, `mount`, `submitted_at` (gateway clock), plan context, and
  the requested target as `target` with its reference point when the params
  determine one. "Submitted" is what the gateway knows; the robot's
  `startedAt` arrives with the outcome. A target never appears under
  `mounts.<mount>.position`.
- `last_command`: the latest outcome, with robot `started_at` / `completed_at`
  when the robot returned them.
- `state`: `idle`, `in_flight`, `paused`, `external_run`, `motion_unknown`
  (after `transport_unknown`, `interrupted`, `not_completed`, an unconfirmed
  stop latch, or robot unreachable). Kept separate from service health and
  activity: a service `ERROR` after a failed transport does not mean motion
  stopped.

### 4.4 Positions

Per mount: `xyz_mm`, `source`, `reference_point`, the §4.2 provenance, and the
acquisition tokens it was committed under (`session_generation`,
`validity_generation`, `epoch`, `geometry_revision`).

`source` (never `feedback`):
- `command_endpoint`: from a terminal command that `succeeded` (§2.4). It is
  the planned target of a movement the robot reports finished.
- `stored_endpoint`: an HTTP `savePosition` read (§2.3).
- `driver_step_count`: an SSH `M114.2` read.
- Never from a legacy-mapped command (§2.8). Phase 1 only sees the gateway's
  own commands, which are never legacy-mapped.

`valid` / `reason`:
- `current`: committed under the current session generation, validity
  generation, epoch and geometry revision, with nothing in flight.
- `superseded`: a later command may have moved the gantry. `reason`:
  `move_in_flight` (a motion command is outstanding), `other_mount_moved`
  (the other mount's move succeeded), `unattributed_move` (a move tied to no
  mount succeeded, such as `home`, or an SSH action), `external_run`. Stays
  visible as last-known, with the superseding `move_id`. A mount's own move
  never leaves it `superseded`: it ends `current` or `invalid`.
- `invalid`, `xyz_mm` null: `move_failed`, `pose_unknown_after_command`
  (pickup, drop, unlisted, non-success outcomes), `read_failed` (a position
  read that failed cannot confirm the earlier value), `session_lost`,
  `robot_unreachable`, `instrument_changed`, `instrument_removed`,
  `observation_degraded`. `superseded` also has `stop_in_progress`.
- `unavailable`, `xyz_mm` null: `not_acquired`, `instrument_not_loaded`,
  `instrument_absent`, `instrument_unknown`, `dry_run`.

**Commit rule.** An acquisition records the four tokens current when it
*began* (pre-send or read start) and is published only if they all still match
at commit, and not at all while the robot is unreachable or an external run is
active. A delayed response therefore cannot resurrect a position after a stop,
a session loss, a reachability loss, an external run or another command.

**Coherence.** Two positions describe the same gantry state only if they share
`boot_id`, `session_generation`, `validity_generation` and `epoch`. Different epochs imply no
simultaneity.

### 4.5 Reference point

Resolved per acquisition, from the command type, the location mirror's state
at the time, the instrument and its nozzle layout, and tip state:

- `horizontal`: `primary_nozzle` (with `nozzle`, e.g. `"A1"`),
  `nozzle_set_y_center`, `nozzle_set_xy_center`, or `unknown`.
- `vertical`: `nozzle_end`, `tip_end`, or `unknown`.
- `to_primary_nozzle_mm`: the vector with
  `primary_xyz = reported_xyz + to_primary_nozzle_mm`, given only when the
  layout is known to be full and the centring is known. Otherwise null, and
  conversion is not allowed.

`RunEngineClient`'s location mirror is deliberately conservative the other
way: it may keep an old well where the engine has moved on, because that errs
toward refusing a jog. Observations therefore track the engine's current
location themselves, strictly: it is certain only after an outcome that
settles it (a succeeded well-addressed command, or a succeeded `home` /
coordinate / addressable-area move), and any motion command that fails, loses
its reply or has an unlisted effect makes it uncertain. A read taken while it
is uncertain gets `horizontal: unknown`. A command endpoint takes its
reference from the command's own target labware.

### 4.6 Instruments, tips and geometry

- **Instrument**: model, serial and channels from the probe's instruments
  response (source `robot_instruments`), plus `loaded_in_session`. A change of
  serial on a mount invalidates its position (`instrument_changed`).
- **Tip**: two sources, both labelled, neither sensing. The gateway ledger
  (`service_accounting`) and the engine's belief from `currentState`
  (`run_engine_state`). They can disagree, and both are published.
- **Tip length**: the effective `tipLength` from the `pickUpTip` result
  (source `engine_pickup_result`), with the robot run id and command id, the
  instrument serial, and the tip rack's definition URI and a SHA-256 of its
  loaded definition (the engine selects calibrated length by definition, §2.9). Null when the tip was not picked up by a command the
  gateway observed.
- **`geometry_revision`**: increments when the effective geometry known to the
  gateway changes: a pickup or drop (tip length), an instrument change, a
  session change. Positions committed under an older revision become
  `invalid` with `geometry_changed` only when that change affects their mount.
- **Calibration metadata**: published separately, as last read
  (`calibration.read_at`, per-item `lastModified`, read outcome). It does not
  claim to describe the geometry a sample used. A change in it is a reason to
  re-establish geometry (a fresh session), not an automatic invalidation, and
  a 10-minute read cannot detect a change sooner than that.

### 4.7 Clocks

`offset = robot_time − gateway_time`. For a newly submitted command that the
robot reports `succeeded`, with gateway send and receive times `g_s`, `g_r`
and robot start and completion times `r_s`, `r_c`:

```
r_c − g_r ≤ offset ≤ r_s − g_s
```

Published as `robot_offset_s: {min, max, width, samples, from_command,
measured_at, resolution_s, drift_ppm}`. `width = max − min` is the uncertainty: the true
offset lies in `[min, max]`, with no preferred point inside it. `measured_at`
(gateway clock) is when the latest contributing command completed; `samples`
is how many commands contributed. The interval is the intersection of the bounds from the commands of the last 10 minutes within one
session generation (at most 50), widened by the robot's timestamp resolution
and by 50 ppm drift for each sample's age *at `measured_at`*. The interval
holds at `measured_at`; a client widens it by `drift_ppm × 1e-6 × age` itself,
so repeated reads of unchanged state stay identical. It expires when the
session generation changes or when bounds become inconsistent (a clock jump).
No midpoint is published as if latency were symmetric. Gateway-side durations
use the monotonic clock.

### 4.8 Inventory and run

- **Revisions** on the tip ledger, mounted tips, deck state and loaded plate:
  opaque content digests, so any change they summarise changes them, including deck changes
  observed from the run. `/status` carries the same revisions
  (`details.revisions`), so a client that fetches `/status` after seeing a new
  revision can check it got that revision or later, and retry if not.
- **Run**: plan context captured at submission time (`plans.py` sets progress
  before invoking a step, `plans.py:708`); the gateway's run id; other runs
  from the probe (`external_run_ids`). `external_run_active` is `null` when the
  probe's runs request failed. An external run found by the probe mid-session
  supersedes positions and blocks commits, the same as one found at boot.

### 4.9 Null semantics

A mount is `null` only when a successful instruments response omits it. If
that response failed or has not arrived, the mount is present with
`instrument.valid: unknown`. A present instrument with nothing known has a
position with `valid: unavailable`. Every other null means "not known", never
zero or default.

## 5. Concurrency

- Feeds compute their update outside any observation lock, including any
  metadata that needs robot I/O.
- The cache holds one immutable snapshot. An update takes a short cache lock,
  checks the commit tokens, swaps in a new snapshot and increments
  `observation_seq`. A reader copies the reference under the lock and
  serializes outside it.
- Lock order: observation code never acquires `_command_lock`, `_state_lock`
  or `_stop_lock`, and never calls a transport method. Hooks run on the
  thread that holds those locks but take only the cache lock, which is never
  held while waiting on anything else.
- Deterministic tests cover a stop during a command, a probe flipping
  reachability during a command, and a response arriving after invalidation.

## 6. Restarts and invalidation

| event | effect |
|---|---|
| gateway restart | new `boot_id`; everything `unavailable`; counters restart |
| session created, lost, shut down, stopped, rebuilt | `session_generation` +1; positions `invalid` (`session_lost`) |
| robot unreachable | positions `invalid` (`robot_unreachable`); motion `motion_unknown` |
| external run seen | positions `superseded` (`external_run`) |
| instrument serial changes on a mount | that mount `invalid` (`instrument_changed`); tip geometry unknown |
| instrument absent from a fresh instruments read | that mount `invalid` (`instrument_removed`); reattaching never revives the old value |
| tip pickup or drop | that mount's pose unknown; a failed or unconfirmed one also makes its vertical reference and tip length unknown |
| robot unreachable, external run, degraded | tip geometry unknown on both mounts as well |
| calibration metadata changes | published; positions unaffected until the session is re-established |

Values return only through a new terminal command or read.

## 7. Phase 2 sketch: move events

Astra prefers move events, with snapshots for initial state and recovery.

- `GET /observations/events?after=<event_seq>&limit=<n>` over a bounded
  in-memory ring of immutable transition events (proposed 5000), scoped by
  `boot_id`. Each response carries `oldest_seq`, `latest_seq` and the
  `observation_seq` it is consistent with, so a client can take a snapshot and
  then read events from that watermark without gaps. A client that falls
  behind the ring, or sees a new `boot_id`, re-syncs from `/observations`.
- **Events are transitions, not one record per move.** Each has a unique
  `event_seq`, a stable `move_id`, and one of `submitted`, `started`,
  `completed`, `failed`, `cancelled`, `outcome_unknown`. Fields per Astra: mount,
  command type, target and reference point, robot-clock `started_at` /
  `completed_at`, gateway-clock `observed_at`, and a `command_endpoint`
  when §2.4 says the command has a terminal one. No extra position read per
  move.
- **Gateway-driven moves** come from the two hooks. `started` is emitted only
  from the robot's `startedAt` (in the outcome); before that the gateway only
  knows `submitted`.
- **External runs** (protocols started from the Opentrons App):
  - Discovery and lifecycle are separate. A cursor walks the run's command log
    for new commands; every command not yet terminal is re-read until it is,
    because status changes in place (§2.7). A terminal move's full record is
    read once for its endpoint.
  - When a run ends, its log is drained to the end. Runs that started and
    ended between two probes are discovered from the runs list and drained
    too.
  - Commands from legacy-mapped protocols (API level below 2.14, from the run's
    protocol metadata) never carry endpoints; `custom` commands become events
    with `kind: "legacy_text"`, `target: null` and the text.
- **Coverage is command level.** Waypoints, pickup strokes and retracts inside
  one command are not separate logged commands, and anything run outside the
  run engine is invisible.

### 7.1 Reading the robot's log

Per tracked run the gateway keeps a discovery cursor (index of the next
unseen command) and the set of commands not yet terminal. Each poll it lists
from the lowest of the two, so commands seen earlier as `queued` or `running`
are re-read until they reach `succeeded` or `failed` (the engine has only
`queued`, `running`, `succeeded`, `failed`; a command cut short by a stop ends
`failed` with `RunStoppedError`, published as state `cancelled`); status
changes under an existing id are captured, not only new ids. A transition is
emitted only when a command's state differs from the last one recorded for
it, which also makes the robot-side reads idempotent.

- **Run ends:** the log is drained to the end and every remaining
  non-terminal command gets one final read.
- **Robot log gap:** if a tracked run's log shrinks, its cursor runs past
  `totalLength`, or the run disappears (robot restart, run pruned), the gateway
  emits one `log_gap` event naming the run and the last index read, and every
  command not yet terminal gets `outcome_unknown`. Nothing is inferred about
  what happened in the gap.
- **Gateway restart during an external run:** new `boot_id`; the gateway
  re-walks that run's log from index 0 and emits each command's current state
  once, marked `backfill: true` with its robot timestamps.

### 7.2 Client cursor, recovery and deduplication

- The cursor is `(boot_id, event_seq)`. `event_seq` is strictly increasing
  within a `boot_id` and never reused.
- Each response carries `boot_id`, `oldest_seq`, `latest_seq`, and `gap: true`
  when `after < oldest_seq − 1` (events were dropped from the ring).
- `/observations` carries `event_watermark`: the latest `event_seq` already
  reflected in that snapshot.
- **Recovery:** on a different `boot_id` or `gap: true`, fetch `/observations`
  and continue from its `event_watermark`. Events at or below the watermark
  are already in the snapshot.
- **Deduplication:** an event's identity is `(boot_id, event_seq)`; a
  transition's identity is `(move_id, state)`. A client that re-reads
  overlapping pages drops repeats by either. `backfill` events after a restart
  repeat transitions the client may have seen under the old `boot_id`; it
  matches them by robot `command_id`.
- **Delivery delay:** events for external runs lag by the poll interval, about
  1 s, pending Complexation validation. Gateway-driven moves are emitted as
  they complete.

Open before committing to Phase 2: confirm the log fills during a real
protocol on Complexation (§2.7), and measure the load of polling it at about
1 Hz during a run.

## 8. Implementation, fixtures and tests (Phase 1)

- `gateway/observations.py`: snapshot model, the cache (§5), token checks, and
  the per-command interpretation table (§2.4) as data, versioned against the
  Opentrons release.
- `RunEngineClient`: pre-send and outcome hooks; command `key`s. The location
  mirror stays where it is and is read at pre-send.
- `OT2Service`: wires the hooks, the SSH motion hook, explicit reads, the probe
  change, calibration reads on the probe thread, ledger revisions, dry-run
  initialization, and `details.revisions` in `/status`.
- `models.py`: response models, so `/openapi.json` documents the schema.
- `api.py`: the route, no claim dependency.

Fixtures (`tests/fixtures/observations_*.json`), generated from the cache in
mocked states and validated against the response model. Every fixture exists
in two instrument configurations: **HTE** (left `p300_multi_gen2`, right
`p1000_single_gen2`, the twin's target) and **Complexation** (left
`p300_single_gen2`, right `p20_multi_gen2`, where motion acceptance runs). The
8-channel is on opposite mounts, so the pair also checks that nothing assumes
a mount. Cases: idle with nothing acquired; after a terminal move (moving mount current, other superseded);
command in flight; after a pickup (pose invalid, tip length known); paused
plan; robot unreachable; probe sub-request failed (instrument and run state
unknown); unhomed refusal; with and without tip; reservoir-centred
multi-channel with conversion vector; reference point changing from primary
nozzle to reservoir centre and back on the same mount; mirror unconfirmed
(`horizontal: unknown`); a shared-gantry update (a move on one mount
superseding the other, and both current at one epoch after two reads); a
failed move; a move cancelled by a software stop (`interrupted`, motion
unknown); external run active; dry run; after gateway restart (new `boot_id`,
everything unavailable).

Phase 2 event fixtures, provided early for review against the same two
configurations: a protocol move's `started` then `completed` under one
`move_id`; a `failed` command and one `cancelled` by a stop; a legacy `custom` command
(`target: null`, `kind: "legacy_text"`); a pickup with no terminal endpoint; a
`log_gap`; a ring `gap: true` response; restart `backfill`.

Unit tests, all offline: the route makes no robot request and takes no
service lock; repeated reads keep `observation_seq`; no value is re-stamped;
an unreachable or failed-probe robot never yields coordinates or a null
mount; pickup and drop never publish an endpoint; a legacy-mapped result with
`(0, 0, 0)` and with a nonzero position is never published; a calibration
metadata change is published without invalidating positions, and a session
rebuild after it does invalidate them; SSH read, move, read across both mounts
gives different epochs; a failed observation hook
leaves the command result untouched; the commit rule drops stale
acquisitions under the three races in §5 and after a reachability loss or
external run with a delayed successful command; equal epochs only within one
session generation; the clock bounds follow §4.7.

### 8.1 Validation on each robot

- **Complexation**, operator-run motion acceptance: poll during idle, jog,
  plan, pause and error; confirm no claim changes and no extra commands in the
  run log; run the 5 Hz polling ceiling during a plan. Its right `p20_multi`
  exercises the 8-channel reference-point rules.
- **HTE**, passive only, never interrupting campaigns: no control actions, no
  claims, no jogs. Once deployed there, the twin reads `/observations` during
  normal campaigns and checks HTE's reference points, calibration metadata and
  mappings against what it sees. The only robot traffic the feature adds is
  the calibration-metadata read (3 GETs, session start then every 10 min),
  agreed for both robots on 2026-10-11; a per-gateway setting turns it off.

### 8.2 Fixture files

Generated by `tests/unit/test_observations.py` through the real hooks (a fake
robot behind `RunEngineClient._request`), one file per configuration and case,
each a sequence of `{after, observation}` steps; regenerate with
`OBSERVATION_FIXTURES_REGEN=1`. Serials in them are placeholders. Settings:
`OT2_CALIBRATION_READ=0` turns the calibration read off;
`OT2_CALIBRATION_READ_INTERVAL` (seconds, default 600) sets its cadence.

## 9. Lab contract alignment

The lab's event convention is devices pushing domain events to the central
history DB via `POST /api/ingest/events` (`ac-organic-lab`
`docs/ARCHITECTURE.md`, `docs/LAB_MONITORING.md`). A device-local, cursor-based
move feed is new. Proposed split: move events stay device-local (too frequent
for `lab.db`, and only useful live); session edges and failures keep going to
the history DB as now. To agree with the `ac-organic-lab` owners before field
names are frozen, along with whether `/observations` should become a lab-wide
convention (an xArm follower would want the same shape).

## 10. Open questions

1. Read access: same as `/status`, or require an identity?
2. Is publishing the in-flight target in Phase 1 wanted? The design keeps it
   in `motion.in_flight` only.
3. Calibration read cadence (proposed: session start, then every 10 min).
4. Ring size and retention for Phase 2.
5. Resolved 2026-10-11: the probe's default-on-failure behaviour is fixed in
   `/status` too (§2.12).
6. Resolved 2026-10-11: the calibration-metadata read runs on both robots.

## 11. Coordinate limits (answer to the request)

`coordinate_limits` on `/control/pipette-position` is the gateway's check on
requested *destinations*, in the same critical-point frame, not a workspace:
X 0–446.75 and Y 0–347.5 from Opentrons' nominal OT-2 deck extents, Z 0–218 a
local configurable cap. Readings outside it, such as at home, are legitimate
and never clamped. Observations do not carry these limits. See
`docs/MANUAL_PIPETTE.md`.
