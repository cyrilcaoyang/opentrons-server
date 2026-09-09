# Flex HTTP support and the sample-prep codebase

The local reference project is `sdl2_sampleprep_platform` (without an underscore
between “sample” and “prep”). Its `src/sdl2_solid_dose/opentrons/ot_flex_protocol.py`
wraps `matterlab_opentrons` and also issues private hardware calls through SSH.
That project has been inspected, not modified.

## Select a separate Flex gateway

Set `OT2_ROBOT_MODEL=Flex` and `OT2_TRANSPORT=http` in a new gateway instance's
configuration. Configure its own identity, robot URL and state-file paths.
Do not repoint either existing OT-2 service. OT-2 remains the default profile.

The Flex profile uses A1–D4 slots and X/Y bounds from the installed Opentrons
OT-3 definition. Z remains a conservative 218 mm ceiling, not a measured
reachability guarantee. A reference workflow's 250 mm safe-Z therefore needs
machine-specific envelope validation before it can be used here. Passing a
coordinate bound does not establish collision clearance.

Startup reads the robot model and OpenAPI command schemas before creating a
run. It refuses an OT-2 identity or missing Flex command types. Command shapes
were checked against the official Opentrons v8.7.0 source; no Flex hardware
acceptance has been performed.

## Mapping the sample-prep functions

| Sample-prep operation | Gateway equivalent |
|---|---|
| Aspirate/dispense/mix | Shared pipetting endpoints and proposal actions |
| Move pipette to absolute XYZ or a well | `/control/move-to`, preserving `force_direct`, speed and minimum Z |
| Heater-shaker functions | Shared `hs-*` endpoints; load the module through setup |
| Move labware with gripper | `/control/move-labware` with `use_gripper: true` |
| Move onto module/adapter with offsets | Name the loaded module/adapter as `new_location`; provide `pick_up_offset` and `drop_offset` XYZ objects |
| Open/close gripper jaw | `/control/gripper-open-jaw`, `/control/gripper-close-jaw`; robot default gripping force |
| Absolute gripper motion | `/control/gripper-move-to-absolute`, XYZ, speed and explicit motion choice |
| Relative gripper motion | `/control/gripper-move-to-relative`, dx/dy/dz and speed |
| Home gripper | `/control/home-gripper`, gripper Z only; no hidden homing before moves |
| Trash bin | `/control/load-trash-bin`, explicit location such as A3 |

The operator panel displays Flex's alphabetic slots. Module and adapter nesting
is resolved from observed run locations so a plate remains visible after a move.
Manual jaw/axis commands do not update the engine's labware location records;
use managed gripper labware moves for tracked plates whenever applicable.

## Direct motion is intentional

Pipette moves default to an arced path; `force_direct: true` preserves straight
motion. Absolute gripper moves default to direct movement, matching the
sample-prep wrapper's `hardware.move_to`. They use `robot/moveAxesTo`, letting
the robot apply its own mount offsets and critical point. Setting
`force_direct: false` selects the arced `robot/moveTo` command.

Relative gripper moves use `robot/moveAxesRelative`. A zero dz retains the
current gripper height while XY changes. The gateway does not insert a retract
or home into these direct operations. They require an explicitly reviewed clear
path; machine travel bounds do not provide collision avoidance.

## Deliberate limits

The first Flex profile supports full 1- and 8-channel heads only. It rejects
96-channel and partial nozzle layouts before setup sends commands. Existing
tip selection/tracking is retained, with model-specific slot keys. Moving a
tracked tip rack with the gripper is refused because its tracker remains tied
to the declared slot. A future 96-channel/partial-layout phase requires its
own tracking design.

Flex has no assumed fixed trash. Register a bin or give an explicit well drop
location; an unconfigured bare drop is refused. Robot deck configuration must
already match the physical fixture. Trash chutes and staging fixtures are not
automatically installed or configured by the gateway.

Private jaw-width/position readbacks, pipette-relative convenience wrappers,
camera streaming and stem-pressure logging have not been ported. No arbitrary
Python execution endpoint has been added. Cross-device sample-prep sequences
remain workflow code; they do not belong inside this gateway.

The central SDK and sample-prep workflow migration are still separate work.
They must consume this typed surface through the lab's existing approval,
claim and interlock layers. Changes outside this repository require the
repository's explicit path-and-change approval process.

Sources: [gripper labware move](https://github.com/Opentrons/opentrons/blob/v8.7.0/api/src/opentrons/protocol_engine/commands/move_labware.py),
[robot commands](https://github.com/Opentrons/opentrons/tree/v8.7.0/api/src/opentrons/protocol_engine/commands/robot),
[waypoint planning](https://github.com/Opentrons/opentrons/blob/v8.7.0/api/src/opentrons/protocol_engine/execution/movement.py),
[axis coordinate conversion](https://github.com/Opentrons/opentrons/blob/v8.7.0/api/src/opentrons/protocol_engine/execution/gantry_mover.py).
