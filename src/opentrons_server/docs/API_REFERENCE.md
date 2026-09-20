# Opentrons OT-2 gateway — API reference

Base: the deployed gateway's `http://<host>:8020` (HTE) or `:8021`
(Complexation). Both run the same code, so the surface below is identical;
only identity, robot address and state-file paths differ. All timestamps are
UTC ISO-8601. Exact request/response schemas: `/openapi.json`. Tags: `spec`,
`documentation`, `ui`, `claim`, `control`, `plans`, `assistant`.

Two conventions before the tables:

- **Claim gate.** Rows marked *claim* require a valid `X-Claim-Token` header.
  Without one they return **423** `{detail, claimed_by, retry_after_s}`.
- **Strict bodies.** Every `/control/*` body model forbids unknown fields, so
  a misnamed argument is **422**, never a silently different command. Any
  route taking a body can also return **422** on a schema violation; the
  tables name 422 only where it carries extra meaning.

## Read — open, side-effect-free, no claim

| method + path | returns |
|---|---|
| `GET /` | `ProbeResponse` — `{equipment_id, equipment_name, protocol_version}` (`"1.2"`) |
| `GET /health` | `{"status": "healthy"}` — liveness only, never touches the robot |
| `GET /status` | the full `EquipmentStatus` envelope — see below |
| `GET /docs/agent` | the JSON equipment guide + generated action catalog. No claim, no hardware I/O, available before startup. |
| `GET /plans/actions` | `{actions: [{action, idempotent, args_schema}]}` — the proposable catalog, generated from the same models that validate a step |
| `GET /openapi.json`, `GET /docs`, `GET /redoc` | OpenAPI document, Swagger UI, ReDoc |
| `GET /agent-docs`, `GET /agent-docs/api-reference` | this documentation (`text/markdown`) |
| `GET /llms.txt` | discovery index (`text/plain`) |

### `GET /status` envelope

Top level: `protocol_version`, `equipment_id`, `equipment_name`,
`equipment_kind` (`"liquid_handler"`), `equipment_version`, `host`,
`equipment_status`, `activity`, `activity_since`, `message`,
`required_actions`, `allowed_actions`, `device_time`, `uptime_seconds`,
`components`, `metrics`, `last_error`, `details`.

| `metrics` key | unit | notes |
|---|---|---|
| `cycles_total` | `count` | §2.3.1 reserved key: protocol commands **completed** since this process started |

| `details` key | meaning |
|---|---|
| `service_state` | the internal state name (`requires_init`, `connecting`, `ready`, `busy`, `paused`, `dry_run`, `error`, `unknown_outcome`, `external_control`) |
| `dry_run`, `simulation` | simulation flags |
| `snapshot` | `{deck, pipettes, labwares, modules}` — `deck` is the normalized, provenance-tagged `DeckState`; the rest are the raw control-plane read |
| `session_recipe` | what `/control/setup` loaded. **Not authoritative** for what is on the deck |
| `loaded_plate` | the orchestrator-tracked `LoadedPlate`, or `null` |
| `tip_racks` | per-rack tip-status summary |
| `mounted_tips` | tips currently on each pipette: origin rack/well, covered span, channels, `last_sample`, `contacted_liquid`, `uncertain` |
| `pipette_channels` | tip wells consumed per pick, per pipette |
| `pipette_volumes` | `{pipette: {min_ul, max_ul}}` — the **effective** live envelope the 412 guard uses |
| `gateway_profile` | `{model, slots, supported_channels}` |
| `robot` | last probe plus `reachable`, `probe_url`, `last_seen_at`, `readback_age_s`, `unreachable_since`, `probe_failures`. Present once the robot has been probed or declared unreachable |
| `claimed_by` | `{owner, session_id, expires_at}`, present only while claimed |
| `ui_mode` | `off` \| `open` \| `edge` |
| `control_auth` | `identity` \| `claim_only` \| `open` |

## UI support (edge-gated when `OT2_TRUST_LOCAL_UI=false`)

| method + path | body | responses |
|---|---|---|
| `GET /labware` | — | `{definitions: [...]}` grid summaries for the deck-declare picker. Empty when `opentrons-shared-data` is not installed. |
| `GET /labware/{load_name}` | — | one full Opentrons definition; **404** for an unknown name *or* a missing `opentrons-shared-data` |

With `OT2_TRUST_LOCAL_UI=false`, any request to `/labware`, `/labware/*`,
`/ui` or `/ui/*` that did not come through the auth edge is **404** — the UI
surface simply does not exist for anyone else. `/ui` is a mounted static SPA
and therefore absent from the OpenAPI document.

## Claim protocol

| method + path | body / header | responses |
|---|---|---|
| `POST /control/claim` | `{owner, session_id, ttl_s, takeover?}` | **200** `{claim_token, heartbeat_interval_s, expires_at}`. `ttl_s` floored at 5 s; `heartbeat_interval_s` = ttl/2, floored at 1 s. Idempotent for the same `owner`+`session_id` (keeps the token). **409** `{detail, claimed_by, retry_after_s}` + `Retry-After` when another owner holds it. `takeover: true` supersedes the **same owner's** claim and mints a fresh token. **401** `{error: "login_required", hint}` when `OT2_REQUIRE_LOGIN` is on and no credential was presented. **403** `{error: "propose_only_principal", hint}` for an `OT2_PROPOSER_KEYS` credential. |
| `POST /control/heartbeat` | header `X-Claim-Token` | **200** with a new `expires_at`; **401** when the token is unknown or expired |
| `POST /control/release` | header `X-Claim-Token` | **204**, idempotent |

## Control — lifecycle (claim)

All return `CommandResponse` `{ok, message, state}` on 200 unless noted.

| method + path | body | responses / notes |
|---|---|---|
| `POST /control/startup` | `{host_alias?, password, simulation}` | **503** `{detail}` on failure (`last_error.code: "startup_failed"`) |
| `POST /control/shutdown` | — | always succeeds; device then reports `requires_init` |
| `POST /control/setup` | `{labware: [], instruments: [], modules: []}` | the session recipe. Each item's `nickname` must be a non-reserved Python identifier → **422** otherwise. **409** on setup failure. Tip racks here auto-register with the tip tracker. |
| `POST /control/home` | — | **409** on failure |
| `POST /control/pause` | — | → `PAUSED` / `degraded` |
| `POST /control/resume` | — | leaves `PAUSED` |
| `POST /control/stop` | — | **software** stop of this gateway's HTTP run — not a hardware e-stop. Stays reachable while a command is in flight. Success only after stopped readback; latches until `shutdown` + `startup`. **409** when the transport is SSH, there is no session, the robot is under external control, or a stop is already in progress. |
| `POST /control/reconcile` | optional raw snapshot object, or no body | acknowledges `unknown_outcome`, or an `error` that still has a live session, and returns to `ready`. **409** while a stop is latched. |

## Control — motion and liquid handling (claim)

`pick_up_tip`, `drop_tip`, `aspirate`, `dispense` and `move_labware` are
**non-idempotent**: a transport loss during one leaves the device in
`unknown_outcome`. `move_to` is idempotent and can simply be re-issued.

| method + path | body | responses / notes |
|---|---|---|
| `POST /control/move-to` | `{pipette, location \| coordinates, speed?, force_direct?, minimum_z_height?}` — exactly one of `location` (`{labware_nickname, position, top?/bottom?/center?}`) or `coordinates` (`{x, y, z}` in deck mm) | **422** if both or neither target, or a bound is exceeded; **409** on failure |
| `POST /control/pick-up-tip` | `{pipette, labware_nickname?, position?, sample_id?, force?}` — omitting `position` on a tracked rack auto-picks; omitting `labware_nickname` too auto-selects a compatible tracked rack | **412** `TipUnavailable` body; **409** otherwise |
| `POST /control/drop-tip` | same `TipRequest` shape | **409** on failure |
| `POST /control/aspirate` | `{pipette, volume_ul, location, flow_rate?}` | **412** out-of-envelope body when the volume is outside the attached pipette's min/max; **422** above 1000 µL or ≤ 0; **409** otherwise |
| `POST /control/dispense` | as aspirate, plus `push_out?` (extra plunger air in µL, separate from blow-out) | as aspirate |
| `POST /control/move-labware` | `{labware_nickname, new_location, use_gripper?, pick_up_offset?, drop_offset?}` | **422** when `use_gripper` is set on a non-Flex profile, or offsets are given without it; **409** otherwise |

## Control — bookkeeping (claim, no robot motion)

These work in any state, including dry run, and stay available while the robot
is unreachable.

| method + path | body | responses / notes |
|---|---|---|
| `POST /control/plate/load` | `{plate_id, model, wells?}` — `wells` defaults to 96 empty | **200** `LoadedPlate`; **422** on an invalid well map |
| `POST /control/plate/unload` | — | **200** the removed `LoadedPlate`, or `null` |
| `POST /control/well/update` | `{well, sample_id?, volume_ul?, notes?, clear_sample_id?, clear_notes?}` | **200** `WellSample`; **409** no plate loaded or well not on it; **422** invalid value |
| `POST /control/deck/declare` | `{slots: {"2": "<load_name>" \| {load_name\|kind\|module_name, definition?} \| null}}` | **200** the merged `DeckState`. **Full-layout replacement, not a patch** — omitted slots are cleared, an empty map clears the declaration. **422** on an invalid slot value. |
| `DELETE /control/deck/declare` | — | **200** the merged `DeckState` after clearing the declaration |
| `POST /control/tips/reset` | `{slot \| nickname, wells?}` | **200** `TipRackState` — (re)registers a rack with every tip fresh (a physical swap). **422** on an unknown target or well list |
| `POST /control/tips/mark` | `{slot \| nickname, status: "new"\|"empty", wells \| columns}` — exactly one of `wells`/`columns`; columns 1–12 | **200** `TipRackState` — the partial repair tool. **409** when the slot holds no tracked rack; **422** for a well the rack does not have |

## Control — convenience and modules (claim)

| method + path | body | responses / notes |
|---|---|---|
| `POST /control/lights` | `{on: bool}` | **409** with no session; **502** `robot lights request failed: …` when the robot's HTTP API fails |
| `POST /control/tempmod/set` | `{celsius: 4–95, module?}` | returns once the target is **accepted**; the block ramps in the background. **409** on failure |
| `POST /control/tempmod/deactivate` | `{module?}` | **409** on failure |

## Control — allowlisted advanced actions (claim)

Generated from one typed table, so each route, its plan action and its schema
cannot drift. All take `CommandResponse`; all refuse with **409** on failure,
**422** on a schema violation, **423** without a claim, and — where a volume
is involved — **412** out of envelope. Advertised in `allowed_actions` only
while the device is `ready` and no command is in flight.

| method + path | body | idempotent |
|---|---|---|
| `POST /control/comment` | `{message}` 1–2000 chars | yes |
| `POST /control/delay` | `{seconds}` 0–86400 | no |
| `POST /control/blow-out` | `{pipette, location \| in_place: true}` — exactly one | no |
| `POST /control/touch-tip` | `{pipette, labware_nickname, position, radius?, v_offset?, speed?}` — radius 0–1, `v_offset` −100–0, speed 1–80 | no |
| `POST /control/mix` | `{pipette, volume_ul, location, repetitions 1–1000, rate?}` — `flow_rate` is rejected here; set it with `set_flow_rate` first | no |
| `POST /control/air-gap` | `{pipette, location, volume_ul, height?}` — `height` is above the well **top**; location offsets are rejected | no |
| `POST /control/prepare-aspirate` | `{pipette}` | no |
| `POST /control/home-pipette` | `{pipette}` | yes |
| `POST /control/home-plunger` | `{pipette}` | yes |
| `POST /control/set-flow-rate` | `{pipette, aspirate?, dispense?, blow_out?}` in µL/s — at least one | yes |
| `POST /control/set-speed` | `{pipette, speed}` mm/s, ≤ 400 — applies to **explicit** gantry moves only | yes |
| `POST /control/hs-latch-open` | `{module}` | no |
| `POST /control/hs-latch-close` | `{module}` | no |
| `POST /control/hs-set-and-wait-shake-speed` | `{module, rpm 200–3000}` | no |
| `POST /control/hs-deactivate-shaker` | `{module}` | yes |
| `POST /control/hs-set-target-temperature` | `{module, celsius 37–95}` | yes |
| `POST /control/hs-set-and-wait-temperature` | `{module, celsius 37–95}` | no |
| `POST /control/hs-wait-for-temperature` | `{module}` | no |
| `POST /control/hs-deactivate-heater` | `{module}` | yes |
| `POST /control/hs-deactivate` | `{module}` | yes |
| `POST /control/tempmod-await-temperature` | `{module}` | no |
| `POST /control/magmod-engage` | `{module, height_from_base 0–20}` mm above labware base | no |
| `POST /control/magmod-disengage` | `{module}` | yes |
| `POST /control/thermocycler-open-lid` | `{module}` | no |
| `POST /control/thermocycler-close-lid` | `{module}` | no |
| `POST /control/thermocycler-set-block-temperature` | `{module, temperature 4–99, hold_time_seconds?, block_max_volume?}` | no |
| `POST /control/thermocycler-set-lid-temperature` | `{module, temperature 37–110}` | no |
| `POST /control/thermocycler-deactivate-block` | `{module}` | yes |
| `POST /control/thermocycler-deactivate-lid` | `{module}` | yes |
| `POST /control/thermocycler-deactivate` | `{module}` | yes |

`module` is a loaded module nickname or a declared deck slot.

**Flex profile only.** When the process is started with
`OT2_ROBOT_MODEL=Flex` (which also requires `OT2_TRANSPORT=http`), the table
above additionally serves `POST /control/gripper-move-to-relative`,
`/control/gripper-move-to-absolute`, `/control/gripper-open-jaw`,
`/control/gripper-close-jaw`, `/control/home-gripper` and
`/control/load-trash-bin`, and **drops** `/control/magmod-engage` and
`/control/magmod-disengage`. Neither deployed instance here runs the Flex
profile; both are OT-2.

## Plans — agent proposes, human approves and runs

Plans live in memory and die with the process. Errors map as: `PlanNotFound`
→ **404**, `StepValidationError` → **422**, `ApprovalRequiresClaim` →
**423**, `PlanHashMismatch` → **409**, everything else in the family →
**409**.

| method + path | gate | body | responses / notes |
|---|---|---|---|
| `GET /plans/actions` | open | — | the proposable catalog with per-action `args_schema` |
| `GET /plans` | open | — | every plan, newest first, each with `executable`, `blocked_reason`, `non_idempotent_actions` |
| `POST /plans` | open (identity when `OT2_REQUIRE_LOGIN`) | `{steps: [{action, args}], created_by?, notes?}` | **201** the draft plan view. Never touches the robot. **422** on an unknown action, bad args, or an empty step list. |
| `GET /plans/{plan_id}` | open | — | **404** when unknown |
| `PUT /plans/{plan_id}/steps` | open (identity when `OT2_REQUIRE_LOGIN`) | `{steps}` | replaces the steps, recomputes `step_hash`, resets to `draft` and **voids any approval**. **409** for an `executing`/terminal plan; **422** on bad steps |
| `POST /plans/{plan_id}/approve` | claim | `{step_hash}` | the human gate. **409** when the hash does not match what was displayed, or the plan is not a draft; **423** with no live claim. Approval expires after **600 s** and records the claim's owner + session. Emits `plan_approved`. |
| `POST /plans/{plan_id}/execute` | claim | — | runs one step at a time, re-checking `allowed_actions` before each. **423** when the live claim is a different owner/session than the approver; **409** when not approved or the approval expired. Fail-fast: first failure halts, remaining steps are `skipped`, `halt_reason` is set. Emits `plan_executed`. |
| `POST /plans/{plan_id}/abort` | claim | — | operator stop; pending steps become `skipped`. Terminal plans are returned untouched. |
| `DELETE /plans/{plan_id}` | claim | — | **204**. Only `executed` / `failed` / `aborted` plans — anything else is **409** ("abort it instead"), and a plan with a command still in flight is **409** too. |

## Assistant — optional in-page chat

A proposer, not a driver: its only write tool is `propose_plan`. Off unless an
API key is configured.

| method + path | gate | body | responses / notes |
|---|---|---|---|
| `GET /assistant/health` | open | — | `{configured, reason, model, key_source, env_file_searched}`. Never the key or provider URL. |
| `POST /assistant/chat` | claim (+ identity when required) | `{messages: [{role: "user"\|"assistant", content}]}` — 1–40 messages, content ≤ 8000 chars | **503** when not configured; **502** on a provider failure; **423** without a claim |
| `POST /assistant/chat/stream` | claim (+ identity when required) | same | `text/event-stream` of tool-boundary events (never model reasoning). Access is validated **before** the stream opens, so 401/403/423/503 keep their normal status; a provider failure after that arrives as a terminal `error` event. |

## Refusal codes

| status | body shape | when |
|---|---|---|
| 401 | `{detail: {error: "login_required", hint}}` or `{detail}` | login required; unknown/expired claim token on `heartbeat` |
| 403 | `{detail: {error: "propose_only_principal", hint}}` | a propose-only credential tried to claim or control |
| 404 | `{detail}` | unknown plan or labware `load_name`; a `/ui`/`/labware` request that bypassed the auth edge |
| 409 | `{detail}` or `{detail, claimed_by, retry_after_s}` | state conflict, command failure, claim held by another owner, plan state/hash error |
| 412 | **top-level structured body**, never wrapped in `detail` | tip unavailable (`{detail, rack, well, tip_status, requested_sample_id, retry_after_s, …}`) or out of envelope (`{detail, pipette, requested_ul, max_ul, min_ul, retry_after_s}`). Does not touch `last_error`. |
| 422 | `{detail: [...]}` | schema violation, unknown body field, unknown plan action |
| 423 | `{detail, claimed_by, retry_after_s}` | missing or invalid `X-Claim-Token` |
| 502 | `{detail}` | robot HTTP API failure (lights); assistant provider failure |
| 503 | `{detail}` | startup failed; assistant not configured |

## Skill names (what `allowed_actions` lists)

`startup`, `shutdown`, `setup`, `home`, `pause`, `resume`, `stop`, `move_to`,
`pick_up_tip`, `aspirate`, `dispense`, `drop_tip`, `move_labware`,
`plate.load`, `plate.unload`, `well.update`, `tips.reset`, `tips.mark`,
`lights.set`, `deck.declare`, `tempmod.set`, `tempmod.deactivate`, plus every
advanced-action name while the device is `ready`. `reconcile` is a
`required_actions` entry, not an `allowed_actions` one.
