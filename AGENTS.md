# AGENTS.md — shared agent instructions (`opentrons-server`)

**Read this before proposing or editing anything.** It is the shared,
model-agnostic instruction file for every coding agent working in this repo.
Anything specific to one agent goes in that agent's own file (e.g.
[`CLAUDE.md`](CLAUDE.md)), never here.

This repo layers on the canonical base in
[`ac-organic-lab/AGENTS.md`](../ac-organic-lab/AGENTS.md). Read that first for
the lab-wide picture; this file adds only this repo's specifics and never
weakens anything inherited.

## 1. The binding contract — do not weaken

Three documents are binding and take precedence over everything here. Agents
reference them; they do not restate, reinterpret, or work around them.

- **[`AGENT_RULES.md`](AGENT_RULES.md)** — this repo's rules, which link back to
  the canonical [`ac-organic-lab/docs/AGENT_RULES.md`](../ac-organic-lab/docs/AGENT_RULES.md).
- **[`ac-organic-lab/docs/STATUS_SPEC.md`](../ac-organic-lab/docs/STATUS_SPEC.md)**
  — the device contract this gateway implements. **v1.2**, via the shared
  `sdl-lab-contract` package pinned to tag `v1.2.0`.
- **[`ac-organic-lab/docs/INTERLOCKS.md`](../ac-organic-lab/docs/INTERLOCKS.md)**
  — the four-layer safety model. This repo owns layers 1 and 2 (hardware limits
  in the Pydantic request bodies; the device state machine in
  `gateway/service.py`). Layers 3 and 4 live in `lab-skills` and project repos.

The short list agents most often need:

- **This repo is the device side of the boundary**, not a caller of it. It
  implements `/status` and `/control/*`; it never orchestrates other devices.
- Never report a state the gateway has not observed. `unknown` is the honest
  answer; a convenient `ready` is a contract violation (§2.2).
- Never derive `activity` from `equipment_status` — observe it (§2.3).
- `/status` is side-effect-free. Always.
- When something is irreversible, ambiguous, or uncovered: stop and ask a
  human. The absence of a rule is not permission.

## 2. What this repo is

An AC-conformant REST gateway fronting an **Opentrons OT-2** liquid handler.
Two instances of this one codebase run as separate services against two
different robots.

```
lab-skills / dashboard / agents          this repo                        robot
  ──────────────────────────────▶  gateway/api.py (FastAPI)
                                   gateway/service.py (OT2Service) ──SSH──▶ OT-2 REPL
                                   control/http_control.py        ──HTTP─▶ robot-server :31950
```

- **`src/opentrons_server/gateway/`** — the STATUS_SPEC surface. `api.py`
  (routes, auth, CORS), `service.py` (the state machine and `/status` builder —
  1.8k lines, the heart of the repo), `models.py` (this gateway's domain
  vocabulary + re-exported contract types), `claims.py`, `deck.py`,
  `tip_state.py`, `plate_state.py`, `events_exporter.py`.
- **`src/opentrons_server/control/`** — two interchangeable transports to the
  robot: SSH REPL (`ot2_control.py`) and the run-engine HTTP API
  (`http_control.py` / `http_run.py`). Their parity is a tested invariant; see
  `docs/HTTP_SSH_PARITY.md` and `docs/TRANSPORT_TRADEOFFS.md`.
- **`ui/`** — the operator SPA (React 19 + Vite + Tailwind 4), built into
  `src/opentrons_server/ui_dist/` and shipped inside the wheel. The dashboard
  **frames** this panel rather than reimplementing it, so it is the operator
  surface for both robots.
- **`docs/`** — start at `docs/DEVICE_BRINGUP.md` to bring a robot up,
  `docs/DECK_STATE.md` for the normalized deck model, `docs/OT2_TAILSCALE.md`
  for Tailscale/SSH on the robots themselves.

## 3. Working conventions

- **Environment: `uv`**, on Windows. From WSL the binary is
  `/mnt/c/SDL_Tools/uv.exe`; there is no `uv` on the WSL `PATH`.
- **Extras matter.** `uv sync --extra labware` — a plain `uv sync` **strips**
  `opentrons-shared-data`, which silently empties the `/labware` catalog and
  the UI's deck-declare picker. Same class of trap as the Cytation's
  `--extra plr`.
- **Tests: use `.venv.test`, not `.venv`.**
  `./.venv.test/Scripts/python.exe -m pytest tests/unit -q` — 532 tests, no
  hardware, about a minute. `.venv/` is the **running services'**
  environment; syncing or installing into it can disturb a live gateway (§4).
- **Never actuate hardware to check a change.** Everything in `tests/unit/` runs
  against `dry_run=True`, mocks, and `tests/fixtures/status_*.json`. Add a
  fixture rather than reaching for a robot. The one sanctioned exception is
  `tools/ot2-tip-lifecycle-check.ps1` below — an *operator-run acceptance*
  check, not a development loop. Reach for it to confirm a shipped change
  behaves on real hardware, never to find out whether your code works.
- **Bench tools live in `tools/` (PowerShell, run from the device PC).** They
  exist because the equivalent inline one-liner keeps failing: bash expands
  `$vars` before PowerShell sees them, and nested quotes inside `"$( ... )"`
  break its parser.

  | script | does | needs |
  |---|---|---|
  | `ot2-preflight.ps1` | one-screen state of both gateways before a session; flags a tip left on a head and whether the volume guard is actually live | nothing — read-only |
  | `ot2-tip-lifecycle-check.ps1` | picks one tip and returns it on Complexation, printing the rack at each step; plan-and-stop unless `-Run`; homes before releasing | resolves its own API key; **actuates** |
  | `ot2-enable-assistant.ps1` | toggles `OT2_ASSISTANT_ENABLED` for one gateway **without destroying the rest of its service env** | elevation (RDP session) |
  | `ot2-forget-stale-racks.ps1` | retires tracked tip racks whose slot the deck says holds no rack (a moved rack leaves one, and auto-pick will still send the head there); marks them empty, releasing any mount they explain; plan-and-stop unless `-Run` | resolves its own API key; metadata only, **no motion** — so unlike the tip check it does not refuse HTE |
  | `ot2-set-robot-url.ps1` | repoints one gateway at its robot (`OT2_HTTP_BASE_URL`, optionally `OT2_HOST_ALIAS`) preserving the other 13 env vars, then re-probes and reports whether the robot actually answered — a stale address hides behind a live session and only surfaces on the next restart; plan-and-stop unless `-Run` | `-Run` needs elevation (RDP session); the dry run does not |

  Run them by absolute path:
  `powershell -NoProfile -ExecutionPolicy Bypass -File <path>`. Two traps they
  encode, worth knowing before writing another: `nssm set AppEnvironmentExtra`
  **replaces** the whole variable block rather than appending, and `nssm get`
  writes to stderr even when it succeeds — which is a *terminating* error under
  `ErrorActionPreference = "Stop"` despite `2>$null`.
- **Live testing happens on Complexation only**, through the edge-gated panel
  at `http://100.64.254.6/ot2/complexation/ui/`. **Never HTE** (`ot2_hte`,
  :8020) — it runs real campaigns. This applies to any hands-on check: manual
  clicks, `curl` against `/control/*`, bench acceptance. The two deploy
  checkouts track the same branch, so a change reaches both robots; the
  *testing* does not.
- **UI:** `cd ui && npm run typecheck` / `npm run build`. The build must be
  committed as `ui_dist/` for the wheel to serve `/ui`.
- **Fail-fast style.** Do not add defensive code that swallows exceptions and
  hides failures — on this device a swallowed error becomes a robot whose state
  nobody can trust. Report truthfully.
- **Agent API discovery:** `/docs/agent` serves a read-only equipment guide;
  `/openapi.json` describes HTTP routes and `/plans/actions` supplies proposal
  schemas. The built-in assistant and agent MCP expose `get_equipment_docs`.
  Python transport methods are not automatically gateway endpoints; verify
  the route and plan catalogs before advertising a capability.
- **Robot profiles:** OT-2 is the default. A separate Flex instance sets
  `OT2_ROBOT_MODEL=Flex` and `OT2_TRANSPORT=http` before process startup;
  deck slots and schemas are selected at import. Use the shared profile,
  not hard-coded numeric slots. See `docs/FLEX_HTTP_SUPPORT.md` for limits.
- **Direct motion:** preserve `force_direct` on pipette moves. Flex
  `robot/moveTo` plans an arc; direct gripper motion uses `robot/moveAxesTo`
  or `robot/moveAxesRelative`. Substituting these changes the physical path.
- **Stop recovery:** `ot2_stop_state.json` beside the tip-state file (override
  `OT2_STOP_STATE_PATH`) blocks automatic reconnect after a software stop.
  Give each gateway its own state paths; do not delete this latch to recover.
- **Prefer reading source in `.venv/Lib/site-packages/`** over searching online
  for a dependency's usage (`sdl_lab_contract`, `paramiko`, `opentrons`).

## 4. Recurring pitfalls (project-specific)

- **This tree does not serve any robot.** Since 2026-08-08 each gateway runs
  from its own deploy checkout, with its own venv:

  | service | port | runs from |
  |---|---|---|
  | `ot2-gateway-hte` | 8020 | `C:\SDL_Deploy\ot2-hte` |
  | `ot2-gateway-complexation` | 8021 | `C:\SDL_Deploy\ot2-complexation` |

  Editing `Projects\opentrons-server` is therefore safe — it changes nothing a
  robot runs. **Deploying is an explicit act**, in the deploy checkout:
  `git pull` → `uv sync --extra labware` → `nssm restart <svc>`. Rollback is
  `git checkout <old-ref>` there plus a restart, and it moves one robot without
  touching the other.

  Before this, both services ran `uv run --project` out of *this* tree, so a
  restart — from a crash, a reboot, anyone's stray `uv run` — silently deployed
  whatever was on disk, committed or not. On 2026-08-07 both robots picked up
  uncommitted work mid-session and ended on *different* builds of it, because
  they restarted at different moments; `git stash` was unsafe for the same
  reason. Do not repoint a service back at this tree.
- **A commit is still not a deployment.** A deploy checkout only moves when
  someone pulls, so a merged fix can sit unshipped and the two robots can sit on
  different commits indefinitely. Confirm on the wire (`/status`,
  `/openapi.json`) before believing a bug is in the source, and check where a
  service actually runs from with `nssm get <svc> AppDirectory`.
- **Python is pinned to 3.12** by `requires-python = ">=3.10,<3.13"` — the
  upper bound is what makes `uv venv` choose 3.12 in a fresh deploy checkout
  (`.python-version` is gitignored, so it does not travel).
  `opentrons-shared-data` pulls `numpy~=1.26.4`, which has no wheel past cp312;
  without the pin a fresh venv picks 3.14, tries to build numpy from source, and
  fails for want of MSVC. This broke the first deploy-checkout build.
- **`AppEnvironmentExtra` replaces the whole variable block.** It does not add
  one variable — it replaces all of them, state paths included. Read the current
  block, append, write it back, and verify the variable count before restarting.
- **`uv sync` can break a running service.** If a release adds or bumps a
  dependency, uv must replace the console-script `.exe` the running service
  holds open, aborts the whole transaction on `os error 32`, and can leave the
  new dependency **not installed at all** while the service keeps running off
  memory-resident code. Stop only that service first, sync, start. Full
  recovery notes in
  [`DEVICE_PC_SETUP.md`](../ac-organic-lab/docs/DEVICE_PC_SETUP.md) §8.
- **Elevation and cache ACLs.** Never run `uv sync` from an elevated shell — it
  poisons the uv cache's ACLs for the service accounts. UAC prompts only appear
  in an RDP session, so an elevated command from a headless shell simply hangs.
- **Python version is pinned by the labware extra.** `opentrons-shared-data`
  pulls `numpy~=1.26.4`, which has no wheels past cp312. Do not bulk-upgrade
  this venv's Python.
- **Single-PC concentration.** xArm (8000), PlateLoc (8010), both OT-2 gateways
  (8020/8021), and Cytation 5 (8040) all live on `sdl2-pc-03-cytation`. One
  reboot takes out five workflow-critical services, and the USB-enumeration
  race on boot is a known failure mode for the serial devices.
- **Complexation's robot is reached through the UPLC PC, not its tailnet IP.**
  `ot2training` has no lab Ethernet; its Wi-Fi radio drops on its own after a
  reboot (`wlan0` disconnected, empty scan) and only a hard reboot brings it
  back. Since 2026-09-05 the gateway points at the `netsh` USB bridge
  `http://100.64.254.19:31951` (DEVICE_BRINGUP.md *Network paths*). Since
  2026-09-06 the gateway watches the path itself: three failed probes (~15 s)
  flip `/status` to `unknown` with `components.robot: unreachable` and
  `details.robot.readback_age_s`, robot-touching actions are refused up front,
  and a robot that rebooted during the outage gets its session rebuilt
  (README *Robot reachability*). Before that, `ready` and
  `details.robot.reachable: true` stayed frozen for hours after the robot
  vanished — do not trust envelopes from older builds on this point.
- **The SSH REPL breaks on the first `>>>` prompt.** Snapshot reads are sent as
  two separate `invoke`s routed through `compile()`/`exec()` for exactly this
  reason (`service.py` `_REMOTE_SNAPSHOT_*`). Do not "simplify" them into one
  multi-statement send.
- **`equipment_version` is the gateway's, not the robot's.** The robot's
  `api_version` lives in `details.robot`. These were conflated until
  2026-08-07; stored history before that date has the robot's number.

## 5. Memory & instruction policy

Inherited from the canonical base; the scope boundaries for this repo:

- **`AGENTS.md` (this file)** — durable, model-agnostic repo knowledge:
  conventions, commands, architecture facts, recurring pitfalls. When you learn
  one, update this file.
- **`AGENT_RULES.md`** — binding rules; changes only when a human asks.
- **`CLAUDE.md`** — Claude-Code-specific only. Nothing another agent needs.
- **Cross-repo or device-PC-wide facts** (the shared `sdl2-pc-03` layout, uv/
  NSSM behaviour, other repos' roles) belong in the agent's **global** memory,
  *proposed for approval* — not written into this repo.
- **Never** commit temporary debugging notes, stale TODOs, or one-off
  observations to any instruction file.

## 6. Safety protocol for edits outside this repo

Before editing anything outside this repository — another device repo,
`../ac-organic-lab`, `~/.claude`, service configs on the device PC — first show
the human the exact path, the reason, the proposed change, and whether it
affects only this repo or future global behavior. **Do not proceed until they
approve.** In particular, `../ac-organic-lab` is the central server's repo
mirrored here: contract changes are preferably made there, and any edit from
this PC must be coordinated so the two do not diverge.
