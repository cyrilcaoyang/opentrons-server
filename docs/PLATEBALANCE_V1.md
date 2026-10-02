# platebalanceV1

Optional local Sartorius WZB254-N balance controls in OT-2 slot 9. The gateway
owns its serial transport and serializes balance actions with robot commands.
This is not an Opentrons `loadModule` model. Pipetting geometry is available
only when the local config qualifies an exact plate definition and measured
holder height. Setup refuses ordinary labware or native modules in reserved slot 9.
Flex is unsupported. With no configuration, the module is absent.

## Configuration

Install with `uv sync --extra labware --extra platebalance`. Set
`OT2_PLATEBALANCE_CONFIG` to an absolute path to a local JSON file:

```json
{
  "model": "WZB254-N",
  "slot": "9",
  "adapter_height_mm": null,
  "balance_blow_out_enabled": false,
  "max_labware_height_mm": 25,
  "com_port": "COM3",
  "baudrate": 9600,
  "bytesize": 7,
  "parity": "odd",
  "stopbits": 1,
  "timeout": 1.0,
  "units": "g"
}
```

The slot defaults to **9**; explicitly set `slot` for other installations.
Changing it does not move or retire any recorded tips or labware. The optional
`adapter_height_mm` field is metadata only. Single well plates with definition height
**strictly below 25 mm** can be declared on the balance for weighing. A configured
maximum may lower this limit but cannot raise it. These fields do not enable
pipetting; geometry must be qualified before loading plates into the robot run. Never substitute zero for an unknown height.

The serial settings must match the balance's configured SBI interface; the
example uses the Matter Lab defaults, not observed device settings. Confirm
port identity on the PC actually hosting this gateway. Serial ports are opened
only for explicit actions, never during construction or `/status` polling.

## Plates on the balance

In the slot picker, select the balance slot and a well plate. The gateway checks
its schema-2 definition, well-plate category and positive finite height below
25 mm. Exactly 25 mm, tip racks, reservoirs, adapters and assemblies are refused.
Custom plates require their full definition. Any well grid is accepted.

The existing full-layout `deck.declare` payload represents that slot as:

```json
{
"9": {
  "load_name": "corning_96_wellplate_360ul_flat",
  "support_module": "platebalanceV1",
  "plate_id": "collection_plate_001"
}
}
```

Retain all other slot declarations in a full-layout request. The explicit
`support_module` marker distinguishes intended placement from an old occupant.
The plate's identity and complete definition survive restart and unrelated edits.
Normal claimed users may declare or clear a plate; only admins may change module
layout through the API or approved chat. Clearing the plate leaves the balance.
No robot transfer happens. Balance Read/Tare/Zero remain available with the plate.
Native loading and pipetting by slot, nickname or plate ID stay blocked unless
the local config contains qualified geometry for the exact declared plate.
Manual jogs do not infer deck clearance. The operator confirmed that the
balance holder locates the Agilent plate in slot 9's normal XY position and
orientation.

For qualified balance dosing, dispense only with the tip at least
**2 mm above the highest physical top of the seated plate**, including its rim.
Only balance dosing uses at most half the official dispense default for the attached
OT-2 GEN2 pipette model; ordinary HTTP dispenses use the full model default.
The gateway must enforce both balance limits on every such
dispense, including calls with explicit offsets or flow rates; a generic
well-top default does not establish clearance above the plate rim. For the Agilent 19 mm plate seated on the
slot-9 balance, the operator measured the highest rim at 121 mm above the OT-2
top deck surface. The required stationary tip clearance is therefore at least
123 mm above that same surface; the requested 20 mm-above-A1 position would be
141 mm because the measured rim and the definition's A1 top coincide. These are
physical measurements; the first robot move still requires operator acceptance.
The Agilent definition gives A1's plate-local
center as (13.88, 74.26) mm and its well top as 19 mm above the plate base.
The normal slot XY alignment is operator-confirmed; the robot's calibrated deck
coordinates and collision clearance along the travel path still require a
reviewed Complexation operator acceptance before routine dosing.

The optional `pipetting_geometry` config binds these heights to one exact Lab
Store definition. It is absent by default, leaving balance well actions blocked.
The operator confirmed that the 102 mm seating height and 121 mm rim height
were both measured from the OT-2 top deck surface. The Agilent geometry entry is:

```json
{
  "pipetting_geometry": {
    "load_name": "agilent_96_700ul_square_flat",
    "definition_sha256": "982450933a332b5755569896666836c1d51ea8c60fc6313ef45b7145a4af9568",
    "xy_alignment": "slot",
    "seating_height_mm": 102,
    "rim_height_mm": 121
  }
}
```

The hash covers the full schema-2 definition with sorted JSON keys and compact
separators. A changed definition, unqualified plate, missing geometry, or deck
placement conflict fails closed. The compiled run definition raises each well
by 102 mm and gives the robot path planner a 121 mm plate envelope. Balance
well moves require an explicit offset at least 2 mm above the rim and an arced
path. Dispense defaults to that clearance and caps flow at half the official
GEN2 model default, including explicit flow rates. This path accepts only
single-channel GEN2 pipettes until a multi-channel clearance is qualified.
Aspirate, mix, touch tip, tip handling and other contact actions remain blocked
at the balance. A separate `balance_blow_out_enabled` config flag, off by
default, permits a balance-well `blow_out` only after operator acceptance on
Complexation. It requires the same exact plate geometry, qualified
single-channel GEN2 pipette, 2 mm rim clearance and guarded arc as dispense.
The current pipette blow-out flow rate must be no more than half its documented
dispense default; set it explicitly before the blow-out if needed. The gateway
refuses a faster rate before motion and never silently changes it. Use an
explicit well destination; `in_place=true` has no balance placement or flow
check and must not be used as a balance workaround. This configuration applies
to Complexation; do not apply it to HTE. Blow-out moves the plunger beyond
its usual dispense position to expel residual liquid, but only a stable
balance read measures what reached the plate.

## Operator controls

`POST /control/platebalance/{read|tare|zero}` requires the existing gateway
claim and authentication. Actions also require an idle, ready gateway, valid
slot placement and no stop latch or unresolved outcome. They are published in
`allowed_actions`. Read, Tare, and Zero are also proposable plan actions. A
loaded plate should be tared and its stable near-zero baseline observed before
aspiration and dosing. The read step contains `reading` with value in grams, stability and observation time. An
unstable single read is labelled unstable; request `wait_until_stable: true`
when the plan requires a stable weight. A timeout fails the step and skips later
steps. Tare and Zero are non-idempotent plan steps. Tare sends its serial
command once, then waits up to 30 seconds for two fresh stable readings within
0.0002 g of zero. Its successful result is `baseline_observed`; a timeout
halts the plan before the next action and records `baseline_unconfirmed`.
Zero remains `sent_unconfirmed` because its write has no acknowledgment.

- **Weight** (API action `read`) sends `ESC P CR LF` once and returns the reported weight, stability,
  unit and observation time. Invalid, partial, overload and error frames fail.
- **Wait until stable** is checked by default in the UI (10-second wait budget).
  `read` accepts `{"wait_until_stable": true, "timeout_s": 10}`. Without a body,
  it retains the single-read behavior. Timeout is bounded to 1–30 seconds.
  Only valid unstable samples trigger another read; communication errors,
  malformed frames and overload stop immediately without retry. The pinned
  Sartorius SBI parser uses the gram unit marker as its stability indication;
  the device must be configured for gram weighing and single-value SBI output.
  Stability must be checked against the physical display during operator acceptance.
  A timeout returns HTTP 409, records `stability_timeout` and preserves the last
  sample with its timestamp/stability flag; it does not latch a robot error.
  The gateway holds the command lock throughout. A software stop ends the loop
  after the current bounded serial transaction; no recovery motion is issued.
  Use a client request timeout longer than the wait budget plus serial I/O overhead.
- **Tare** sends `ESC U CR LF` once, then reads until the stable baseline is observed or the bounded wait expires.
- **Zero** sends `ESC V CR LF` once.

The model-specific commands extend `matterlab-balances==1.1.0` using
`matterlab-serial-device==1.2.1`. The generic driver's combined `ESC T` and
repeated stable-tare routine are not used. See the manufacturer's
[WZB operating instructions, §8.3–8.5, pp. 32–38](https://api.sartorius.com/document-hub/dam/download/190205/Manual_Weigh_Cells_WZB_N_WZB254-NC_WWZ6018-e221001.pdf).
Zero is distinct from calibration; no adjustment commands are exposed.

Use Tare to make an already loaded plate the baseline; Zero is for the unloaded
balance and has a limited zero range (2% of maximum load by default). The
driver waits one second after sending either command. For Tare, the gateway
then queries the balance under the same command lock until two stable values
within 0.0002 g of zero are observed; this verifies the baseline, not receipt
of a serial acknowledgment. The manufacturer's default setting tares after
stability, so vibration from nearby robot motion can delay the baseline. A
weight query may return no frame during this wait; the gateway retries
only those empty reads within the 30-second wait and never sends tare again.
If no frame is available at the deadline, the outcome remains unknown and the
gateway requires operator reconciliation. Malformed nonempty frames fail
immediately. A valid but unsettled baseline times out as `baseline_unconfirmed`.
gateway robot command cannot run during the tare wait. Park the robot and let
motion settle before starting a tare; the gateway cannot stop motion commanded
outside its own session. A timeout stops the plan before any following
dispense. The last observed reading is retained for inspection, and Tare is
never repeated automatically. A communication failure after the serial write
latches `unknown_outcome` for operator reconciliation. Zero returns
`sent_unconfirmed`; a later reading cannot prove that command changed the
reference. A cached reading is timestamped and is not proof of current
connectivity. Simulation/dry run perform no serial I/O. Plan results report
observed weight under the balance's current reference.

An existing slot-9 declaration or observed occupant is preserved, never silently
overwritten. Reconcile the old rack/plate records and clear its slot declaration
through the operator controls before enabling balance actions. Readings are not
persisted as scientific records; workflows must record measurements in BitacoraDB.

## Host migration

Before moving a gateway: check idle/unclaimed state and inactive robot run;
prepare the new checkout/environment without starting its gateway; stop and
disable the old service; copy all instance state files including the stop latch
and HTTP run state; preserve credentials without logging them; start the new
service; verify identity/state/authentication by read-only routes; switch the
registry and authenticated edge upstream. Keep the old installation for rollback.
Never run both gateways concurrently. Do not clear a stop latch to make startup
succeed. Hardware acceptance (Read/Tare/Zero and motion) is operator-run.
