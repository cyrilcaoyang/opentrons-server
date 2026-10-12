# Agent rules — `opentrons-server`

**The canonical rules live in
[`ac-organic-lab/docs/AGENT_RULES.md`](../ac-organic-lab/docs/AGENT_RULES.md).**
Read that file first. It is binding for every agent operating on lab
infrastructure: safety and hardware, records and data integrity, protocols and
change control, chemicals, escalation.

This file adds only what is specific to **this repo** — the OT-2 gateway. It
may never weaken a rule in the canonical file. If a rule here ever appears to
conflict with one there, the canonical file wins: stop and flag the conflict.

Day-to-day working instructions (commands, layout, conventions) are in
[`AGENTS.md`](AGENTS.md), not here. This file is rules only.

---

## 1. This repo is a device, not an orchestrator

The canonical rule "never drive hardware directly — go through the `lab-skills`
SDK" is written for *callers*. This repo is on the other side of that boundary:
it **is** the device service the SDK calls. That inversion has consequences an
agent working here must hold onto.

1. **Do not add cross-device orchestration here.** No calls to the sealer, the
   xArm, the Cytation, or the dashboard's control passthrough. Anything that
   coordinates two devices belongs in workflow code that imports `lab-skills`,
   on the central server. The one permitted outbound call is the best-effort
   event push to the history DB (`OT2_INGEST_URL`), which is observability, not
   control.
2. **The gateway is the authority for OT-2 state, and must never lie about it.**
   If the robot's state cannot be determined, report `unknown` — never a
   convenient `ready`. Never let a run-blocking fault hide under a healthy
   top-level state (STATUS_SPEC §2.2), and never derive `activity` from
   `equipment_status` (§2.3). An agent "fixing" a red tile by softening what
   `/status` reports has broken the safety contract, not the tile.
3. **`/status` stays side-effect-free.** The aggregator polls it constantly. It
   must never connect, initialize, home, or move anything. If the hardware is
   not initialized, that is a *state* (`requires_init`), not a reason to
   initialize.

## 2. Driving the robot from this repo

4. **Never actuate a robot to test a code change.** This repo's tests run
   against `dry_run=True`, mocks, and fixtures — every test in `tests/unit/`
   (count in `AGENTS.md` §3), with no hardware. Hardware verification is a human-at-the-bench activity, planned in
   advance, on a robot known to be free. `pytest -m 'not integration'` is the
   default; assume it.
4a. **When a live check is genuinely needed, it may run on either robot**
   (Complexation `ot2_complexation`, :8021, or HTE `ot2_hte`, :8020), with
   the same rules for both: only when a human asks for that check, and only
   while the robot is free — idle, no plan or automation running, no claim
   held, no external run — confirmed from `/status` immediately before the
   first motion. Both robots run real work; a check that starts on a busy
   robot costs someone their experiment. (Changed 2026-10-12 at the owner's
   request; HTE was previously off limits.)
5. **Each gateway runs from its own deploy checkout, venv and PC; both track
   `main`.** HTE (:8020) runs from `C:\SDL_Deploy\ot2-hte` on the Cytation PC,
   Complexation (:8021) from `C:\SDL_Deploy\ot2-complexation` on the UPLC PC
   (since 2026-08-08). Editing a development tree changes nothing a robot
   runs; a change reaches a robot only when someone deploys it there (pull,
   `uv sync` if dependencies changed, restart), and only while that robot is
   free. Because both checkouts track the same branch, every merged change is
   a two-robot change, yet the two can sit on different commits: confirm what
   each one runs on the wire (`/status`, `/openapi.json`). `uv sync` can
   disturb a running service (see `AGENTS.md` §4).
6. **Never weaken the claim gate to make something work.** `enforce_claims`,
   `OT2_REQUIRE_LOGIN`, and the `X-Claim-Token` check are the concurrency and
   identity boundary. Turning one off to get a call through is the exact move
   rule 2 of the canonical file forbids. If a claim blocks you, report it.
7. **Tip and plate state are records, not scratch.** `ot2_tip_state.json` /
   `ot2_state.json` describe physical consumables. Do not hand-edit them to
   clear a contamination guard or an "empty tip" refusal; the guard is the
   point. A physical rack swap is expressed as `POST /control/tips/reset`.
8. **`unknown_outcome` is never retried automatically.** When transport dies
   mid-aspirate, whether liquid moved is genuinely unknowable. It requires an
   operator to look at the deck and reconcile. Code or agents that retry past
   it are fabricating a record of what happened.

## 3. What may leave this machine

9. **No run data in git.** Per-well contents, sample IDs, and measurements go
   to AnaliticaDB. Fixtures in `tests/fixtures/` use synthetic samples
   (`"caffeine"`, plate `"D"`) — keep it that way; never commit a real run's
   plate state.
10. **No secrets, no host credentials.** `OT2_SSH_PASSWORD`, API keys, and edge
    keys are service-environment values on the device PC. They never appear in
    commits, in `/status`, in logs, or in a README example. Tailnet IPs and
    hostnames that are already in the lab's committed `equipment.yaml` are
    fine; anything else stays local.

## 4. Escalation, specific to this device

11. **A plate or tip stuck on the deck is a stop-and-escalate.** Do not
    improvise recovery involving pipette or gantry motion. The canonical rule
    ("anything physically unexpected → stop and escalate") applies literally:
    halt, preserve state, tell a human.
