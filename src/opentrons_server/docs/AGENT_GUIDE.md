# Opentrons OT-2 gateway — agent guide

This service fronts **one** Opentrons OT-2 liquid handler and speaks the AC
Organic lab's STATUS_SPEC **v1.2** (`equipment_kind: "liquid_handler"`,
`protocol_version: "1.2"`). Read this before driving it; the
[API reference](agent-docs/api-reference) lists every route with its gate,
body and refusal codes, and `/openapi.json` carries the exact schemas.

Two machine-readable resources already exist on this device and are **not**
duplicated here — read them, do not guess:

- **`GET /docs/agent`** — the JSON equipment guide: links, discovery order,
  conventions (slots, units, deck-declare semantics), agent boundary,
  liquid-handling notes, `stop` semantics, Python-API gaps, and the generated
  `actions` catalog. No claim, no hardware I/O.
- **`GET /plans/actions`** — the proposable action catalog: every action name,
  its `idempotent` flag and its JSON argument schema, generated from the same
  Pydantic models that validate a proposed step. Catalog membership is *not*
  live readiness; cross-check `/status.allowed_actions`.

## Deployments

One process per robot; everything that distinguishes an instance is an
environment variable read at startup. Two instances run on the lab Windows PC
from the **same code**, so the route surface, refusal codes and schemas below
are identical on both:

| instance | `OT2_EQUIPMENT_ID` | port | notes |
|---|---|---|---|
| HTE | `ot2_hte` | 8020 | runs real campaigns |
| Complexation | `ot2_complexation` | 8021 | the instance used for hands-on testing |

What genuinely differs per instance is configuration, not API: equipment id
and name, `OT2_HOST_ALIAS` / `OT2_HTTP_BASE_URL` (which robot, and by which
network path), and the three state-file paths
(`OT2_PLATE_STATE_PATH`, `OT2_DECK_STATE_PATH`, `OT2_TIP_STATE_PATH`), which
**must** be distinct or the two gateways corrupt each other's plate, deck and
tip records. Deck contents, loaded labware and installed pipettes differ too
and are readable only from `/status` — never assume one robot's deck for the
other. Auth posture (`OT2_REQUIRE_LOGIN`, `OT2_TRUST_LOCAL_UI`,
`OT2_CORS_ORIGINS`), the optional chat assistant
(`OT2_ASSISTANT_ENABLED`) and the robot profile (`OT2_ROBOT_MODEL`, default
`OT-2`) are also per-process; read `details.control_auth`, `details.ui_mode`
and `details.gateway_profile` on `/status` to see which posture a gateway is
actually running rather than assuming.

## What "primary operation" means here

**A protocol command in flight on the robot** — a motion, a liquid transfer, a
tip or labware move, or the `setup` that loads them. `activity` is observed
from the control plane, never derived from the health word:

- `running` — the gateway is inside exactly one command (`BUSY`, bracketed
  around the blocking call), **or** the robot-server reports a run the gateway
  deliberately did not seize (`EXTERNAL_CONTROL`).
- `unknown` — transport died during a non-idempotent command
  (`UNKNOWN_OUTCOME`), the robot is unreachable, or a software stop has been
  latched but not yet confirmed. Whether the robot is still moving is exactly
  what is not known.
- `idle` — everything else, **including dry run**. A simulated device reports
  its real (idle) activity; readers exclude simulations, devices do not
  self-censor.

An open robot-server run is *not* evidence of motion: the HTTP transport keeps
a run open between commands, so `run_active` alone is never treated as
`running`.

`activity_since` stamps the start of the **current** span only, and is
re-stamped only when the value actually changes.
`metrics.cycles_total` counts **completed protocol commands** since this
gateway process started — increment happens on success only. A poller on a
60 s interval will miss whole commands, so use that counter for utilization,
not sampled `activity`.

## Health vs. activity (STATUS_SPEC §2.2 / §2.3)

`equipment_status` answers "is it fit for a run"; `activity` answers "is it
running". They are answered independently. The internal service state maps to
the wire as:

| internal state | `equipment_status` | `activity` |
|---|---|---|
| `REQUIRES_INIT`, `CONNECTING` | `requires_init` | `idle` |
| `READY` | `ready` | `idle` |
| `BUSY` | `busy` | `running` |
| `EXTERNAL_CONTROL` | `busy` | `running` |
| `PAUSED` | `degraded` | `idle` |
| `DRY_RUN` | `dry_run` | `idle` |
| `UNKNOWN_OUTCOME` | `unknown` | `unknown` |
| `ERROR` | `error` | `idle` |

Two overrides sit on top of that table:

- **Robot unreachable** (and not dry run) forces `equipment_status: "unknown"`
  and `activity: "unknown"`, whatever the session state says — the gateway is
  fine, the hardware cannot be reached, so its state cannot be determined.
  `message` names the address and since when; `details.robot` carries the
  readback age.
- `CONNECTING` is deliberately `requires_init`, not `busy`: the SSH + protocol
  API init legitimately takes minutes on an OT-2, and reporting `busy` would
  make a slow-but-healthy boot indistinguishable from real work.

`required_actions` is the short list of what a human must do: `["startup"]`
when uninitialised (or after a failed startup with no session),
`["manual_reconcile"]` after an unknown outcome, `["reconcile"]` for an
`ERROR` that still has a live session, and `[]` when the robot is simply
unreachable (nothing this API can do about that).

## Claims and gating (STATUS_SPEC §5)

Claims are **cooperative coordination, not authentication**. They are held in
memory and do not survive a gateway restart.

1. `POST /control/claim` with `{owner, session_id, ttl_s, takeover?}` →
   `{claim_token, heartbeat_interval_s, expires_at}`. `ttl_s` is floored at
   5 s; `heartbeat_interval_s` is half the TTL, floored at 1 s.
2. `POST /control/heartbeat` with `X-Claim-Token` before each expiry. A
   401 means the claim is gone — stop driving and re-lock your controls.
3. `POST /control/release` when done (204, idempotent).

Re-claiming with the **same** `owner` + `session_id` is idempotent and keeps
the token. A claim held by a *different* owner is a **409** with `claimed_by`
and a `Retry-After` header — never taken over. `takeover: true` supersedes a
claim held by the **same owner** (the operator's other tab, or a reloaded
page that stranded its token); it mints a *fresh* token, so the superseded
page's next heartbeat 401s.

**What the claim gates.** Every `/control/*` route (including
`deck.declare`, `tips.*`, `plate.*` and the metadata-only ones), plus
`/plans/{id}/approve`, `/plans/{id}/execute`, `/plans/{id}/abort`,
`DELETE /plans/{id}`, and `/assistant/chat*`. Without a valid
`X-Claim-Token` these return **423** with `{detail, claimed_by,
retry_after_s}`. Everything else — `/`, `/health`, `/status`, `/docs/agent`,
`/plans` (list, read, propose, revise), `/plans/actions`, `/labware*`,
`/assistant/health`, `/openapi.json` and this documentation — is open and
side-effect-free.

**Identity (`OT2_REQUIRE_LOGIN`).** Optional and off by default. When on, the
claim — the chokepoint every motion endpoint already sits behind — requires a
verified principal, and the verified identity **overrides** the body's
`owner`, so `details.claimed_by.owner` names a real principal rather than a
string the caller typed. Two credentials, checked in order, with no external
auth service contacted:

| credential | trusted when | becomes |
|---|---|---|
| `X-Auth-User` | the request also carries `X-Edge-Key` **or** `X-Edge-Auth` matching `OT2_EDGE_SECRET` | the person's own name |
| `X-Api-Key` | constant-time match against `OT2_API_KEYS` | `api:<name>`, may claim |
| `X-Api-Key` | constant-time match against `OT2_PROPOSER_KEYS` | `api:<name>`, **propose-only** |

A missing credential with login on is **401** `{error: "login_required",
hint}`. A propose-only principal asking for a claim is **403**
`{error: "propose_only_principal", hint}` — that single refusal is what keeps
the approval gate real, since approving and executing need nothing but a
claim token.

## The agent boundary

```
propose / revise / read plans        ← an agent may do this
approve / execute / abort / delete   ← claim-gated; a human at the panel
raw /control/*                       ← claim-gated; a human or a full-key workflow
```

An agent's reach ends at the proposal. Do **not** route around an unavailable
action by calling `/control/*` directly or by talking to the robot-server.

## Run lifecycle

1. **`POST /control/startup`** `{host_alias?, password, simulation}` — opens
   the control session (SSH REPL, or the HTTP run engine when
   `OT2_TRANSPORT=http`). Failure is **503** and records
   `last_error.code: "startup_failed"`. On process start the gateway may
   self-heal: it probes the robot and, only when reachable **and** idle,
   re-establishes the session in the background.
2. **`POST /control/setup`** `{labware, instruments, modules}` — the session
   recipe. Every `nickname` must be a non-reserved Python identifier (the
   names are shared with the SSH transport); a bad one is **422**. Tip racks
   named here register with the tip tracker automatically, preserving
   already-used tips across restarts.
3. **Commands** — `home`, `move_to`, `pick_up_tip`, `aspirate`, `dispense`,
   `drop_tip`, `move_labware`, the temperature-module pair, and the
   allowlisted advanced/module actions. Exactly one command runs at a time;
   a second concurrent request is refused (`another command is in flight`),
   and while `activity == "running"` every run-starting action is withheld
   from `allowed_actions`.
4. **`POST /control/pause` / `resume`** — control flow; `PAUSED` reports
   `degraded` and allows only `resume` and `shutdown`.
5. **`POST /control/shutdown`** — ends the session. The gateway then reports
   `requires_init`. Do not end an agent session with the device shut down
   unless you were asked to; release your claim and leave it connected.

**`POST /control/stop` is a software stop, not an emergency stop.** It is
offered only on the HTTP transport with a live session, stays reachable while
a command is in flight, and returns success only after a *stopped readback*.
It latches: a file beside the tip state (`OT2_STOP_STATE_PATH`) blocks
automatic reconnect, `reconcile` cannot clear it, and the only way forward is
inspect → `shutdown` → `startup`. There is no SSH interrupt channel and no
physical-e-stop guarantee.

## `last_error.code` — branch on the code, never the message

A closed set of five. *Which* command failed lives in `last_error.message`
and in the `control_action` audit row, not in the code.

| code | severity | meaning and recovery |
|---|---|---|
| `startup_failed` | error | could not reach or initialise the robot. Fix connectivity, then `POST /control/startup`. |
| `snapshot_failed` | warning | a deck/labware read failed. Non-blocking: `/status` keeps serving the last good `details.snapshot`. |
| `command_failed` | error | the robot rejected or failed a command. It did **not** take effect; safe to retry. |
| `command_transport_failed` | error | the link dropped during an **idempotent** command. Effect unknown but harmless to repeat. |
| `command_unknown_outcome` | critical | the link dropped during a **non-idempotent** command (`aspirate`, `dispense`, `pick_up_tip`, `drop_tip`, `move_labware`). Whether it happened is genuinely unknowable. |

`command_unknown_outcome` puts the device in `UNKNOWN_OUTCOME` /
`required_actions: ["manual_reconcile"]`. **Never retry automatically.** A
human inspects the deck, then `POST /control/reconcile` (optionally with a
snapshot body) clears it back to `ready`. Letting a plan clear its own
"did that actually happen?" state is exactly why `reconcile` is not a
proposable action.

## Preconditions and refusals

`allowed_actions` is rebuilt from live state on every call and is the same
helper the endpoints themselves consult, so the wire and the gate never
disagree. **Read `allowed_actions` before acting.** If an action is missing,
the POST will be refused.

Two live precondition refusals return **412** with a *structured top-level
body* (never wrapped in `{"detail": ...}`) and never touch `last_error`:

| refusal | raised by | body |
|---|---|---|
| tip unavailable | `pick_up_tip` against a tracked rack | `{detail, rack, well, tip_status, requested_sample_id, retry_after_s}`, plus `held_by` when the tip is on a pipette, and `channels` / `covered_wells` / `blocking_well` for a multi-channel pick |
| out of envelope | `aspirate` / `dispense` / `mix` volume vs the *attached* pipette | `{detail, pipette, requested_ul, max_ul, min_ul, retry_after_s}` |

`tip_status` is `new`, `empty`, `on_pipette`, or the sample id the tip already
touched. `force: true` overrides the contamination guard **only** — never the
absence of a tip. A tip on a pipette is refused with `force` too: drop or
return it first. Publish-side counterparts you can size a request against
before proposing it: `details.tip_racks`, `details.mounted_tips`,
`details.pipette_channels`, `details.pipette_volumes`.

The static half of the same envelope is enforced by the request schemas
(`Field(ge=, le=)`) and comes back as **422** — well offsets bounded to
±100 mm, deck coordinates to the robot's own X/Y travel and a conservative
218 mm Z, volumes to 1000 µL. Unknown body keys are **rejected**, not ignored
(`extra="forbid"`): a misnamed argument is a 422, not a silently different
command.

Coarse conflicts — no session, wrong state, action not allowed right now,
another command in flight, a stale run, no plate loaded — are **409**.

### Refusal codes at a glance

| status | meaning |
|---|---|
| 401 | login required, or an unknown/expired claim token on `heartbeat` |
| 403 | propose-only credential attempting to claim or control |
| 404 | unknown plan, unknown labware `load_name`, or a UI/labware request that did not come through the auth edge |
| 409 | state conflict, claim held by another owner, plan-state/hash error |
| 412 | live precondition — tip unavailable or out of envelope (structured body) |
| 422 | schema/validation failure, including unknown body fields and unknown plan actions |
| 423 | missing or invalid `X-Claim-Token` (also: approving a plan with no live claim) |
| 502 | upstream robot HTTP failure (lights) or assistant provider failure |
| 503 | startup failed, or the assistant is not configured |

## Protocol / plan execution

Plans are the only sanctioned way for an agent to get work onto this robot.
They are held **in memory** and die with the process, on purpose: a claim does
not survive a restart, so an approval must not either.

1. **`POST /plans`** `{steps: [{action, args}], created_by?, notes?}` → a
   **draft**, and a `step_hash`. No claim needed. Every step's `args` are
   validated against the same request model the matching `/control/*` route
   uses, so a bad volume is refused while it is still text. An unknown action
   or bad args is **422**. Actions come from `/plans/actions`.
2. **`PUT /plans/{plan_id}/steps`** replaces the step list, which recomputes
   the hash and drops the plan back to `draft` — any prior approval is
   discarded by design.
3. **`POST /plans/{plan_id}/approve`** `{step_hash}` — the human gate.
   Claim-gated. The hash must match what the reviewer was shown; a mismatch is
   **409**. The approval records the claim's owner and session and expires
   after **600 s**.
4. **`POST /plans/{plan_id}/execute`** — claim-gated, and the live claim must
   be the *same owner and session* that approved. Runs one step at a time,
   re-checking `allowed_actions` before **every** step, so a plan approved
   against a ready robot cannot fire into one that has since faulted or been
   seized. Fail-fast: the first refusal or error halts the plan, marks the
   rest `skipped`, and sets `halt_reason`.
5. **`POST /plans/{plan_id}/abort`** stops a live plan; **`DELETE
   /plans/{plan_id}`** dismisses a settled one (`executed` / `failed` /
   `aborted` only — anything else is **409**).

Every plan read carries `executable` and `blocked_reason`, computed device-side
so the review UI and an agent see the same answer, plus
`non_idempotent_actions` — the steps that cannot be safely repeated after a
transport loss.

`startup`, `shutdown`, `pause`, `resume`, `stop` and `reconcile` are
deliberately **not** proposable: they are bring-up, control flow over a
running plan, or the very adjudication a plan must not perform on itself.

Two approval events are emitted to the lab history: `plan_approved` (who
agreed, to what hash) and `plan_executed` (what became of it).

## Conventions

- Volumes in microliters, coordinates and offsets in millimeters, temperature
  in Celsius. Timestamps are UTC ISO-8601.
- Deck slots come from `details.gateway_profile.slots` — `"1".."12"` on an
  OT-2, `A1..D4` on a Flex profile. Use the profile, never hard-coded slots.
- `deck.declare` is a **full-layout replacement, not a patch**: omitted slots
  are cleared.
- `tempmod.set` accepts a target and returns; the block ramps in the
  background. Watch current vs target on `/status`.
- `move_to` defaults to an arced path. `force_direct: true` omits the
  Z-retract waypoint and hands collision avoidance to the caller;
  constant-height XY travel means passing the *current* Z as the destination.
- Resolve loaded labware and pipettes from `details.snapshot.labwares` /
  `details.snapshot.pipettes`; `details.session_recipe` is not authoritative.
  Have the operator confirm the physical deck before use.

## Discovery

`llms.txt` (index) · `agent-docs` (this guide) ·
`agent-docs/api-reference` · `openapi.json` · `/docs` (Swagger UI) ·
`/redoc` · `/docs/agent` (JSON guide) · `/plans/actions` (action schemas) ·
`/status` (live state — read it, never infer it from this document).
