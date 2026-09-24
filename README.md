# opentrons-server

Python tools and a STATUS_SPEC v1.2 REST gateway for Opentrons robots.
OT-2 supports SSH or HTTP; the opt-in Flex gateway uses HTTP.
The Python distribution is `opentrons_server`.

This repo conforms to lab status spec v1.2. The wire types come from the
shared [`sdl-lab-contract`](https://github.com/AccelerationConsortium/sdl-lab-contract)
package (pinned to the tag matching the spec revision) rather than a vendored
copy — see [Status envelope](#status-envelope-v12).

## What This Repo Contains

```text
opentrons-server/
├── src/opentrons_server/
│   ├── transport/              # SSH transport/session management
│   │   └── ssh_client.py       # paramiko-based SSHClient + SessionState
│   ├── control/                # OT-2 command wrapper and state readers
│   │   ├── ot2_control.py      # OT2Control (alias: OpentronsControl)
│   │   └── state_readers.py    # get_*_state, get_all_states, print_*_summary
│   ├── gateway/                # FastAPI equipment gateway
│   │   ├── api.py              # FastAPI app + create_app()
│   │   ├── service.py
│   │   ├── models.py
│   │   └── claims.py
│   └── labware/                # Container, well, pipette, and event models
│       ├── containers.py
│       └── events.py
├── workflows/                  # Prefect examples and helpers
│   ├── examples/
│   ├── prefect_tasks.py
│   └── README.md
├── tests/
│   ├── fixtures/
│   └── unit/
├── demo/                       # Preserved legacy demos
└── backup/                     # Preserved backup/reference code
```

`demo/` and `backup/` are intentionally preserved and are not part of the new
runtime layout.

## Documentation

Feature docs describe what is built and how it behaves; records capture what
happened on the bench; trackers hold open work. Completed plan docs are
retired into feature docs (history stays in git).

| Doc | Kind | What's in it |
|---|---|---|
| [`docs/HTTP_API_COVERAGE.md`](docs/HTTP_API_COVERAGE.md) | explanation | Plain-language guide to extending HTTP control: existing support, missing gateway integration, Python protocol execution, and blow-out behavior |
| [`docs/HTTP_TRANSPORT.md`](docs/HTTP_TRANSPORT.md) | feature | The HTTP run-engine transport: plain-language operator story, driving model (never-played setup run), enable/revert, plate-handoff safety spec, confirmed v8.7.0 command reference |
| [`docs/HTTP_SSH_PARITY.md`](docs/HTTP_SSH_PARITY.md) | feature | SSH ↔ HTTP method-by-method parity table, `/status` snapshot shapes per transport, bench-verification status |
| [`docs/TRANSPORT_TRADEOFFS.md`](docs/TRANSPORT_TRADEOFFS.md) | feature | Pros/cons of the two transports + current default and recommendation |
| [`docs/DECK_STATE.md`](docs/DECK_STATE.md) | feature | Normalized deck/labware state: model, run>repl>declared merge, mismatch flagging, declare endpoints |
| [`docs/AGENT_PROPOSALS.md`](docs/AGENT_PROPOSALS.md) | feature | Agent-proposed, human-approved plans: the propose/approve/run boundary, what can be planned, why this is *not* a "run authorization", and wiring an agent harness (Hermes) to it |
| [`docs/HTTP_DRIVE_VALIDATION.md`](docs/HTTP_DRIVE_VALIDATION.md) | record | 2026-07-14 hardware validation of the HTTP transport |
| [`docs/DEVICE_BRINGUP.md`](docs/DEVICE_BRINGUP.md) | runbook + records | Parameterized bring-up for any additional Opentrons gateway (install → verify → motion test → dashboard registration), plus the completed per-robot sign-offs |
| [`docs/NEXT_STEPS.md`](docs/NEXT_STEPS.md) | tracker | Open work items |
| [`users/readme_ssh_commands.md`](users/readme_ssh_commands.md) | user guide | `SSHClient` batch execution, timeouts, variable persistence |
| [`users/readme_opentrons_states.md`](users/readme_opentrons_states.md) | user guide | The state-readers functions (`get_deck_state`, …) and their dict shapes |
| [`workflows/README.md`](workflows/README.md) | user guide | Prefect workflow examples layout |

## Install

For the gateway/core package:

```bash
pip install -e .
```

For API-only deployments:

```bash
pip install -r requirements_api.txt
```

For workflow development with Prefect:

```bash
pip install -e ".[workflows]"
```

For tests/dev tools:

```bash
pip install -e ".[dev]"
```

## Core Concepts

- `transport.SSHClient` owns SSH connection state, shell mode, Python REPL mode,
  retries, batch execution, and closing sessions.
- `control.OT2Control` owns high-level OT-2 verbs such as protocol
  initialization, labware loading, homing, aspirating, dispensing, and moving
  labware.
- `control.state_readers` converts live Opentrons protocol/labware/pipette
  objects into plain dictionaries.
- `gateway.OT2Service` owns the device state machine used by the REST API.
- `gateway.api` exposes the FastAPI app for dashboard/status integration.
- `labware` contains optional stable models for containers, wells, pipette
  state, and append-only events.
- `workflows/` contains Prefect orchestration examples. The core gateway does
  not require Prefect to import or start.

## OT-2 onboard environment: no install required

The OT-2 runs **only** the official Opentrons SDK. `opentrons_server` is not
installed on the robot and never should be.

State introspection that needs to run where the live `protocol` object lives
(deck contents, loaded labware, pipette state) works by shipping the
`control.state_readers` module **source** over the SSH REPL each snapshot.
`state_readers.py` has no imports from `opentrons_server` — only stdlib
`datetime` and the Opentrons SDK that's already on the robot — so the source
is self-contained.

`gateway.OT2Service.refresh_snapshot` `exec()`s the source as a single
compiled string (the OT-2's interactive REPL would otherwise break on blank
lines inside function bodies). Two separate REPL invokes — one to define,
one to call and capture JSON — because the SSH command reader breaks on the
first `>>>` prompt it sees.

Result: bumping the gateway's package version never requires touching the
OT-2 itself.

## Gateway UI (`/ui`)

The gateway serves its own operator web UI at `http://<host>:<port>/ui` — the
full OT-2 control page (deck view with declared-vs-observed state, mismatch
flags, module telemetry, tip tracking, deck-declare picker, session controls,
lights) with **no dashboard, no auth stack, and no node tooling required**:
the prebuilt static bundle ships inside the package (`src/opentrons_server/ui_dist/`).

- **Enable/disable (`OT2_UI`):** served whenever the build output exists;
  set `OT2_UI=off` (or `create_app(ui=False)`) to run headless.
- **The trust switch (`OT2_TRUST_LOCAL_UI`):** who may reach the UI.
  - `false` (**production**): `/ui` and `/labware` answer **only** requests
    forwarded by the lab's auth edge — the edge injects `X-Edge-Key`
    (must match `OT2_EDGE_SECRET`, required in this mode) and
    `X-Auth-User` (the logged-in identity, stamped into the claim's
    `owner` so `details.claimed_by` names a person). Direct hits — from
    localhost, the tailnet, anywhere — get 404. Identity headers without
    a valid `X-Edge-Key` are ignored, so attribution can't be forged by
    direct `curl`.
  - `true` (**dev bypass, the default for a bare checkout**): UI served to
    anyone who can reach the port; no identity trusted. Startup logs a
    warning. Flip to `false` before any deployment that faces the lab.
  The effective state is visible at `/status` → `details.ui_mode`
  (`edge` = gated, `open` = trusted/dev, `off` = headless). Spec read surfaces
  (`/`, `/health`, `/status`) are never gated — the aggregator and SDK reach
  them directly as before. `/control/*` is not gated by *this* switch either;
  gating who may drive the hardware is a separate opt-in,
  [`OT2_REQUIRE_LOGIN`](#who-may-drive-the-hardware-ot2_require_login).
- **Claims:** the UI is claim-native. "Take control" acquires a cooperative
  claim (STATUS_SPEC v1.1) and heartbeats it in the background; every control
  button attaches `X-Claim-Token` and unlocks only while the claim is held.
  Releasing (or closing the tab) frees the device for workflows/the dashboard.
  A tab that reloads, or a second tab, arrives with a new `session_id` and no
  token, so it is refused — the stranded claim is one nobody can heartbeat or
  release until its TTL expires. For that case `POST /control/claim` accepts a
  gateway-local `takeover: true`, which supersedes a claim **held by the same
  `owner`** and mints a fresh token (the superseded page's next heartbeat gets
  401 and it re-locks). A different owner — an agent mid-plan, the dashboard's
  per-request claim — is never taken over; that stays a 409, and the UI offers
  its "TAKE OVER" button only when the holder is the same owner. Behind the
  auth edge that owner is the logged-in user, so takeover means *your* other
  session; in `OT2_TRUST_LOCAL_UI=true` dev mode every tab is
  `ot2-gateway-ui`, so it means any UI tab. The field is additive and
  defaults false, so spec-shaped `lab-skills` / dashboard bodies are unchanged
  and never take over by accident.
- **Labware catalog:** the deck-declare picker merges the authored catalog with
  `GET /labware`, a read-only summary of the official Opentrons definitions.
  That endpoint is populated when the optional extra is installed:

  ```bash
  pip install -e ".[labware]"   # opentrons-shared-data
  ```

  Without it the endpoint returns an empty catalog and the picker still works
  from its authored entries + free-text load names.

## Who may drive the hardware (`OT2_REQUIRE_LOGIN`)

**A claim is not authentication.** STATUS_SPEC §5 is explicit that claims are
*cooperative* — `POST /control/claim` takes an `owner` and a `session_id`,
both strings the caller invents. So by default, anyone who can reach the port
takes a claim under any name and drives the robot. That is the deliberate v1
posture (§11: "No auth at the equipment-repo level for v1. Tailscale ACLs gate
access") — **the security boundary is the network, not this service.**

`OT2_REQUIRE_LOGIN=true` moves that boundary into the gateway. Claim
acquisition then demands a *verified principal*, and since every motion
endpoint already sits behind the claim, gating that one chokepoint gates the
control plane. It is **off by default**, so existing and dev deployments are
unchanged.

```
OT2_REQUIRE_LOGIN=true
OT2_EDGE_SECRET=<shared secret>                    # for edge-injected identity
OT2_API_KEYS=solubility-workflow:k1                # machine principals that ACT
OT2_PROPOSER_KEYS=agent-a:k2                       # machine principals that only PROPOSE
```

At least one credential source must be configured, or startup fails — a
fail-closed device with no way in is bricked, not secure.

### Two credentials, no external auth service

Deliberately no auth-server dependency, so this is usable by anyone who
deploys the gateway and not only by this lab:

| credential | how it is verified | who it is for |
|---|---|---|
| `X-Auth-User` | trusted **only** with a matching `X-Edge-Key` *or* `X-Edge-Auth` (`OT2_EDGE_SECRET`) | humans behind any authenticating reverse proxy |
| `X-Api-Key` | constant-time match against `OT2_API_KEYS` | workflows and the SDK — machine principals that drive the robot |
| `X-Api-Key` | constant-time match against `OT2_PROPOSER_KEYS` | agents that draft plans for a human to approve |

The header pair is all a proxy has to produce, so this lab's Caddy edge,
oauth2-proxy, Authelia, nginx `auth_request` and Cloudflare Access all work
unchanged. `X-Auth-User` **without** a valid `X-Edge-Key` is ignored: the
device is directly reachable, so an unauthenticated caller must not be able to
assert an identity with one header.

Two spellings of the same secret are accepted: `X-Edge-Key` (what this
gateway's Caddy block sets) and `X-Edge-Auth` (what the dashboard's control
passthrough sends, matching the xArm). The passthrough reaches devices on their
tailnet `base_url` rather than through the edge, so it needs the alias — set
`DEVICE_EDGE_SHARED_SECRET` on the dashboard to the same value as
`OT2_EDGE_SECRET` to enable that path.

A verified principal always **overrides** the request body's `owner` — even
when the gate is off — so `details.claimed_by.owner`, and the `control_action`
rows the events exporter writes, name a real principal rather than a
self-declared string. An API key resolves to `api:<name>`, never to the key.

`GET /status` publishes the posture as `details.control_auth`:

| value | meaning |
|---|---|
| `identity` | a verified principal is required to claim |
| `claim_only` | a claim token is the only gate — coordination, not authentication |
| `open` | claims disabled too |

### What it does *not* cover

- **Read surfaces stay open.** `/`, `/health` and `/status` must keep
  answering or the aggregator marks the device unreachable — so deck, tip and
  plate state remain readable by anyone who can reach the port.
- **CORS defaults to `*`.** Any web page loaded in a browser that can route to
  this device can call the API. With the gate on it cannot obtain a claim, but
  tighten it anyway (STATUS_SPEC §10) by setting `OT2_CORS_ORIGINS` to your
  dashboard's origin — comma-separated, e.g.
  `OT2_CORS_ORIGINS=http://sdl2-pc-03-cytation.tail6a1dd7.ts.net`. The default
  stays `*` because keeping a device debuggable from a browser when the
  aggregator is down is the reason §10 exists; narrowing it is a deployment
  decision, not a library default.
- **Direct API callers need a key.** `lab-skills` and `execute_plan` reach
  `/control/*` on the tailnet with no credential, so they get 401 once the gate
  is on. Give them an `OT2_API_KEYS` entry before enabling it. The framed
  operator panel is unaffected — it goes through the edge, which injects both
  headers.
- **Give an agent a proposer key, never a full one.** Approving and running a
  plan need nothing but a valid claim token, and an `OT2_API_KEYS` entry can
  claim. Hand one to an agent so it can draft and you have handed it
  approve-and-run, making the human review decorative. `OT2_PROPOSER_KEYS`
  names the principal with strictly less power — it may draft and read, and
  `POST /control/claim` refuses it with 403 `propose_only_principal`. See
  [`docs/AGENT_PROPOSALS.md`](docs/AGENT_PROPOSALS.md).
- **The network is still the real boundary.** For a deployment that is not on
  a trusted tailnet, bind to loopback and put a reverse proxy in front — the
  pattern `kasa-tapo-services` uses — rather than relying on this gate alone.

### Developing the UI

The source lives in `ui/` (Vite + React + TypeScript + Tailwind). The build
output is committed so installs need no node:

```bash
# One-time
cd ui && npm install

# Dev loop against a local dry-run gateway (proxies /status, /control, /labware)
OT2_DRY_RUN=true uv run uvicorn opentrons_server.gateway.api:app --port 8020
cd ui && npm run dev

# Rebuild the committed bundle after changes
cd ui && npm run build   # writes src/opentrons_server/ui_dist/
```

## Chat assistant (optional)

A chat bubble in the operator panel that can read this robot and **propose**
work on it. Off unless you configure a key: with none, `/assistant/health`
reports why, the UI renders no bubble, and every other surface behaves exactly
as before.

It exists so the package stays self-contained — install this gateway alone, with
no dashboard, no central server and no agent harness, and you still get one.

**It cannot move the robot.** Its whole tool surface is four reads plus
`propose_plan`, the same door an agent harness comes through. A proposal is a
*draft*; approving and running it are claim-gated clicks in the **Proposed
plans** panel. See [`docs/AGENT_PROPOSALS.md`](docs/AGENT_PROPOSALS.md) for the
gate itself. It is also scoped to *this* robot by construction — the tools close
over one service instance, so there is no device to select and no shell.

### Turning it on

Any OpenAI-compatible endpoint works; OpenRouter is the default. Either put the
key in the environment, or drop a `.env` at the repo root:

```bash
# opentrons-server/.env   (git-ignored)
OPENROUTER_API_KEY=sk-or-v1-…
```

Read per request, so a key added here takes effect on the next poll — no
restart. Prefer this to `nssm set … AppEnvironmentExtra`, which **replaces the
entire variable block**: get it wrong and you wipe the three state paths that
stop two gateways from overwriting each other's plate and tip records.

> ⚠️ **A repo-root `.env` is shared by every gateway running from this
> checkout.** Fine for an API key. It cannot supply anything instance-specific
> — the reader takes an allowlist of assistant settings only, so
> `OT2_EQUIPMENT_ID`, `OT2_HOST_ALIAS` and the state paths can never come from
> it. Those stay in each service's own environment.

| Setting | Purpose | Default |
|---|---|---|
| `OPENROUTER_API_KEY` / `OPENAI_API_KEY` | provider credential; `OPENAI_API_KEY` requires an explicit base URL | unset → off |
| `OT2_ASSISTANT_MODEL` | any OpenAI-compatible model slug | `deepseek/deepseek-v4.1-flash` |
| `OT2_ASSISTANT_MODELS` | comma-separated models the chat panel's picker may switch to per turn (the default model is always offered); the gateway refuses any other slug with 422 | OpenRouter: `deepseek/deepseek-v4.1-flash,z-ai/glm-5.3-flash`; custom base URL: none |
| `OT2_ASSISTANT_BASE_URL` | provider endpoint | OpenRouter |
| `OT2_ASSISTANT_ENABLED` | `0` disables it even with a key — per-instance kill switch | `true` |
| `OT2_ASSISTANT_MAX_TOKENS` / `_TIMEOUT_S` | per-reply cap, per-request wallclock | 4096 / 60 s |
| `OT2_ENV_FILE` | explicit `.env` path | `./.env` |

Environment variables win over the file, including across the two API-key names.
When using `OPENAI_API_KEY`, set `OT2_ASSISTANT_BASE_URL` and a model supported
by that provider explicitly; the gateway will not send that key to OpenRouter
by default. Invalid numeric assistant settings disable chat with a diagnostic
from `/assistant/health` while the device service stays available.
A shared `.env` can carry the key while one instance turns itself off with
`OT2_ASSISTANT_ENABLED=0` in its own service environment.

**Pick a model that supports tool calling.** The assistant works by choosing a
verb from the plan catalog and filling in its schema. Nous Hermes models on
OpenRouter do *not* advertise tool support — point `OT2_ASSISTANT_MODEL` at one
and you get text-only replies that never propose anything.

### Checking it

```bash
curl http://<gateway>/assistant/health
# {"configured": true, "model": "deepseek/deepseek-v4.1-flash",
#  "models": ["deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3-flash"], "key_source": "file",
#  "env_file_searched": ["C:\\...\\opentrons-server\\.env"]}
```

`key_source` is `environment`, `file`, or `null` — the answer to "did it see my
key?", which is the only question worth asking when the bubble stays hidden.
The key itself is never returned. Chat requires the caller's valid
`X-Claim-Token`, rechecked between model/tool rounds: a proposal is only
useful to whoever holds the device, and it stops a passer-by spending the API
budget on a robot they do not control.

## Basic Python Control

```python
from opentrons_server.control import OT2Control

robot = OT2Control(host_alias="ot2_robot", simulation=False)

robot.setup_protocol(
    labware=[
        {
            "nickname": "tips",
            "loadname": "opentrons_96_tiprack_300ul",
            "location": "1",
            "ot_default": True,
        },
        {
            "nickname": "plate",
            "loadname": "corning_96_wellplate_360ul_flat",
            "location": "2",
            "ot_default": True,
        },
    ],
    instruments=[
        {
            "nickname": "p300",
            "instrument_name": "p300_single_gen2",
            "mount": "right",
            "ot_default": True,
        }
    ],
)

robot.get_location_from_labware("tips", "A1")
robot.pick_up_tip("p300")
robot.get_location_from_labware("plate", "A1")
robot.aspirate("p300", 100)
robot.get_location_from_labware("plate", "B1")
robot.dispense("p300", 100)
robot.drop_tip("p300")
robot.shutdown()
```

Legacy imports still work:

```python
from opentrons_server import OpentronsControl, SSHClient
```

## SSH Configuration

The SSH client can use either an SSH host alias or explicit environment
variables.

Example `~/.ssh/config`:

```sshconfig
Host ot2_robot
    HostName 192.168.254.50
    User root
    IdentityFile ~/.ssh/ot2_ssh_key
    StrictHostKeyChecking no
```

The AC OT-2 key is expected at `~/.ssh/ot2_ssh_key`. When a control request
passes `host_alias: "ot2_robot"`, the gateway first tries to resolve that alias
from this SSH config block and uses the `IdentityFile` above. If the config file
does not exist, the gateway treats `host_alias` as the hostname and uses
`root` plus `~/.ssh/ot2_ssh_key`.

The AC key is passphrase-protected. Pass the key passphrase in the startup
request body as `password`; do not commit the passphrase to this repository.

On the AC Windows gateway host, the service may run under `systemprofile`.
The SSH client still prefers the `sdl2` profile when it detects that case, so
`~/.ssh/config` and `~/.ssh/ot2_ssh_key` resolve to:

```text
C:\Users\sdl2\.ssh\config
C:\Users\sdl2\.ssh\ot2_ssh_key
```

Override this only if the gateway moves to another account:

```powershell
$env:OT2_SSH_HOME = "C:\Users\sdl2"
$env:OT2_SSH_CONFIG = "C:\Users\sdl2\.ssh\config"
```

Equivalent environment variables:

```bash
export HOSTNAME="192.168.254.50"
export USERNAME="root"
export KEY_FILE_PATH="$HOME/.ssh/ot2_ssh_key"
```

On PowerShell:

```powershell
$env:HOSTNAME = "192.168.254.50"
$env:USERNAME = "root"
$env:KEY_FILE_PATH = "$HOME/.ssh/ot2_ssh_key"
$env:OT2_SSH_COMMAND_TIMEOUT = "120"
```

## Operator setup (device PC)

The gateway needs to know how to reach the OT-2 (`OT2_HOST_ALIAS`)
and what passphrase decrypts the SSH key (`OT2_SSH_PASSWORD`). These
are read once at service startup in
`opentrons_server/gateway/api.py`:

```python
host_alias=os.environ.get("OT2_HOST_ALIAS"),
password=os.environ.get("OT2_SSH_PASSWORD", ""),
```

**These credentials belong to the device PC running the gateway, not
to any workflow repo.** Set them once via NSSM on the OT-2 gateway
host:

```powershell
# On the OT-2 gateway host (run from an elevated PowerShell)
nssm set ot2-gateway-hte AppEnvironmentExtra `
    OT2_HOST_ALIAS=192.168.254.50 `
    "OT2_SSH_PASSWORD=<your key passphrase>"
nssm restart ot2-gateway-hte

# Verify the values are visible to the service:
sc qenvironment ot2-gateway-hte
```

After that, workflow callers should invoke `/control/startup`
**without** `host_alias` or `password` in the body — the gateway
will use its own configured values. The fields remain in
`StartupRequest` for per-request override (useful for local dev
against a one-off gateway), but should not be supplied from
production workflow code.

Rotating the passphrase: re-run the `nssm set` command and restart
the service. Workflows never need to know.

Security notes:

- `AppEnvironmentExtra` is stored in the Windows registry under the
  service's NSSM key. It's readable by Administrators on that host.
  This is the standard place for service-account secrets on Windows.
- The same env vars work without NSSM (e.g. for local dev): set
  them in the PowerShell session before `uv run uvicorn ...` and the
  gateway picks them up.

Request-body precedence: `/control/startup` accepts optional
`host_alias` and `password` fields. Both use truthy override — an
empty string or missing field falls through to the env-var default.
So workflow code that sends `{"simulation": true}` (or with
`password=""`) keeps using the gateway's configured credentials;
only a non-empty value in the body actually overrides.

## Running multiple OT-2 gateways

The gateway is **one process per robot**. To front several OT-2s from the
same host, run several independent gateway instances, each on its own port,
pointed at its own robot, with its **own state files**. Everything that
distinguishes an instance is an environment variable read at startup in
`gateway/api.py` — nothing is hard-coded to a single robot:

| Env var | Purpose | Must differ per instance? |
|---|---|---|
| `OT2_EQUIPMENT_ID` | identity reported on `/status` (e.g. `ot2`, `ot2_complexation`) | **yes** |
| `OT2_EQUIPMENT_NAME` | human-readable name | recommended |
| `OT2_HOST_ALIAS` | which robot to SSH to (e.g. `sdl2-ot2-hte` / `sdl2-ot2-complexation`) | **yes** |
| `OT2_HTTP_BASE_URL` | robot HTTP API base (else derived from the host alias) | if not derivable |
| `OT2_PLATE_STATE_PATH` | per-well plate store JSON | **yes** — else instances clobber each other |
| `OT2_DECK_STATE_PATH` | operator-declared deck layout JSON | **yes** — same reason |
| `OT2_TIP_STATE_PATH` | per-rack tip tracking JSON | **yes** — same reason |
| `OT2_SSH_PASSWORD` | SSH key passphrase | per robot |
| `OT2_CORS_ORIGINS` | comma-separated allowed browser origins; defaults to `*` | no |
| listen port (uvicorn `--port`) | the gateway's own port | **yes** |

> ⚠️ **Distinct state paths are mandatory.** `OT2_PLATE_STATE_PATH`,
> `OT2_DECK_STATE_PATH` and `OT2_TIP_STATE_PATH` default to `./ot2_state.json`
> / `./ot2_deck_state.json` / `./ot2_tip_state.json` relative to the working
> directory. Both instances run from the *same* checkout, so with the defaults
> they share — and corrupt — each other's state. Always set all three to
> instance-specific paths. Tip state is the easiest to forget and the most
> expensive to get wrong: two robots sharing one tip store will each consume
> tips the other's tracker thinks are fresh.

Example: the HTE robot on port 8020 and the Complexation robot on 8021, both
gateways on `sdl2-pc-03` as separate NSSM services:

```powershell
# Instance 1 — HTE (matches equipment.yaml `ot2_hte`; robot sdl2-ot2-hte,
# 100.64.254.90, robot name "ot2cytation")
nssm set ot2-gateway-hte AppEnvironmentExtra `
    OT2_EQUIPMENT_ID=ot2_hte `
    OT2_HOST_ALIAS=sdl2-ot2-hte `
    OT2_HTTP_BASE_URL=http://sdl2-ot2-hte.tail6a1dd7.ts.net:31950 `
    OT2_PLATE_STATE_PATH=C:\ProgramData\ot2\ot2_state.json `
    OT2_DECK_STATE_PATH=C:\ProgramData\ot2\ot2_deck_state.json `
    OT2_TIP_STATE_PATH=C:\ProgramData\ot2\ot2_tip_state.json `
    "OT2_SSH_PASSWORD=<passphrase>"
# ... AppParameters: run uvicorn opentrons_server.gateway.api:app --host 0.0.0.0 --port 8020

# Instance 2 — Complexation (matches equipment.yaml `ot2_complexation`;
# robot "ot2training", reached through the USB bridge on the UPLC PC —
# not its Wi-Fi tailnet IP 100.64.254.91; see DEVICE_BRINGUP.md "Network paths")
nssm set ot2-gateway-complexation AppEnvironmentExtra `
    OT2_EQUIPMENT_ID=ot2_complexation `
    OT2_EQUIPMENT_NAME="Opentrons OT-2 (Complexation)" `
    OT2_HOST_ALIAS=169.254.40.81 `
    OT2_HTTP_BASE_URL=http://100.64.254.19:31951 `
    OT2_PLATE_STATE_PATH=C:\ProgramData\ot2_complexation\ot2_state.json `
    OT2_DECK_STATE_PATH=C:\ProgramData\ot2_complexation\ot2_deck_state.json `
    OT2_TIP_STATE_PATH=C:\ProgramData\ot2_complexation\ot2_tip_state.json `
    "OT2_SSH_PASSWORD=<passphrase>"
# ... AppParameters: run uvicorn opentrons_server.gateway.api:app --host 0.0.0.0 --port 8021
```

Set `OT2_HTTP_BASE_URL` explicitly whenever `OT2_HOST_ALIAS` is an
SSH-config alias rather than a resolvable hostname/IP — the HTTP probe
(`/health`, `/runs`, lights) derives its URL from the alias otherwise and
would fail to reach the robot even though SSH works. Both robots are on
the tailnet with MagicDNS names, so the tailnet URL is the stable choice.

Register each instance as its own entry in the dashboard's `equipment.yaml`
(`ot2` → `:8020`, `ot2_complexation` → `:8021`). The dashboard's skill catalog
and `LiquidHandlerTile` are keyed by `kind`/`id`, so both are handled with no
per-robot code.

---

## REST Gateway

Start the OT-2 gateway:

```bash
uvicorn opentrons_server.gateway.api:app --host 0.0.0.0 --port 8020
```

Or with `uv`:

```bash
uv run uvicorn opentrons_server.gateway.api:app --host 0.0.0.0 --port 8020
```

### Required Status Endpoints

The gateway exposes the AC equipment status contract:

- `GET /` - probe response with `equipment_id`, `equipment_name`, and
  `protocol_version`
- `GET /health` - liveness
- `GET /status` - side-effect-free equipment status envelope
- `GET /openapi.json` - generated by FastAPI

### API documentation for agents

- `GET /docs/agent` — JSON equipment guide: discovery steps, naming conventions,
  units, agent boundaries, Python API gaps, and generated proposal argument schemas.
- `GET /docs` — Swagger UI; `GET /redoc` — ReDoc reference.
- `GET /openapi.json` — complete gateway HTTP request and response schemas.
- `GET /plans/actions` — proposable action catalog. Check `/status`
  `allowed_actions` separately for current availability.

Append these paths to the gateway base URL, preserving any reverse-proxy prefix.
The guide requires no claim and never contacts the robot. Both the built-in
assistant and `tools/ot2_agent_mcp.py` expose it as `get_equipment_docs`.
An agent harness with a tool allowlist must include that read tool to use it.
Agents propose drafts; the operator approves and runs them in the panel.
Hardware workflows use `lab-skills` and the lab's plan approval process.

**Advanced control:** `/control/blow-out`, `/control/touch-tip`, `/control/mix`,
`/control/air-gap`, pipette configuration/homing and module actions now have
validated request bodies and matching proposal schemas. `/control/dispense`
accepts optional `push_out`; it never automatically adds a blow-out. See
[HTTP API coverage](docs/HTTP_API_COVERAGE.md) for the implemented surface and gaps.

**Stop:** `/control/stop` and the panel's **STOP RUN** button stop this gateway's
HTTP run through an independent connection. Success requires stopped readback;
a lost reply remains an unknown outcome. The claim gate stays in force. Inspect
the robot, shut down the session and start a fresh one before further work.
The recovery latch persists beside the tip-state file as `ot2_stop_state.json`
(or `OT2_STOP_STATE_PATH`); restarting the gateway does not clear it. This is a
software stop, with no SSH interrupt or hardware emergency-stop guarantee.

**Flex:** a separate gateway process can select `OT2_ROBOT_MODEL=Flex` with
`OT2_TRANSPORT=http`. The default remains OT-2. Flex includes alphabetic deck
slots, full 1/8-channel pipetting, gripper actions and explicit trash-bin setup.
Startup checks robot identity and required HTTP command schemas before creating
a run. See [Flex support and sample-prep mapping](docs/FLEX_HTTP_SUPPORT.md).

The documentation describes the build serving the request. A checkout change
becomes available on a running gateway only after a separate deployment.
For the corresponding central SDK/dashboard work, use the
[server integration handoff](docs/DASHBOARD_UPDATE_PROMPT.md). The
[package review](docs/PACKAGE_REVIEW.md) records the corrected defects and
remaining limits.

### Read status

Example:

```bash
curl http://127.0.0.1:8020/status
```

Before startup, `/status` should report:

```json
{
  "equipment_id": "ot2",
  "equipment_kind": "liquid_handler",
  "equipment_status": "requires_init",
  "activity": "idle",
  "required_actions": ["startup"]
}
```

### Status envelope (v1.2)

`equipment_status` answers *is this robot healthy and fit for a run*;
`activity` answers *is it working right now*. They are independent, and the
gateway derives each from its own observation — never one from the other.

**Primary operation.** For this liquid handler, the primary operation is **a
protocol command in flight on the robot** — a motion, a liquid transfer, a tip
pick-up or drop, a labware move, or the `setup` that loads them. `activity` is
`running` for exactly the span of such a command, and for a robot-server run
the gateway deferred to (see `external_control` below). `activity_since` is the
instant the current span began, so a reader can recover a command's true
elapsed time without timing its own polls.

`metrics["cycles_total"]` counts the protocol commands this process has
completed. It matters because most OT-2 commands are far shorter than the
dashboard's 60 s poll: an `activity` series sampled at that cadence does not
undercount them, it misses them outright (STATUS_SPEC §2.3.1). The poll-to-poll
delta of this counter is the accountable number. It resets on gateway restart,
by contract — a reader seeing it decrease treats that as a restart, not as
negative usage.

| Service state | `equipment_status` | `activity` | Notes |
|---|---|---|---|
| `requires_init` / `connecting` | `requires_init` | `idle` | REPL + protocol-API init legitimately takes minutes; nothing is driving the robot |
| `ready` | `ready` | `idle` | Initialized, no command in flight |
| `busy` | `busy` | `running` | One protocol command in flight (`_run_action` brackets it exactly) |
| `paused` | `degraded` | `idle` | A paused protocol is not performing its operation; only `resume` / `shutdown` are offered |
| `external_control` | `busy` | `running` | A run the gateway found on the robot-server at boot and deliberately did not seize |
| `unknown_outcome` | `unknown` | `unknown` | Transport died mid-command — whether the robot is still moving is precisely what we cannot determine |
| `error` | `error` | `idle` | With a live session, recovery actions (`home`, `move_to`, `drop_tip`, the bookkeeping verbs) stay advertised — a mounted tip after a failed step must have a way out; run-starting actions are withheld until the error clears (§2.2). Without a session, only `startup`. |
| `dry_run` | `dry_run` | `idle` | The simulation reports its real activity; readers exclude simulated devices from utilization |

While `activity` is `running`, `allowed_actions` omits every action that would
start a second concurrent command; `pause` and the bookkeeping verbs stay
listed. `degraded` + `running` never occurs on this device, because its only
degraded state is a paused protocol.

An open run is deliberately *not* treated as activity on its own: the HTTP
transport keeps a run open between commands (`docs/HTTP_TRANSPORT.md`), so an
open run is not evidence of motion.

**`equipment_version` is this gateway's version**, read from the installed
distribution's metadata (`pyproject.toml` is the only place the number is
written). It is *not* the robot's software version: the robot's `api_version`,
firmware, and system versions describe the attached hardware and live in
`details.robot`, alongside `robot_model` and `robot_name`.

Until 2026-08-07 the field was overwritten with the robot's `api_version` on
every probe, so a dashboard version column showed `8.7.0` for both robots and
the gateway's own release was invisible on the wire. If you are reading stored
history from before that date, an OT-2 `equipment_version` there is the
robot's, not the gateway's.

#### `last_error.code` taxonomy

STATUS_SPEC best practice #6 asks each device repo to define a **stable** code
set so clients branch on `code` rather than string-matching `message`. This
gateway's set is closed and small, declared once as `ErrorCode` / `ERROR_CODES`
in `gateway/models.py`, and enforced in `OT2Service._set_error` — the single
mutation site for `last_error`. A code outside the set raises rather than being
coerced, and a test walks the source AST to fail the build if any call site
passes a computed string.

| `code` | Severity | What happened | Recovery |
|---|---|---|---|
| `startup_failed` | `error` | The gateway could not reach or initialize the robot. | Fix connectivity, then `POST /control/startup`. |
| `snapshot_failed` | `warning` | A deck/labware read failed. Non-blocking — `/status` keeps serving the last good `details.snapshot`. | Usually self-clears on the next successful read. |
| `command_failed` | `error` | The robot rejected or failed a protocol command. It did not take effect. | Safe to retry. Any successful `/control/*` action clears it (§6.4); the panel's **CLEAR ERROR** (`POST /control/reconcile`) acknowledges it without motion. |
| `command_transport_failed` | `error` | The link dropped during an **idempotent** command. The effect is unknown but harmless to repeat. | Retry, or re-`startup` to re-establish the session. |
| `command_unknown_outcome` | `critical` | The link dropped during a **non-idempotent** command (aspirate / dispense / tips). Whether it happened is genuinely unknowable. | Operator reconciliation via `POST /control/reconcile` — never an automatic retry. |

The codes are deliberately **not** per-command. Until 2026-08-07 the gateway
emitted `f"{name}_failed"`, growing a new code per protocol verb
(`aspirate_failed`, `move_to_transport_failed`, …) — an open-ended space no
client could enumerate, and one where `aspirate_failed` vs `dispense_failed`
implied a difference in recovery that does not exist. Which command failed is
now in `last_error.message` (prefixed `"<command>: "`) and in the
`control_action` audit row. Stored history from before that date carries the
old per-command codes.

Per §6.3/§6.4: a 412 precondition refusal never populates `last_error`, and
`last_error` clears on the first successful operational `/control/*` call.

### Initialising the OT-2

If `/status` reports `equipment_status: "requires_init"` or the AC dashboard
shows **Needs init**, the gateway is running but has not opened an SSH/protocol
session to the OT-2 in this gateway process.

For this gateway, `ready` means `POST /control/startup` completed successfully:

- the gateway created an `OT2Control` instance;
- SSH to the OT-2 is connected;
- a robot-side Python session is running;
- the Opentrons protocol API was imported;
- a protocol context was created with `execute.get_protocol_api('2.21')`
  (`simulate.get_protocol_api('2.21')` when `simulation: true`).

Startup does not load labware, load instruments, or home the robot. Those are
separate `setup` / `home` actions once the gateway is ready.

On the current OT-2, creating the protocol context can take about a minute.
The gateway uses `OT2_SSH_COMMAND_TIMEOUT=120` by default so startup has enough
time to complete.

On the AC HTE deployment, the OT-2 gateway is reachable at:

```text
http://sdl2-pc-03-cytation.tail6a1dd7.ts.net:8020
```

Use `Invoke-RestMethod` on Windows when possible; it avoids `curl.exe` JSON
quoting issues.

```powershell
$base = "http://sdl2-pc-03-cytation.tail6a1dd7.ts.net:8020"

# 1. Check current state.
Invoke-RestMethod "$base/status"

# 2. Claim the OT-2 before sending control commands.
$claim = Invoke-RestMethod `
  -Method Post "$base/control/claim" `
  -ContentType "application/json" `
  -Body (@{
    owner = $env:USERNAME
    session_id = "manual-$([guid]::NewGuid())"
    ttl_s = 60
  } | ConvertTo-Json -Compress)

# 3. Initialise the gateway's OT-2 session. `host_alias` maps to a `Host`
#    entry in $HOME\.ssh\config, which should use ~/.ssh/ot2_ssh_key.
Invoke-RestMethod `
  -Method Post "$base/control/startup" `
  -Headers @{ "X-Claim-Token" = $claim.claim_token } `
  -ContentType "application/json" `
  -Body (@{
    simulation = $false
    host_alias = "192.168.254.50"
    password = "<key-passphrase>"
  } | ConvertTo-Json -Compress)

# 4. Confirm the gateway is ready.
Invoke-RestMethod "$base/status"

# 5. Release the claim when finished.
Invoke-RestMethod `
  -Method Post "$base/control/release" `
  -Headers @{ "X-Claim-Token" = $claim.claim_token }
```

If the OT-2 uses a different SSH `Host` alias, replace `ot2_robot` with that
alias. The alias must point at the OT-2 and use the AC key:

```sshconfig
Host ot2_robot
    HostName <OT2_IP_OR_HOSTNAME>
    User root
    IdentityFile ~/.ssh/ot2_ssh_key
    StrictHostKeyChecking no
```

The equivalent startup body is:

```powershell
Invoke-RestMethod `
  -Method Post "$base/control/startup" `
  -Headers @{ "X-Claim-Token" = $claim.claim_token } `
  -ContentType "application/json" `
  -Body (@{
    simulation = $false
    host_alias = "192.168.254.50"
    password = "<key-passphrase>"
  } | ConvertTo-Json -Compress)
```

PowerShell aliases `curl` and treats quotes differently from bash. If you need
`curl.exe`, build JSON with `ConvertTo-Json` and avoid trailing spaces after
backticks.

```powershell
$base = "http://sdl2-pc-03-cytation.tail6a1dd7.ts.net:8020"

$claimBody = @{
  owner = $env:USERNAME
  session_id = "manual-curl-$([guid]::NewGuid())"
  ttl_s = 60
} | ConvertTo-Json -Compress

$claim = curl.exe -s -X POST "$base/control/claim" `
  -H "Content-Type: application/json" `
  --data-raw $claimBody | ConvertFrom-Json

$startupBody = @{
  simulation = $false
  host_alias = "192.168.254.50"
  password = "<key-passphrase>"
} | ConvertTo-Json -Compress

curl.exe -X POST "$base/control/startup" `
  -H "Content-Type: application/json" `
  -H "X-Claim-Token: $($claim.claim_token)" `
  --data-raw $startupBody

curl.exe "$base/status"

curl.exe -X POST "$base/control/release" `
  -H "X-Claim-Token: $($claim.claim_token)"
```

If `curl.exe` returns a FastAPI `json_invalid` error, the request body reached
the gateway without valid JSON quoting. Re-run the `ConvertTo-Json` version
above, or switch to `Invoke-RestMethod`.

After a successful startup, `/status` may include a `snapshot_failed` warning if
the robot-side Python environment does not have `opentrons_server` installed.
That warning is non-blocking: `equipment_status: "ready"` and
`components.protocol.connected: true` are the readiness signals.

## Claim Then Control

Control endpoints use the v1.1 claim protocol. First claim the device:

```bash
curl -X POST http://127.0.0.1:8020/control/claim \
  -H "Content-Type: application/json" \
  -d '{"owner":"sdl2","session_id":"manual-cli","ttl_s":60}'
```

Use the returned `claim_token` on control calls:

```bash
curl -X POST http://127.0.0.1:8020/control/startup \
  -H "Content-Type: application/json" \
  -H "X-Claim-Token: <claim_token>" \
  -d '{"simulation": false}'
```

Heartbeat before the token expires:

```bash
curl -X POST http://127.0.0.1:8020/control/heartbeat \
  -H "X-Claim-Token: <claim_token>"
```

Release when finished:

```bash
curl -X POST http://127.0.0.1:8020/control/release \
  -H "X-Claim-Token: <claim_token>"
```

Useful control endpoints:

> **Addressing.** `labware_nickname` accepts the setup recipe's nickname, or —
> on a declared deck with no recipe — the deck slot (e.g. `"9"`); `pipette`
> accepts the recipe nickname or the mount (`"left"` / `"right"`). Slot- and
> mount-addressed gear is loaded into the control session on first use, from
> the operator's declaration and the robot's own instrument probe, so the
> declare-then-run flow needs no `/control/setup`. Request bodies are strict:
> unknown keys are rejected (422), never silently ignored.

- `POST /control/startup`
- `POST /control/shutdown`
- `POST /control/setup`
- `POST /control/home`
- `POST /control/pause`
- `POST /control/resume`
- `POST /control/move-to` — move a pipette without liquid handling. Body takes
  exactly one of `location` (labware nickname + well, same shape as
  aspirate/dispense) or `coordinates` (`{"x","y","z"}` absolute deck frame,
  mm), plus optional `speed` (mm/s), `force_direct`, `minimum_z_height`.
  Idempotent: a transport loss mid-move records an error (re-issue is safe),
  never `unknown_outcome`.
- `POST /control/pick-up-tip` — on a tracked tip rack, omitting `position`
  auto-picks the next available tip; omitting `labware_nickname` too
  auto-selects a tracked, tip-size-compatible rack (slot order), so a bare
  `{"pipette": "p300"}` works. `sample_id` / `force` drive the contamination
  guard (see *State and Labware Tracking*). Refusals — no rack can serve the
  pick, or an untracked rack named without a `position` — are HTTP 412 with a
  structured body, issued before any hardware motion.
- `POST /control/aspirate`
- `POST /control/dispense`

**Layer-1 limits (`gateway/limits.py`).** Motion coordinates, well offsets, and
volumes are bounded, in two tiers. **Static** bounds live in the request models
as `Field(ge=, le=, description=...)`, so a violation is a 422 before anything
moves *and* the numbers appear in `/openapi.json` and in the assistant's
`list_actions` — the limit an agent reads is generated from the limit that is
enforced, so the two cannot drift. X/Y come from `opentrons_shared_data`'s OT-2
robot definition; **Z is a documented conservative constant** (that definition
publishes no Z extent), so passing it means "not obviously impossible", not
"reachable". Well offsets are bounded to ±100 mm as a sanity check on intent —
real protocols use single-digit offsets, and an unbounded one is how a move ends
up hundreds of mm above a plate. **Live** bounds — what *this* pipette can
meter — cannot be schema constants, so `aspirate`/`dispense` refuse them
pre-motion with HTTP 412 (§6.1) and `/status` publishes
`details.pipette_volumes` so an agent can size a transfer before proposing it.
`OT2_MAX_Z_MM`, `OT2_MAX_WELL_OFFSET_MM`, and `OT2_MAX_PIPETTE_VOLUME_UL`
override the static bounds.
- `POST /control/drop-tip` — no body location → the fixed trash, on both
  transports (HTTP adopts the run's preloaded trash labware when the server
  provides one, else drops via the `fixedTrash` addressable area; registered
  when the run is created, so setup-less declared-deck sessions route there
  too). Naming the trash without a well (`labware_nickname: "12"`, `"trash"`,
  `"waste"`, …) is the same as naming nothing; any other explicit location
  must carry **both** `labware_nickname` and `position` — a half-specified
  one is refused rather than silently ignored.
- `POST /control/move-labware`
- `POST /control/tips/reset` — body `{"slot": str, "wells"?: [str]}`;
  (re)registers a tip rack with every tip fresh (a physical rack swap).
  `{"nickname": ...}` is accepted as a legacy alias.
  Metadata-only, works in any state including dry-run.
- `POST /control/tips/mark` — body `{"slot": str, "wells"|"columns": [...],
  "status": "new"|"empty"}`; corrects *part* of a rack, for the common case
  reset cannot express (used in some columns, full in others). Presence is
  assertable, contact is not, hence the two-value `status`. Also the recovery
  path when the gateway's view of a head disagrees with the bench: it releases
  any mount whose tips came from the wells it touches. Metadata-only.
- `POST /control/lights` — body `{"on": bool}`; toggles the deck (rail) lights
  by proxying to the robot's own `POST /robot/lights`. A convenience control:
  `lights.set` appears in `allowed_actions` and `components.lights`
  (`on`/`off`/`unknown`) is reported on `/status` whenever the robot is
  reachable, regardless of `equipment_status`.
- `POST /control/reconcile` — operator acknowledgement: clears an
  `unknown_outcome`, or an `error` while a control session is live (the
  panel's **CLEAR ERROR** button). Optional body replaces the cached deck
  snapshot after manual inspection.

## AC Organic Lab Dashboard Integration

In `ac-organic-lab/equipment.yaml`, configure the OT-2 as an HTTP device:

```yaml
- id: ot2
  name: Opentrons OT-2
  platform: hte
  kind: liquid_handler
  adapter: http
  protocol: "1.2"
  base_url: http://<gateway-host>:8020
  status_path: /status
  poll_timeout_seconds: 2.0
  do_not_call_connect: true
  tile: { w: 2, h: 2 }
```

Use `http://127.0.0.1:8020` only when the dashboard API and this gateway run on
the same host. If the dashboard is on a Linux server and this gateway is on a
Windows/Tailscale host, use the Windows host's Tailscale MagicDNS name or
Tailnet IP.

After changing `equipment.yaml`, restart the dashboard API on the Linux host:

```bash
sudo systemctl restart ac-dashboard-api.service
sudo systemctl status ac-dashboard-api.service --no-pager
```

### History events (`OT2_INGEST_URL`)

Optionally the gateway pushes domain events to the dashboard's history DB
(`POST /api/ingest/events`, the device-side exporter of ARCHITECTURE decision
#9). Off unless configured:

```
OT2_INGEST_URL=http://<dashboard-host>:8001/api/ingest/events
OT2_INGEST_DEVICE_ID=ot2_hte     # optional; defaults to OT2_EQUIPMENT_ID
```

It carries only what the aggregator's 60 s poll **cannot** see — an operation
that starts and finishes between two polls is invisible to it, not merely
undercounted (the reasoning STATUS_SPEC §2.3.1 gives for `cycles_total`):

| event | carries |
|---|---|
| `control_action` | `action`, `outcome`, `owner`, `duration_s`, `source: "device"` |
| `tip_pickup` / `tip_drop` | `rack`, `well`, `wells` (the whole covered column for a multi-channel head), `channels`, `sample_id` |
| `tips_reset` | `rack`, plus the `available_before` / `empty_before` / `touched_before` counts the refill discarded |
| `startup` / `shutdown` / `error` | `from_state`, `to_state`, `transport` |

Two things to know:

- **A dashboard-proxied click produces two `control_action` rows** — the
  passthrough's (recording its HTTP hop; carries `method` / `status_code`) and
  the device's (recording what the hardware did; carries `source: "device"`).
  The device row is authoritative for outcome and duration. Count one or the
  other, not both. A write made in this gateway's own `/ui` produces only the
  device row — which is the gap this closes, since that path never reaches the
  dashboard.
- **Tip lifecycle is otherwise current-state only.** `ot2_tip_state.json` says
  which tips are gone; these rows say when they went and for which sample, so
  usage is answerable per run.

Never emitted in dry run — a simulation must not enter the lab's history as
real work. Delivery is best-effort: a bounded queue and a daemon thread, so an
unreachable dashboard drops rows rather than stalling the control path.

## Robot reachability

The gateway's session state machine only moves when a command fails, so on its
own it cannot notice a robot that has gone away — until 2026-09-06 both
gateways reported `ready` for hours about robots nobody could reach. A
reachability monitor now rides the background refresh loop
(`OT2_RUN_REFRESH_INTERVAL`, 5 s): every tick probes the robot-server
(`GET /health`, `OT2_HTTP_TIMEOUT`), and **`OT2_UNREACHABLE_AFTER`
consecutive failures (default 3, ~15 s) declare an outage**. One timeout on a
robot busy loading a protocol is not an outage.

While the robot is unreachable, `/status` says so on every surface:

| field | value |
|---|---|
| `equipment_status` | `unknown` — STATUS_SPEC §2.1's rule for a healthy gateway that cannot reach its hardware. Never `error`, which means a fault the *reachable* device reported. |
| `activity` | `unknown` |
| `message` | `Robot unreachable at <url> since <t>; last seen <t>` |
| `components.robot` | `connected: false`, `state: unreachable`, `last_event_at` = last answered probe |
| `details.robot` | the last identity the robot reported plus `reachable: false`, `probe_url`, `last_seen_at`, `readback_age_s`, `unreachable_since`, `probe_failures` |
| `allowed_actions` | only what needs no robot: `shutdown` and the bookkeeping verbs (`plate.*`, `well.update`, `tips.*`, `deck.declare`) |
| `required_actions` | empty — nothing this API can do about it |

Endpoints agree with `allowed_actions` (§6.2): a robot-touching action is
refused immediately with "robot unreachable since …" instead of spending 20 s
on a dead socket, and the refusal never touches `last_error` (§6.3).
`details.robot.readback_age_s` is published even when the robot is fine, so a
reader can always tell a fresh observation from a stale one.

Two history events bracket an outage (`robot_unreachable`, `robot_reachable`
with `outage_s`), via the same exporter as control actions.

**Recovery is automatic.** The first successful probe ends the outage. The
gateway then asks the robot whether the session it held is still there
(`GET /runs/{id}` on the http transport; the socket on ssh). A robot that only
lost its network keeps its run and nothing changes. A robot that **rebooted**
has forgotten the run: the session is dropped (`session_lost` event) and the
existing `requires_init` self-heal re-initialises it on the same tick, with its
usual guards — never after an operator `shutdown`, never while an external run
is active, at most once per `OT2_SELF_HEAL_INTERVAL`. The same drop-and-rebuild
happens if a command meets `409 … is not the current run`, which is definitive
evidence the run died with the robot; previously that latched `error` until
someone cycled shutdown/startup.

For the dashboard to render this `unknown` as *unreachable* (and PyPoe to
alert on it) the registry entry must be marked gateway-fronted — see
`ac-organic-lab` `docs/STATUS_SPEC.md` §2.1.

## SSH Failure Policy

The gateway treats SSH failures differently depending on the operation:

- Read-only or idempotent actions may be retried or restarted.
- Non-idempotent liquid/physical operations are not blindly retried.
- If SSH fails during `aspirate`, `dispense`, `pick_up_tip`, `drop_tip`, or
  `move_labware`, the service enters `unknown_outcome`.
- `unknown_outcome` requires manual inspection/reconciliation before normal
  operation resumes.

The service exposes this through `/status` as `equipment_status: "unknown"` with
details in `last_error` and `details.service_state`.

## State and Labware Tracking

`control.state_readers` extracts live Opentrons state from protocol objects:

- deck slots
- loaded labware
- loaded modules
- mounted instruments
- pipette `has_tip` and `current_volume`
- tip-rack well `has_tip`
- well geometry and reported liquid volume when available

The `labware` package provides stable models for plate/tip-rack identity and
event tracking. These models are intentionally separate from the device gateway:
the gateway can summarize current deck/pipette state, while full sample
provenance should live in workflow state or a future inventory service.

### Tip lifecycle tracking

The gateway persists per-tiprack tip status (`gateway/tip_state.py`,
`./ot2_tip_state.json`, override with `OT2_TIP_STATE_PATH`) alongside the plate
and deck stores. Each tip well is `"new"`, `"empty"` (dropped), or a sample id
it has touched. The lifecycle is driven automatically and works on both
transports:

- **Racks are identified by the deck slot they sit in**, not by a recipe
  nickname. A tip rack carries no sample and no history worth naming — what an
  operator points at, and refills, is "the rack in slot 4". Three consequences:
  a rack registers from *any* deck source, so **declaring one on a slot starts
  tracking it** (it used to need a `/control/setup`, which is why a declared
  rack never appeared in the panel); tracking **survives a restart**, because
  the declared deck is persisted while the session recipe is in-memory; and the
  UI join always resolves, with no dependency on `labware.nickname`, which is
  null on every slot until a setup runs.
- `/control/setup` and `/control/deck/declare` both register the tipracks they
  place, and **boot re-registers from the persisted deck** — so a rack declared
  in an earlier session is tracked again without the operator re-declaring a
  slot they already declared. All three are non-destructive: a slot already
  tracked keeps its used-tip statuses.
  Protocol calls still address labware by **nickname** — only the tracker uses
  slots, and the two are resolved through the session recipe.
- `/control/pick-up-tip` validates the pick: fresh tips are free; a
  sample-touched tip is reusable only for the same `sample_id` (or with
  `force: true`); a well with no tip in it — `"empty"`, or `"on_pipette"` —
  is always refused, `force` included (force overrides the contamination
  guard, never the absence of a tip). Violations return
  HTTP 412 with `{detail, rack, well, tip_status, requested_sample_id}`
  before any hardware motion (`rack` is the slot). Omitting `position` auto-picks the next
  available tip (column-major, matching protocol-API order).
- **The rack updates when the tip leaves it, not when it is thrown away.** A
  pick marks its wells `"on_pipette"` — a hole, like `"empty"`, but one whose
  tip may come back — *before* the motion, so `/status`, the panel, and the
  next auto-pick never claim a tip that is riding the head. A pick that fails
  definitely is rolled back to the well's prior status; one that ends with an
  unknown outcome keeps the marking and flags the mount `uncertain`, because
  the unsafe assumption is that the tip is still seated.
- Aspirate/dispense record what the mounted tip touched — the tracked
  plate's real `sample_id` when the target well has one, else
  `<labware>_<well>` — plus `contacted_liquid: true`. Both go on the **mount**,
  not the origin well, which is a hole while the tip is up; they are written
  back to the rack if the tip is returned to it. Drop marks the origin well
  `"empty"`.
- **Which head holds which tips is persisted, not session state** — a tip stays
  physically on the pipette across a gateway restart, and the record of where
  it came from is the only thing that can put it back. `details.mounted_tips`
  survives a restart, so an interrupted run's tip can still be returned with
  its history intact.
- `/control/tips/{reset,mark}` release any mount whose tips came from the wells
  they touch: an operator asserting what is physically in a well contradicts a
  mount claiming that well's tip is elsewhere. Only the bookkeeping is dropped —
  a tip on the head stays there, and dropping it to the trash still works. This
  is the recovery path when the gateway and the bench disagree.
- **A drop into a tracked rack well is a relocation, not a disposal.** The
  destination wells take the tip and its history — the sample it last touched,
  `"new"` for a tip that never touched liquid (so it stays available), or
  `"unknown"` for an untracked head. The destination must be empty (dropping
  onto a seated tip is a crash and is refused with 412 pre-motion); the head's
  own origin wells are exempt, which is what makes returning a tip to its own
  well legal — and it now holds even after a restart, since the pick itself
  wrote the hole into the rack. The release descends to ~10 mm above the well bottom so the tip
  seats in the hole instead of being dropped from the well top
  (`OT2_TIP_RESEAT_BOTTOM_MM` to tune per rack geometry).
- `POST /control/tips/reset` takes `{"slot": "4"}` — the operator asserting a
  physical refill, which is never inferred (the gateway cannot see new tips go
  in, and a wrong "full" sends the head onto bare holes). `{"nickname": ...}`
  is still accepted and resolved through the session recipe.
- `/status` surfaces `details.tip_racks` **keyed by slot** (per-rack counts —
  `available` / `empty` / `on_pipette` / `touched` — plus the non-fresh wells
  and a `held_by` well → pipette map),
  `details.mounted_tips` (per-pipette rack / addressed well / covered `wells` /
  `channels` / `last_sample` / `contacted_liquid` / `origin_status` /
  `picked_at` / `uncertain`), and `details.pipette_channels`.

**Multi-channel pipettes** are tracked per *head*, not per addressed well. An
N-channel pipette sent to a row-A well takes N tips **downward in the same
column**, so an 8-channel pick at A1 consumes A1–H1: all eight wells are
validated, marked `"on_pipette"` on the pick, and set `"empty"` on drop — one
mount record speaks for all eight. Consequences
worth knowing:

- **Auto-pick steps by column.** With `position` omitted, an 8-channel pipette
  returns the first well whose *whole* span is free — A1, then A2 — never B1,
  which would put seven channels over holes whose tips are already on the head.
- **A partial column is not pickable.** One consumed well retires the column for
  a multi-channel head; the 412 body adds `channels`, `covered_wells`, and
  `blocking_well` naming the offender. Single-channel picks in that column are
  unaffected.
- **Multi-channel picks must be addressed at row A**; a lower start is refused
  rather than silently tracking the wrong wells.
- **Channel counts come from the robot**, via `GET /instruments` joined to the
  recipe's `mount` (an explicit `channels` on a `/control/setup` instrument entry
  wins, which is what makes this testable in dry-run). They are never inferred
  from the model name — `p20_multi_gen2` containing `multi` is a naming
  convention, not a fact about the hardware. A pipette whose count cannot be
  determined falls back to 1, i.e. pre-multi-channel behaviour; check
  `details.pipette_channels` if a rack's counts look eight times too optimistic.

## Workflows

Prefect code now lives outside the runtime package:

```text
workflows/
├── examples/
│   ├── sample_preparation.py
│   ├── analytical_workflow.py
│   └── high_throughput_screening.py
└── prefect_tasks.py
```

Install workflow dependencies with:

```bash
pip install -e ".[workflows]"
```

The workflow examples are for experiment orchestration across one or more
devices. They should call the gateway or `OT2Control`; they should not be needed
to start the gateway.

## Testing

Run focused gateway tests:

```bash
uv run --extra dev pytest tests/unit/test_gateway_service.py -q
```

Run all tests:

```bash
uv run --extra dev pytest tests -q
```

Compile/import sanity check:

```bash
python -m compileall src workflows tests/unit
```

## Notes

- This repo's OT-2 gateway conforms to the AC lab equipment status contract
  shape for `liquid_handler` devices.
- The HTTP run-engine transport (`OT2_TRANSPORT=http`) mirrors the full
  `OT2Control` (SSH) method surface — see `docs/HTTP_SSH_PARITY.md` for the
  method-by-method parity table and bench-verification status, and
  `docs/TRANSPORT_TRADEOFFS.md` for the pros and cons of each transport.
- `requirements_api.txt` is intended for gateway/API runtime dependencies.
- Prefect is optional and belongs to workflow development, not gateway startup.
