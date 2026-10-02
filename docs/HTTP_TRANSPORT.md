# HTTP Run-Engine Transport — feature documentation

**Status:** shipped, and what both robots run — `ot2_complexation` since
bring-up, `ot2_hte` since 2026-08-07 (SSH is the fallback; it remains the code
default for an unset `OT2_TRANSPORT`). Hardware-validated 2026-07-14 on
`ot2cytation` (full
cycle incl. plate-out handoff, idle-persistence, custom labware,
transport-loss → `unknown_outcome` — see
[`HTTP_DRIVE_VALIDATION.md`](HTTP_DRIVE_VALIDATION.md)), and brought to full
`OT2Control` method parity 2026-07-18
([`HTTP_SSH_PARITY.md`](HTTP_SSH_PARITY.md)).

> This document replaces the retired **`HTTP_DRIVE_PLAN.md`** (the migration
> plan, removed 2026-07-18 once its tasks completed; full history in git). It
> keeps the plan's durable final-state material: the driving model, the
> confirmed v8.7.0 command reference, and the plate-handoff safety spec.

**Scope:** OT-2 gateway only. Flex is explicitly out of scope: its
absolute-motion/gripper surface has no run-engine equivalent (the run engine
is well/labware-level only).

---

## In Plain Language

How an operator experiences all of this, from safest to most powerful:

**1. Looking (no sign-in needed).** Open the dashboard. The OT-2 tile shows a
status pill (ready / needs init / busy), a picture of the 12 deck slots and
what's in them, whether the deck lights are on, which pipettes are mounted,
and whether the gateway can talk to the robot. It updates itself every few
seconds — you never refresh anything.

**2. Declaring (sign-in required).** Click "Control interface →" on the tile
to reach the OT-2's full page. There you can do exactly two kinds of things:

- **Tell the system what's on the deck.** Pick a slot, choose a labware (or
  type any Opentrons load name, or build a custom one in the labware
  builder), save. This is *declaring intent* — writing on a whiteboard.
  **Nothing moves.** If the robot later *observes* something different in
  that slot, the page shows both and flags the disagreement with a ≠ badge.
- **Toggle the deck lights** — the one physical control allowed from the
  browser, because it can't hurt anything.

Every click goes through the central server's audited control path: it checks
who you are, briefly claims the robot so nothing else can talk to it
mid-action, does the thing, releases the claim, and writes a log line of who
did what.

**3. Actually moving the robot — deliberately *not* in the UI.** Picking up
tips, aspirating, dispensing, moving to a position — the browser cannot do
these. They only run through the Python SDK (`lab-skills`) inside a validated
plan: the plan is checked against safety rules first, each step re-checks the
robot's live state right before it runs, and each step holds a claim. This is
a design decision, not a gap: a mouse click shouldn't be able to crash a
pipette into a plate.

**How everything gets visualized.** The picture on the screen is the end of a
bucket brigade:

1. **The robot** knows its own truth (deck, pipettes, run state).
2. **This gateway** asks the robot — over SSH today, or the HTTP mode this
   document describes — and packages everything into one standard status
   report, merging in the whiteboard declarations and its tip-usage
   bookkeeping.
3. **The central server** polls every gateway a few times a minute and keeps
   the latest report for each device.
4. **The browser** polls the central server every 2.5 seconds and redraws.

The dashboard never talks to the robot directly, and the robot never pushes
anything — state flows up the chain and the page repaints. With the HTTP
transport, the deck picture survives a gateway restart, because the robot
itself remembers it (that durability is the main reason this transport
exists).

**Where this is heading: the workflow UI.** Today, running an experiment
means a chemist writes a small Python plan — "pick up tip, aspirate from A1,
dispense to B1, …" — and the SDK validates and executes it with all the
safety checks. The dashboard's reserved `/platforms/{name}` pages will close
the visibility gap: see a plan and its validation verdict before it runs,
press go on *pre-approved* plans, watch the current step highlight live while
the OT-2 tile shows "busy, held by workflow X", and review the same story in
History afterwards. The plumbing (plans, validation, claims, executor, audit)
already exists; the workflow UI is presentation.

One sentence to keep: **the tile shows you the robot, the control page lets
you describe and illuminate the deck, and workflows — today from Python,
eventually from the workflow UI — are the only thing that makes it move.**

---

## Why this transport exists

The SSH transport drives the robot by holding an interactive Python REPL
(`OT2Control` → `execute.get_protocol_api('2.21')`) and issuing string-built
Python. That works, but:

- the deck picture lives only in the live `protocol` object → an idle robot
  shows a blank deck; a crash loses it;
- the robot's own run records don't know the gateway exists (hence the
  `external_control` state and the `/runs` boot probe);
- it can't coexist with the `opentrons-ot2` connector, which owns the serial
  port.

The OT-2 run engine (robot-server HTTP API on `:31950`) fixes all three: the
run holds deck state **on the robot**, survives idle/restart, is the robot's
own record, and is served by both the stock robot-server *and* the
`opentrons-ot2` connector. Full pros/cons:
[`TRANSPORT_TRADEOFFS.md`](TRANSPORT_TRADEOFFS.md).

## The driving model

**SSH "session" → run engine "run", never played.** Opening a session becomes
creating a run (`POST /runs`, **no `play` ever issued**); each imperative
command is one `intent: "setup"` command POSTed to that run with
`waitUntilComplete=true`; closing the session discards the run (best-effort
`stop`). Deck reads come from `GET /runs/{runId}` instead of the REPL
snapshot.

Why never played (confirmed from v8.7.0 source): `setup`-intent commands
execute immediately, with priority, while the run is in SETUP — but only
until a `play` moves the run to RUNNING, after which every further setup
command is rejected (`SetupCommandNotAllowedError`). So the imperative
one-command-at-a-time model requires the run to stay in SETUP forever.
`/maintenance_runs` behaves the same but is ephemeral and unrecorded — a
persisted `/runs` run is what keeps the deck picture alive when idle, so
that's the path.

Consequences:

- `/control/pause`/`resume` send nothing to the robot on this transport — each
  command already blocks to completion; there is no queue to pause. The gateway
  pauses itself at the next command boundary instead: an in-flight command
  finishes, the gateway becomes `PAUSED` as it ends (`details.pause_requested`
  meanwhile), and a plan executing through the gateway waits at its next step
  until `resume`. Nothing here interrupts motion; that is `stop`.
- `moveLabware` may only use `strategy: manualMoveWithoutPause`.
  `manualMoveWithPause` dispatches a run-level pause that (a) hangs the
  blocking POST until something issues `play` and (b) once played, ends
  imperative driving permanently. `usingGripper` is Flex-only. The client
  rejects both.
- A leftover `current` run (e.g. after a crash) makes the *SSH* gateway stand
  off in `external_control` until the run is deleted — the two transports
  interfere on the robot even though the code paths are separate.
- **An http gateway used to stand off against its own run.** Because the
  session run is created and *never played*, it sits in `status: idle` with
  `startedAt: null` for its whole life — indistinguishable, to the `/runs`
  boot probe, from a run the Opentrons app just created. So a restart while a
  previous session's run was still `current` came up `busy`
  ("Robot has an active run (external / official app); gateway is standing
  off"), and could not leave: `_maybe_resume_from_external_control` waits for
  `run_active` to go false, which the gateway's own leftover run prevented.
  Fixed by `_run_counts_as_active` in `service.py`, which excludes a
  never-started run (`idle` + no `startedAt`) — it has executed nothing, so it
  is not evidence anyone is driving the robot. A genuinely external session
  still counts the moment it starts. Observed live on `ot2_complexation`,
  2026-08-05, where it presented as "the tile says busy and taking control
  takes forever" (the escape path runs `startup()`, minutes of SSH +
  protocol-API init).

Implementation: `control/http_run.py` (`RunEngineClient` + typed
`RunEngineCommands` builders, error mapping to `CommandFailed` /
`CommandNotCompleted` / `RunEngineUnreachable`) and `control/http_control.py`
(`OT2HttpControl`, the full `OT2Control`-parity adapter). `OT2Service`
branches on transport only at control construction and snapshot refresh.

## Enabling / reverting (opt-in, fully reversible)

- **Enable:** set `OT2_TRANSPORT=http` in the service env, plus
  `OT2_HTTP_BASE_URL` — use the robot's reachable **tailnet IP** (a bare host
  alias was observed not to reach `:31950` from the gateway host). Restart.
- **Revert:** unset `OT2_TRANSPORT` (or set `ssh`) and restart. No code
  change — the SSH REPL path is unchanged.
- Related env knobs: `OT2_HTTP_ASPIRATE_FLOW_UL_S` (default 90),
  `OT2_HTTP_DISPENSE_FLOW_UL_S` (optional override; otherwise the
  official OT-2 GEN2 rate for the loaded model), `OT2_HTTP_BLOWOUT_FLOW_UL_S` (100),
  `OT2_HTTP_COMMAND_TIMEOUT` (120 s per blocking command),
  `OT2_HTTP_TIMEOUT` (10 s control-plane calls), `OT2_OPENTRONS_VERSION`
  (`Opentrons-Version` header, default 3).
- The run engine **requires** `flowRate` on every liquid command — there is
  no protocol-API default on this transport. Precedence per call: explicit
  `flow_rate` > `set_flow_rate()` per-pipette override > env override or model default.
  The model default applies to ordinary OT-2 HTTP dispenses, including existing
  non-balance workflows; review that change before deploying either gateway.

**Production stance (2026-07-18):** SSH remains the default. The original
migration surface is hardware-validated; the parity surface added on
`feature-http-ssh-parity` (trash default, absolute-coordinate liquid
handling, module verbs, geometry readbacks) is offline-tested only — see the
bench-unverified list in `HTTP_SSH_PARITY.md`.

## Plate handoff spec (safety-relevant, in force)

The gateway records every plate arrival/departure in the run engine so
`GET /runs/{runId}` is the single truthful deck picture, and mirrors sample
metadata in `PlateStateStore`. **Deck occupancy** is owned by the run engine;
**well/sample metadata** stays in the store.

Two facts drive the ordering:

1. **No auto-retraction.** Manual moves make no hardware motion; whatever
   pose the last command left the gantry in persists. **The gateway MUST
   `home` before any arm/hand enters the deck.**
2. `moveLabware(manualMoveWithoutPause)` is bookkeeping-only — its timing is
   not safety-critical; only home-before-entry is.

**Plate-OUT** (xArm lifts a plate off slot N) — cross-device sequencing is
the orchestrator's job (interlock layer 4); the gateway exposes primitives
and truthful state:

1. Gateway idle (no command in flight).
2. Gateway `home` → gantry parked clear. Safety-critical: must complete
   before the arm enters.
3. Orchestrator signals the xArm to enter and grasp.
4. xArm lifts the plate clear.
5. Gateway `moveLabware {labwareId, newLocation: "offDeck",
   strategy: manualMoveWithoutPause}` — issued *after* the lift so the engine
   never claims empty while the plate is physically present.
6. Gateway clears the plate from `PlateStateStore`.

Steps 5+6 are the atomic `/control/plate/unload` (a robot action under the
BUSY state machine). **Plate-IN** mirrors it: home → arm places → gateway
records — `loadLabware` for a new plate (custom defs registered first via
`POST /runs/{runId}/labware_definitions`, idempotent), or `moveLabware` back
from `offDeck` for a returning plate (never `loadLabware` twice).

Edge cases: unload with no such labwareId → skip the move, clear the store,
log a reconcile warning (idempotent). `moveLabware` failed → surface via
`last_error`, do **not** clear the store. `loadLabware` onto an occupied slot
→ engine errors; reconcile against `GET /runs/{runId}`, don't blindly retry.

The nickname → labwareId map is per-run and reconstructable after a gateway
restart from `GET /runs/{runId}` — a crash mid-residency doesn't orphan it.

## Confirmed HTTP surface (Opentrons v8.7.0, from source)

Every request/response uses the `{"data": {...}}` envelope and carries the
`Opentrons-Version` header.

| Purpose | Call |
|---|---|
| Create a run (empty, live-driven) | `POST /runs` body `{"data": {}}` → `data.id` |
| Issue one command, block for result | `POST /runs/{runId}/commands?waitUntilComplete=true[&timeout=<ms>]` — timeout timer starts at enqueue; on timeout the command is returned in-progress (NOT cancelled) → surfaced as `CommandNotCompleted` |
| Read run + loaded labware/modules/pipettes | `GET /runs/{runId}` |
| Loaded labware definitions (geometry readbacks) | `GET /runs/{runId}/loaded_labware_definitions` |
| Register a custom labware definition | `POST /runs/{runId}/labware_definitions` (idempotent per namespace/loadName/version) |
| Stop a run (shutdown, best-effort) | `POST /runs/{runId}/actions` `{"actionType": "stop"}` |
| Health / external-run probe | `GET /health`, `GET /runs`, `GET /instruments` |
| Deck lights | `GET`/`POST /robot/lights` (no `data` envelope) |
| Live module telemetry | `GET /modules` (serial-matched by the control adapter) |

### Command reference (as used by `RunEngineCommands`)

Common types: `flowRate` µL/s; `volume` µL; `offset {x,y,z}` mm; OT-2 deck
slot = `{"slotName": "1".."12"}`; off-deck = the bare string `"offDeck"`;
well `origin` enum = `top|bottom|center|meniscus` (aspirate/dispense),
`top|bottom|center` (pickUpTip/moveToWell), `top|bottom|center|default`
(dropTip).

| commandType | required params | optional |
|---|---|---|
| `loadPipette` | `pipetteName`, `mount` | `pipetteId` |
| `loadLabware` | `location`, `loadName`, `namespace`, `version` (int) | `labwareId`, `displayName` |
| `loadModule` | `model`, `location` | `moduleId` |
| `pickUpTip` / `dropTip` | `pipetteId`, `labwareId`, `wellName` | `wellLocation`, `homeAfter` (drop) |
| `dropTipInPlace` | `pipetteId` | `homeAfter` |
| `moveToAddressableAreaForDropTip` | `pipetteId`, `addressableAreaName` (`fixedTrash` on the OT-2) | `alternateDropLocation`, `offset` |
| `aspirate` / `dispense` | `pipetteId`, `labwareId`, `wellName`, `volume`, `flowRate`, `wellLocation` | `pushOut` (dispense) |
| `aspirateInPlace` / `dispenseInPlace` | `pipetteId`, `volume`, `flowRate` | `pushOut` (dispense) |
| `blowout` / `blowOutInPlace` | `pipetteId`, `flowRate` (+ well for `blowout`) | — |
| `touchTip` | `pipetteId`, `labwareId`, `wellName` | `radius`, `speed`, `wellLocation` |
| `moveToWell` / `moveToCoordinates` | `pipetteId` + well / `coordinates {x,y,z}` | `speed`, `forceDirect`, `minimumZHeight` |
| `prepareToAspirate` | `pipetteId` | — |
| `moveLabware` | `labwareId`, `newLocation`, `strategy` (`manualMoveWithoutPause` only) | — |
| `home` | — (omit `axes` for all; OT-2 axes: `x`,`y`,`leftZ`,`rightZ`,`leftPlunger`,`rightPlunger`) | `axes` |
| `comment` | `message` | — |
| `waitForDuration` | `seconds` | — |
| module verbs | `heaterShaker/*`, `temperatureModule/*`, `magneticModule/*`, `thermocycler/*` — see `RunEngineCommands` | — |

The full SSH-method ↔ HTTP-command mapping, including what is emulated
client-side and what is unsupported, lives in
[`HTTP_SSH_PARITY.md`](HTTP_SSH_PARITY.md).

## See also

- [`HTTP_SSH_PARITY.md`](HTTP_SSH_PARITY.md) — method-by-method parity table
  + bench-verification status + `/status` snapshot shapes across transports.
- [`TRANSPORT_TRADEOFFS.md`](TRANSPORT_TRADEOFFS.md) — pros/cons and the
  current default/recommendation.
- [`HTTP_DRIVE_VALIDATION.md`](HTTP_DRIVE_VALIDATION.md) — the 2026-07-14
  hardware validation record.
- [`NEXT_STEPS.md`](NEXT_STEPS.md) — remaining work tracker.
- [`DECK_STATE.md`](DECK_STATE.md) — the deck-state layer this transport completes.
