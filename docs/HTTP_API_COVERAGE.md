# Python API operations through HTTP

The gateway already talks to the robot's run engine over HTTP when
`OT2_TRANSPORT=http`. The gateway does not need SSH to send commands that the
run engine supports. Python objects such as wells are represented by loaded
labware IDs and well names; the robot keeps the geometry.

## Implemented gateway surface

The exact request schemas are served by `/openapi.json`; `/plans/actions` and
`/docs/agent` describe what an agent may propose. An operator still approves
and executes proposed steps. Every new control endpoint retains the claim gate.
An available schema does not mean a robot is ready: check `/status` and its
`allowed_actions` as well.

| Operation | Gateway behavior |
|---|---|
| Blow out | `/control/blow-out`: explicit well `location`, or `in_place: true`, exclusively |
| Dispense push-out | Optional `push_out` in microliters on `/control/dispense` |
| Touch tip | `/control/touch-tip`: pipette, loaded labware, well, radius, vertical offset and speed |
| Mix | `/control/mix`: well, explicit volume, repetitions and rate; HTTP repeats aspirate/dispense |
| Air gap | `/control/air-gap`: explicit well and height above its top; checks capacity including held volume when known |
| Pipette controls | `prepare-aspirate`, `home-pipette`, `home-plunger`, `set-flow-rate`, `set-speed` under `/control/` |
| Heater-shaker | Latch, shake speed, target temperature, waits and deactivation; names match the Python methods with hyphens |
| Temperature module | Existing target/deactivate routes, plus `/control/tempmod-await-temperature` |
| Magnetic module | `/control/magmod-engage` with explicit height from base, and `/control/magmod-disengage`; OT-2 only |
| Thermocycler | Lid open/close, block/lid temperatures and deactivation; no fictitious thermocycler labware latch |
| Protocol utilities | `/control/comment` and `/control/delay` |
| Software stop | `/control/stop`, immediate operator action; excluded from proposal steps |

Tip selection, rack lifecycle and contamination tracking retain their existing
implementation. Touch-tip and mixing record contact through that tracker.
Returning a tip still uses the existing explicit drop destination and tracking.
Flow rates are in microliters/second; move speeds are in millimeters/second.
HTTP `set_speed` affects explicit moves, while implicit liquid-command moves
retain robot defaults. Module waits use the configured command timeout; a
wait that has not completed is not reported as success.

## Residual liquid after dispensing

Dispense does not automatically blow out. A volume ledger reaching zero is
arithmetic on commands, not a measurement proving that no droplet remains.
Use an explicit blow-out step to expel residual liquid. `push_out` is an
additional plunger air volume during a dispense and is a separate feature.
See the [Opentrons liquid-command guide](https://docs.opentrons.com/python-api/building-block-commands/liquids/).

Example proposal arguments, for review before execution:

```json
{"action":"blow_out","args":{"pipette":"left","location":{"labware_nickname":"plate","position":"A1"}}}
```

Touch-tip is object-dependent, but requires no Python serialization: the
service resolves the requested labware to the loaded engine ID and submits
`touchTip` with the well name. The engine computes contact points from that
labware's geometry and rejects incompatible labware.

## Preserve the motion path

`/control/move-to` retains `force_direct`, `speed` and `minimum_z_height`.
The default `force_direct: false` permits the robot's raised/arced path.
`force_direct: true` requests a straight move without a Z-retract waypoint.
For XY motion at constant height, the destination Z must equal the current Z;
the flag itself does not freeze Z. The reviewed direct path must clear obstacles.

Flex manual gripper movement differs: its sample-prep wrapper uses direct
hardware movement. The matching HTTP endpoint defaults to direct movement and
uses `robot/moveAxesTo`. `robot/moveTo` explicitly plans an arc, so substituting
that command would change the path. See [Flex mapping](FLEX_HTTP_SUPPORT.md).

## Stop and failure behavior

The STOP RUN button remains usable while a command is pending. A separate
connection sends the run stop; subsequent commands are blocked, including later
commands inside a mix. Success requires `status: stopped` readback. A network
failure remains unknown. Late command completion cannot clear the stop latch.
The latch survives gateway restart. Inspect the robot, close the old session,
and explicitly start a fresh session before continuing.

This is a software stop of the gateway-owned HTTP run. It does not promise a
physical emergency stop, interrupt an SSH REPL, or guarantee all module heaters
and shakers are off. Check module readbacks and the machine's physical stopping
procedure. Claims are still required; the button cannot override another
session's ownership. Plan abort cancels subsequent steps; an already-started command keeps its actual outcome. Use STOP RUN to request interruption of that command.

## Remaining boundaries

This is not a complete mirror of every upstream Python API object. Pure
readbacks already represented by status/labware discovery do not each get a
new control route. Python-only helpers and aliases do not need duplicate routes.
Still unimplemented: per-axis max speeds, effective well-bottom-clearance
configuration, complex transfer/distribute/consolidate compilation, newer
liquid classes/sensing, runtime parameters, uploaded Python protocol lifecycle,
and newer optional modules. Unsupported parameters are rejected rather than
silently ignored. Arbitrary `invoke()` remains unavailable over HTTP.

The local `matterlab_opentrons` wrapper is a useful inventory, but it also uses
private hardware APIs and has methods that the robot cannot execute as written.
Coverage must be checked at argument and robot/software-version level.

New endpoints here do not automatically update the central `lab-skills` SDK
or sample-prep workflows. Those are separate repositories. This change only
updates this gateway; it does not deploy a service or execute a robot. Tests
use mocks and dry-run. Physical acceptance remains an operator-planned activity.
