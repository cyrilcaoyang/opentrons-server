# Manual pipette panel

The gateway UI's foldable **DIRECT DRIVE** panel supports attended
workflow development. It starts folded and manual motion starts disabled on each visit and requires the existing
device claim. Motion is allowed only by the gateway state machine; the panel
does not disable interlocks or reset a stop latch. **Clear error** dismisses only the panel message; it does not reconcile a gateway fault.

## Controls

1. Connect the gateway and take control. Home explicitly if the controller says
   its position is unknown; position refresh never homes automatically.
2. Enable manual control and select the left or right pipette under **DIRECT DRIVE**.
   Each instrument box shows tracked tips and last-read height. **Home Z** homes
   only the selected mount's Z using `POST /control/home-pipette-z`.
3. XYZ reads automatically on mount selection, after each move, and
   once per second while idle. Polling is serialized with panel commands, pauses
   during other controls or hidden tabs, and stops on an error. **Clear error**
   resumes reads; it does not clear a gateway fault. Manual controls movement
   only: turning it off retains XYZ and continues permitted reads. Losing the
   claim or connectivity stops polling and clears readback.
   Readings are timestamped last-read controller positions, not an in-motion stream.
   Switching pipettes shows that mount's cached reading while awaiting a fresh
   read. This browser cache is display-only and never supplies jog limits or
   targets. A selection made while busy waits for gateway read permission; it
   does not clear gateway errors or stop latches.
4. XY and Z have independent step and speed settings: positive steps up to
   10 mm and speeds up to 100 mm/s. Each jog requests one step. No hold-to-repeat or queue is used.
5. **Copy XYZ** performs a fresh controller read and copies a JSON object
   `{"x": ..., "y": ..., "z": ...}`, retaining controller precision. Editable
   target fields are never the copy source. Plain HTTP Tailnet pages use a
   clipboard fallback. The JSON also appears in a selectable text box if browser
   clipboard access is unavailable.

**X+ is right, Y+ is back, Z+ is up in the robot's deck frame.** The reference is
the selected pipette's critical point: the nozzle end without a tip, the tip end
with one (using the tip-length calibration). Deck calibration, the left-mount
offset and pipette-offset calibration are already applied; labware offsets are
not. Removing or attaching a tip changes the reference geometry. On a
multi-channel pipette the default critical point is the primary nozzle (A1, the
back nozzle; 31.5 mm behind the body center on the GEN2 8-channels).

That default has one HTTP exception. After a pipette's last move went to a well
of labware with Opentrons' `centerMultichannelOnWells` quirk (reservoirs such as
`nest_12_reservoir_15ml` or `nest_1_reservoir_195ml`), the run engine reports the
critical point it centered on that labware: the column's Y center for
multi-well reservoirs, the pipette's XY center for 1-well ones. A move to deck
coordinates resets it to the default. On a multi-channel pipette the two
differ by up to 31.5 mm in Y; on a single-channel they coincide. SSH always
reads the default critical point.

These are controller positions, not measurements. The OT-2's motors have no
encoders, so a stall or belt slip is invisible in them (see transport behavior
below for what each transport returns).

Jog is a **straight move with no Z retract**, preserving the other coordinates.
The operator must inspect the physical deck and raise the pipette clear before
lateral travel. Numeric envelope checks do not establish a collision-free path.

**Centered reference refusal (HTTP, multi-channel):** jog adds its step to a
read and moves the default critical point to the result. After a move into a
centering reservoir (critical point above) the read is centered, so the pipette
would also shift up to 31.5 mm toward the front in Y with no Z retract. HTTP
jog therefore refuses a multi-channel pipette with 412 before any command, and
without latching a fault, when its last well was such labware. It also refuses
when that cannot be confirmed: after a well command that failed, timed out or
lost transport. Home, or move the pipette to deck coordinates, then jog. The
robot-server exposes no current-location route, so `RunEngineClient` mirrors
the engine's rule from the commands it posts; only a well-addressed command can
set a centered location. Single-channel pipettes and SSH are unaffected. Copy
XYZ and plain position reads are not refused; in that state they return the
centered position.

Below the pipette selector, the XY pad and Z column stay visible. The center
**HOME** uses full robot homing; **Z Home** homes only the selected pipette Z.
**Move XY** and **Move Z** tabs above the pad select the enabled movement
buttons and the speed and distance presets to edit. The other group stays
visible but disabled. Each axis group retains its own settings.
The bottom row shows XYZ readback and **Copy XYZ**. Absolute XYZ targets and
well-approach controls are not available in this manual panel.

The HTTP software **STOP** remains available during a pending jog. A stopped
run requires inspection and a fresh session; this button does not replace the
physical emergency stop. Stop is unavailable on transports that do not advertise it.

## Coordinate limit sources

The UI labels `coordinate_limits` as **Gateway limits (mm)**. These are the
request-validation envelope from `gateway/limits.py`, not a measurement of the
selected pipette's reachable travel.

- X 0–446.75 and Y 0–347.5 mm use the nominal extents in Opentrons'
  [OT-2 robot definition](https://github.com/Opentrons/opentrons/blob/edge/shared-data/robot/definitions/1/ot2.json).
  Verified against the installed `opentrons_shared_data` definition at
  `data/robot/definitions/1/ot2.json`. The gateway's fallback values match it.
- That definition's Z extent is `0.0`; it does not supply a usable Z limit.
  The gateway's default Z 0–218 mm is a local validation cap, configurable via
  `OT2_MAX_Z_MM`, and must not be described as an Opentrons-certified limit.
- Opentrons documents [deck coordinates](https://docs.opentrons.com/python-api/robot-position/#position-relative-to-the-deck)
  and [critical-point position readback](https://docs.opentrons.com/hardware/hc_api.html#opentrons.hardware_control.api.API.gantry_position).
  Mount, pipette, tip and calibration affect the relationship to machine axes;
  nominal extents alone do not establish the selected critical point's reach.

The UI and gateway continue to enforce the configured envelope. No bounds were
expanded based on this documentation review.

## Gateway and transport behavior

`POST /control/pipette-position` takes `{pipette}`; `POST /control/jog` takes
`{pipette, axis, distance_mm, speed}`. Both require the existing claim. They are
operator controls and are deliberately absent from `/plans/actions`. Agents
continue to propose existing reviewed workflow actions through the normal
lab-skills and plan boundary; no new SDK control route is implied.

Jog reads the current controller XYZ, adds the requested step on one axis,
checks the resulting target with the same `CoordinateLocation` bounds used by
absolute moves, moves with `force_direct=True` at the requested speed, and reads
XYZ again. All of this occurs under the gateway command lock. The browser never
supplies the current XYZ. A limit refusal causes no motion and returns 412.

Position values may legitimately fall outside conservative command bounds (for
example at home); readback is not clamped or fabricated. The destination still
must pass configured bounds. Unhomed or unavailable positions refuse the action.
If communication fails during a jog, its relative displacement cannot be retried
safely: the service records `unknown_outcome` and requires reconciliation.

HTTP uses Opentrons `savePosition` with `failOnNotHomed=true`. It returns the
pipette critical point without motion, from the hardware controller's stored
position: each successful move sets it to that move's target and a failed move
clears it. It does not query the motor controller. The native `moveRelative` command in
v8.7.0 does not accept a speed, so the gateway uses the serialized read plus
existing `moveToCoordinates` path to honor the operator's speed. SSH queries
`protocol._core.get_hardware().gantry_position(..., refresh=True,
fail_on_not_homed=True)`, which asks the Smoothie motor controller for its
step-count position (`M114.2`), and uses the existing protocol pipette move. This
private core query is verified against both legacy and engine cores in v8.7.0;
an incompatible robot version fails explicitly, with no guessed coordinates.
Already loaded pipettes are resolved by mount from the active run/protocol.

Position queries run for the selected pipette while a claim is held and the
gateway permits reads, including when Manual is off, on Copy XYZ, or within a move.
`/status` remains cache-only. Dry run returns null coordinates and cannot create
a copied physical position. Configured simulation coordinates are labeled as
simulation and are also excluded from Copy XYZ.

Protocol sources:

- [Opentrons v8.7.0 savePosition](https://github.com/Opentrons/opentrons/blob/v8.7.0/api/src/opentrons/protocol_engine/commands/save_position.py)
- [Opentrons v8.7.0 moveRelative](https://github.com/Opentrons/opentrons/blob/v8.7.0/api/src/opentrons/protocol_engine/commands/move_relative.py)
- [Opentrons v8.7.0 legacy core](https://github.com/Opentrons/opentrons/blob/v8.7.0/api/src/opentrons/protocol_api/core/legacy/legacy_protocol_core.py)

## Verification

`tests/unit/test_manual_pipette.py` uses mocked transports and dry-run APIs for
claims, state and stop guards, bounds, speed/path preservation, controller
readback, unknown outcomes, and mount resolution. UI tests verify the disabled
initial state, controls, precision and HTTP clipboard behavior.

After reviewed deployment, operator acceptance belongs on **Complexation only**.
With a clear deck and known tip state, check each axis with a small increment,
check both mounts, compare the copied XYZ with the displayed readback, and verify
speed and stop behavior using the approved acceptance procedure. Development
tests do not move either robot.
