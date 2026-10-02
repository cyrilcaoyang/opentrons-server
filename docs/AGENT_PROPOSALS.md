# Agent-proposed plans

An agent may **propose** work on an OT-2. It may not run it. A human reviews
the proposal in the gateway UI, approves it, and runs it. This document
covers the shape of that boundary and how to wire an agent harness to it.

Implementation: `gateway/plans.py` (the gate), `tools/ot2_agent_mcp.py` (the
agent-facing MCP surface), `ui/src/components/AssistantBubble.tsx` (the human
approval popup).

## The boundary

| Operation | Route | Agent | Operator (holds the claim) |
|---|---|---|---|
| propose a plan | `POST /plans` | ✅ | ✅ |
| revise its steps | `PUT /plans/{id}/steps` | ✅ | ✅ |
| read plans / catalog | `GET /plans`, `GET /plans/actions` | ✅ | ✅ |
| **approve** | `POST /plans/{id}/approve` | ❌ 423 | ✅ |
| **run** | `POST /plans/{id}/execute` | ❌ 423 | ✅ |
| discard | `POST /plans/{id}/abort` | ❌ 423 | ✅ |

Approve, run and abort require `X-Claim-Token`. The token is minted by
`POST /control/claim` and lives in the operator's browser; it is never given to
an agent. So every path that moves the robot starts with a human clicking.

This matters more than it would for a purpose-built chat panel, because a
general agent harness has a shell, cron, subagents, and a chat interface
reachable from a phone. It can draft work at 3am. It cannot start it.

## Why the gate holds

Three properties, each with a test named after it in `tests/unit/test_plans.py`:

1. **The step list is hashed and the human approves the hash.** Revising a
   plan resets it to `draft` and discards any approval — it is a different
   plan that has never been reviewed. An agent cannot get a harmless plan
   approved and then swap the steps.
2. **Approval is bound to a claim session, not just a person.** The claim only
   stays alive while the operator's page heartbeats, so "a human approved
   this" decays into "a human is still here". A different tab is a different
   session and must re-review. An approval must be *started* within 10 minutes
   and is spent on use; once running, the plan continues for as long as the
   approving claim session stays live, however long it takes.
3. **`allowed_actions` is re-checked live before every step.** A plan approved
   against a ready robot cannot fire into one that has since faulted, paused,
   or been seized by an external run. The first refusal halts the plan and
   marks the rest `skipped` — never continue-past-error, because later steps of
   a pipetting sequence assume the earlier ones happened.

Plans live in memory and die with the process, deliberately: a claim does not
survive a restart, so an approval must not either.

### Not a "run authorization"

That term is taken. `ac-organic-lab/docs/AGENTIC_ELN_DESIGN.md` §12 defines a
**Run Authorization** as a campaign-level gate: it pins a merged commit SHA, a
protocol schema version and a compiled-package digest, revalidates inventory and
device readiness, and lands an immutable `authorization_id` in AnaliticaDB.

What this module produces is a **step approval** — one operator, holding the
claim, agreeing to one ad-hoc step list on one robot, to be started within ten
minutes.
It covers the work a run authorization does not: bring-up, homing, a manual tip
pickup, turning the lights off. The two compose; calling both "authorization"
would conflate a durable scientific record with an ephemeral operator gesture.

### Audit

Approvals are ephemeral, but the *fact* of them is not. Two events go to the
history DB via `OT2_INGEST_URL`:

| event | when | payload |
|---|---|---|
| `plan_approved` | a human approves | `plan_id`, `step_hash`, `owner`, `session_id`, `proposed_by`, `steps`, `non_idempotent`, `expires_at` |
| `plan_executed` | the run finishes or halts | `plan_id`, `step_hash`, `status`, `owner`, `outcomes`, `halt_reason` |

The per-step `control_action` rows already say what ran; these say **who agreed
to it, and to what**. Without them that fact died with the process — recoverable
only by inference from the step rows.

Note `plan_executed` carries the *approving* owner, read before execution: the
approval is spent by `execute()`, so a naive read afterwards would leave the
completion record with no one's name on it.

## What can be planned

`GET /plans/actions` returns the catalog with each action's JSON schema — the
authoritative list. It includes `platebalance.zero`, `platebalance.tare`, and
`platebalance.read`, so one approved plan can set a reference, dispense, wait,
and measure weight in order. Zero and Tare are non-idempotent; their step result
reports `sent_unconfirmed` after a serial write, not a verified reference
change. The read-step result carries the measured value, unit, stability and
observation time. Plan results are held in the gateway's ephemeral plan store;
they are not scientific records in BitacoraDB.

`tempmod.set` sets the temperature-module *target* and returns; it does not
wait for the ramp (a cool-down to 4 °C is minutes, and would blow the HTTP
command timeout). Current vs target is already on `/status`. Address the
module by `module` (recipe nickname or deck slot); omit `module` when exactly
one temperature module is declared.

Five control actions are deliberately **not** plannable:

| Excluded | Why |
|---|---|
| `startup` | carries an SSH credential; a bring-up step, not lab work |
| `shutdown` | lifecycle — ending a session is an operator decision |
| `pause` / `resume` | control flow over a running plan; must stay immediate, never queued behind other steps |
| `reconcile` | clears `unknown_outcome` — the state meaning "nobody knows whether that aspirate happened". A plan that could clear its own ambiguity would erase the signal a human is meant to adjudicate. |

**Everything else in the catalog is proposable, including the bookkeeping
corrections** — `tips.mark`, `tips.reset`, `plate.load`, `well.update`,
`deck.declare`. That an action asserts a physical fact the gateway cannot
observe does not make it operator-only: a proposal is a draft, and *approving*
it is the operator making the assertion. The distinction is worth stating
because an assistant got it wrong in the field, telling an operator that
`tips.mark` was "operator-only, I can't propose it" and leaving them to repair
a drifted tip rack by hand — while the action sat in `list_actions`, in
`PLAN_ACTIONS`, and in `allowed_actions` the whole time. The refusal came from
prose the model reads (the system prompt's operator-only clause, and
`TipsMarkRequest`'s own docstring, which reaches the model as the action's
schema description); both now say so explicitly, and
`test_bookkeeping_corrections_are_proposable` pins it. A drifted tracker is
precisely when a proposal is most useful — it is one reviewable step versus a
paragraph of instructions.

Step arguments are validated at proposal time against the same request models
the matching `/control/*` endpoint uses, so a malformed step is refused with a
422 naming the field while it is still text. Validation is **strict**: unknown
argument names are refused, not ignored — a live plan once proposed
`pick_up_tip {"slot": 9, "well": "A1"}` and Pydantic's default silently
dropped both keys, turning the reviewed step into a bare pick. Labware is
addressed by `labware_nickname` (the setup recipe's nickname, or the deck slot
on a declared deck), pipettes by recipe nickname or mount (`"left"`/`"right"`);
slot- and mount-addressed gear is loaded into the control session on first
use.

A settled plan is cleared from the panel with **Dismiss**
(`DELETE /plans/{id}`, claim-gated) — abort cannot do it, because a failed
plan is already terminal and abort leaves terminal plans untouched. Draft and
approved plans are *aborted* instead, so the proposing agent can observe the
rejection.

There are **two operator review surfaces**, same gate: the Proposed-plans
panel (every plan, every proposer) and the chat bubble's plan card (the plan
this chat just drafted). Both are claim-gated and both approve the step-hash
they rendered — the card re-fetches the live plan rather than trusting its
proposal-time preview, so a plan revised elsewhere still 409s. Neither is
agent-reachable; the assistant and the MCP server have no approve/execute
tools.

## Wiring up Hermes

`tools/ot2_agent_mcp.py` is a **standalone** stdio MCP server. It imports only
`mcp` and `requests` — never `opentrons_server` — and talks to the gateway over
REST, so it runs in a throwaway environment and the gateway's service venv is
never touched. That is not incidental: two live robots share one checkout and
one `.venv` on `sdl2-pc-03-cytation`, and installing `mcp` there would require
stopping both services (it pulls ~10 packages including pywin32).

It exposes eight tools — four reads, `propose_plan`, `revise_plan`, and two
plan reads. There is no approve/execute/abort tool, and
`tests/unit/test_agent_mcp.py` fails if one appears.

Register the Complexation robot:

```powershell
hermes mcp add ot2-complexation `
  --command uv `
  --args "run --with mcp --with requests C:\Users\sdl2\Projects\opentrons-server\tools\ot2_agent_mcp.py --base-url http://sdl2-pc-03-cytation.tail6a1dd7.ts.net:8021"
```

**Use the gateway's tailnet address, not the Caddy edge.** The edge
(`http://100.64.254.6/ot2/complexation/…`) runs `forward_auth` and returns
**401** for these paths, so every tool would fail. The gateway port itself
answers unauthenticated on the tailnet — the documented v1 posture, where
Tailscale ACLs are the gate (STATUS_SPEC §11). Complexation is `:8021`, HTE is
`:8020`.

The server probes `GET /` at startup and exits non-zero if the gateway is
unreachable — a silent, dead MCP server looks to a model like a robot with no
state, which is the wrong impression to give it.

> **Register Complexation only.** Hands-on OT-2 work goes through
> `http://100.64.254.6/ot2/complexation/ui/`. The HTE robot (`:8020`) runs real
> campaigns and is off limits — see `AGENT_RULES.md` rule 4a.

If the gateway runs with `OT2_REQUIRE_LOGIN`, pass `--api-key` (or
`OT2_MCP_API_KEY`) — and make it an **`OT2_PROPOSER_KEYS`** entry, not an
`OT2_API_KEYS` one.

That distinction is the whole gate. Approving and running a plan require nothing
but a valid claim token, so a credential that can claim can approve its own
proposal and execute it. An `OT2_API_KEYS` entry *can* claim — it is the
credential a workflow driver (`lab-skills`, `execute_plan`) legitimately uses to
be the operator. Hand one to an agent so it can draft, and you have also handed
it approve-and-run, leaving the human review decorative.

`OT2_PROPOSER_KEYS` names the same principal with strictly less power: it may
draft and revise plans and read status, and `POST /control/claim` refuses it
with **403 `propose_only_principal`** — which transitively closes approve,
execute, abort and every `/control/*` verb, since all of them need the token.

```powershell
# on the gateway host, alongside the existing OT2_API_KEYS
OT2_PROPOSER_KEYS=agent:<key>
```

## The operator's side

The **Proposed plans** panel appears at the top of the gateway UI whenever a
plan is open, and is hidden otherwise. Each plan shows its steps with
arguments, the hash being approved, and a ⚠ on any non-idempotent step
(`aspirate`, `dispense`, tip actions) — those cannot be safely repeated if the
link drops mid-step.

Take control, then **Approve these N steps**, then **Run**. Two clicks, not
one: approving records *what was reviewed*, which is what makes a later audit
meaningful, and it keeps the last action before a pipette moves from being a
single click on a screen nobody read.

If the plan changed between the panel rendering it and the click, approving
returns 409 and the panel says to re-read it. That refusal is the feature.
