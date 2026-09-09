# Central-server dashboard update prompt

Paste the following into the coding agent working in the central
`ac-organic-lab` repository. The gateway change is on
`feat/http-actions-assistant-safety` in
`https://github.com/cyrilcaoyang/opentrons-server`.
Pin its fetched commit when starting the work, and report that SHA.

---

Update the central `ac-organic-lab` SDK, dashboard and dashboard assistant to
support the new opentrons-server HTTP action surface. Implement and test the
integration; do not stop at a plan.

First read this server checkout's AGENTS.md and its binding contracts. Fetch
the gateway's `feat/http-actions-assistant-safety` branch into an isolated
reference checkout; do not overwrite local changes or change a running device
checkout. Read its `docs/HTTP_API_COVERAGE.md`, `docs/FLEX_HTTP_SUPPORT.md`,
`docs/PACKAGE_REVIEW.md`, and these source files:

- `gateway/advanced.py`, `gateway/models.py`, `gateway/plans.py`
- `gateway/documentation.py`, `gateway/robot_profile.py`, `gateway/api.py`
- `control/flex_control.py`

Paths above are beneath `src/opentrons_server/`. The exact route/model/catalog
definitions are authoritative; do not reconstruct the list from this prompt.

Investigate the server's current implementation before editing. Likely entry
points, based on the reference checkout reviewed on the device PC, are:

- `skills/src/lab_skills/skill_catalog/liquid_handler.py`
- `skills/src/lab_skills/deck_slots.py`, relevant plan/session validation
- `api/app/assistant_control.py` and the dashboard assistant's instructions
- `web/src/lib/device-panels.ts`, `web/src/lib/ot2-deck.ts` and related views

Implement the following:

1. Match the typed gateway actions in the SDK catalog: blow-out, touch-tip,
   mix, air-gap, pipette settings/homing, heater-shaker, temperature, magnetic
   module and thermocycler actions, comment and delay. Add optional dispense
   `push_out`. Match units, bounds, optional fields and exact endpoint paths.
   These are gateway operations, not arbitrary Python execution.
2. Expose equipment guidance from GET `/docs/agent` to the dashboard assistant.
   Use `/plans/actions` and `/openapi.json` for schemas; still check live
   `/status.allowed_actions`, identity, activity and errors. Gracefully report
   missing capabilities on older gateway deployments. A source checkout is
   not evidence that a running gateway has been updated.
3. Preserve the human review/approval, claim and interlock boundaries. The
   assistant may read and propose; never add approve/execute/stop tools or
   hand it an operator credential. Do not retry unknown outcomes or reset
   metadata to get around an interlock. Read observed run labware/pipette
   references; the setup recipe is not authoritative.
4. Preserve `force_direct`, `speed`, `minimum_z_height`, and explicit zero
   offsets. A direct move has no inserted retract waypoint. Constant-height
   XY travel requires unchanged destination Z. Never silently add a home,
   retract, or different motion path. Preserve existing tip selection/tracking.
5. Handle `/control/stop` as an operator-only software stop of the owned HTTP
   run. The device's embedded panel already supplies STOP RUN; preserve the
   dashboard's framing rather than duplicating that panel. If a separate
   dashboard stop control is needed, keep it usable during a pending command
   and carry the holder's claim. Acknowledged stopped readback is required;
   failure is unknown. This is not a physical emergency-stop guarantee or a
   guarantee that every heater/shaker is off. Plan abort cancels later steps;
   it does not interrupt a command already started.
6. Add model-aware Flex support without changing either OT-2 registration:
   alphabetic A1-D4 locations, full 1/8-channel heads, gripper moves and explicit
   trash configuration. Advertise Flex actions only for a Flex-capable gateway.
   Direct absolute gripper moves default to direct axis movement; relative
   gripper dz=0 retains height. Do not enable 96-channel/partial layouts or
   assume the sample-prep workflow's 250 mm safe Z fits this gateway's bounds.
   Do not edit the separate sample-prep repository in this task.
7. Account for the assistant integration changes: gateway chat requires the
   caller's valid X-Claim-Token; an OPENAI_API_KEY configuration needs an
   explicit OT2_ASSISTANT_BASE_URL and a compatible model. Gateway assistant
   configuration is distinct from the central dashboard assistant's config.

Use mocks, fixtures and simulated status for verification. Add meaningful
tests for schema parity, capability/version differences, refusal in forbidden
states, direct-motion preservation, and agent/operator permissions. Run the
server repo's required Python/frontend checks and update its docs.

Do not actuate robots, alter production credentials, or restart device services.
Gateway deployments on the Windows device PC are separate release steps; HTE
is not a test robot. Follow the server repo's branch/review/deployment policy,
and report the commit/PR, tests, compatibility limits and deployment steps.
