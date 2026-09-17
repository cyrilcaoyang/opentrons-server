# Gibbie Flex HTTP gateway

This branch installs a dedicated `opentrons-server` instance beside the
read-only Gibbie monitor. It reuses the same operator SPA and proposal-only
assistant as the OT-2 gateways, selected at process start with the Flex robot
profile.

## Proposed deployment identity — approval required

Deploy this instance under the Gibbie user's Projects directory. Review the
diff and offline test results before authorizing installation. Committing or
pushing changes is a separate approval from deploying a reviewed working tree.

**Authentication routing is unresolved:** this profile requires
`OT2_TRUST_LOCAL_UI=false` and `OT2_REQUIRE_LOGIN=true`. Do not apply the
installer until an authenticated browser route and its credential provisioning
have been agreed. The existing UI uses an authenticated reverse proxy; it does
not provide its own login form. Direct backend `/ui/` requests return 404.

| Item | Value |
|---|---|
| SSH alias for host administration | `gibbie-pc` |
| Branch | `opentrons_flex_http` |
| Checkout | `C:\Users\sdl2\Projects\opentrons-flex-http` |
| NSSM service | `opentrons-flex-http` |
| Listen port | `8071` |
| Backend UI (proxy-only) | `http://sdl2-pc-04.tail6a1dd7.ts.net:8071/ui/` |
| Operator page | Pending authenticated proxy route approval |
| Robot server | `http://192.168.254.81:31950` |
| State directory | `C:\SDL_State\opentrons-flex-http` |
| Logs | `C:\SDL_Logs\opentrons-flex-http.{out,err}.log` |

Port 8070 remains the monitoring-only `gibbie-server`. The new equipment id is
`gibbie_flex_http`, deliberately distinct from its `gibbie_flex` observation.
Installing or stopping this gateway does not replace or restart the monitor or
the Gibbie workflow.

## Safety boundary

The installer sets `OT2_ROBOT_MODEL=Flex`, `OT2_TRANSPORT=http`, and
`OT2_AUTO_RECONNECT=false`. It reads `/health` to prove that the configured
target identifies as Flex before installing, but it never calls
`/control/startup`. A fresh service therefore reports `requires_init` and
creates no robot run until an operator takes control and initializes it from
the page.

The assistant has the same boundary as the OT-2 page: reads and draft plan
proposals only. It has no claim, approve, execute, startup, stop, or direct
control tool. The operator reviews the exact step hash and performs the
claim-gated Approve and Run clicks.

The authenticated proxy must supply the verified `X-Auth-User` and matching
`X-Edge-Key`, replacing any caller-supplied headers. After explicit approval,
provision the dedicated matching `OT2_EDGE_SECRET` in the installer's process
environment; apply mode passes it to the new service without printing it.
Do not put this secret in `assistant.env`: that file only loads assistant
settings. The firewall rule limits inbound port 8071 to tailnet source addresses.

## Prepare the assistant secret

The approved source is the Complexation OT-2 assistant configuration. Copy
only its provider credential and matching provider settings into the dedicated
Flex file; never copy its control credentials, robot settings, or state paths.
Keep secret values out of terminal output and the repository. For manual
provisioning after approval:

```powershell
New-Item -ItemType Directory -Force C:\SDL_State\opentrons-flex-http | Out-Null
notepad C:\SDL_State\opentrons-flex-http\assistant.env
```

The file contains one provider configuration and is never committed:

```dotenv
OPENROUTER_API_KEY=<secret>
OT2_ASSISTANT_ENABLED=true
# Optional overrides:
# OT2_ASSISTANT_MODEL=z-ai/glm-5.2
# OT2_ASSISTANT_BASE_URL=https://openrouter.ai/api/v1
```

If `OPENAI_API_KEY` is used instead, set both `OT2_ASSISTANT_BASE_URL` and a
model supported by that endpoint. Plan mode does not read this file. Apply
mode checks that it exists; the running gateway validates its configuration
through `/assistant/health`, without making a provider request. This checks
configuration, not whether the provider accepts the key.

## Install

After approval and resolution of the authentication blocker, prepare the
checkout and dependencies in a **non-elevated** PowerShell session on Gibbie:

```powershell
git clone --branch opentrons_flex_http --single-branch `
  https://github.com/cyrilcaoyang/opentrons-server.git `
  C:\Users\sdl2\Projects\opentrons-flex-http

cd C:\Users\sdl2\Projects\opentrons-flex-http
C:\SDL_Tools\uv.exe sync --extra labware --link-mode copy

# Read-only preflight and printed plan.
powershell -NoProfile -ExecutionPolicy Bypass `
  -File tools\install-gibbie-flex-http.ps1

```

Then open an elevated PowerShell session solely for installation:

```powershell
# Apply only after reviewing and approving the plan and authentication route.
powershell -NoProfile -ExecutionPolicy Bypass `
  -File C:\Users\sdl2\Projects\opentrons-flex-http\tools\install-gibbie-flex-http.ps1 -Run
```

The service executes the prepared `.venv\Scripts\python.exe` directly.
Neither installation nor service startup syncs dependencies. Never run
`uv sync` elevated: it can change cache ACLs for service accounts. NSSM
configuration failures abort installation; automatic service startup is only
enabled after read-only acceptance succeeds.

`deploy/gibbie_flex_http.env.example` is the reviewable, secret-free copy of
the environment block the installer writes into NSSM.

## No-motion acceptance

These reads are safe remotely and are the complete automated acceptance for
installation:

```powershell
curl.exe http://127.0.0.1:8071/health
curl.exe http://127.0.0.1:8071/status
curl.exe http://127.0.0.1:8071/openapi.json
curl.exe http://127.0.0.1:8071/assistant/health
# Direct /ui/ returns 404 by design. Check the approved authenticated page
# in the browser. The installer checks backend UI with its approved edge key.
```

Pass conditions:

- `/health` reports healthy and `/status` names `gibbie_flex_http`.
- `/status` is `requires_init` (or `unknown` with an explicit observed cause),
  never convenient `ready` before initialization.
- The installer's robot `/health` read identifies Flex. Before initialization,
  the gateway's profile is configuration, not an observation of the robot;
  do not interpret it as live identity or reachability readback.
- `/assistant/health` reports `configured: true` without exposing the key.
- `/ui/` serves the shared profile-aware control page. Its tab title changes to
  the configured Flex name, its deck uses A1-D4, and its assistant names this
  robot.

Do not automate the next click. Initialization creates the gateway's typed HTTP
run and must be an operator decision. Motion acceptance requires a person at
the Flex with the physical stop available and a separately reviewed procedure.

## Rollback

The Flex profile in this gateway requires HTTP and rejects
`OT2_TRANSPORT=ssh`. It must not be used to switch the existing Gibbie SSH
workflow's transport. Operators handle any later HTTP-server shutdown and SSH
handoff themselves. No HTTP run is created during this installation, and no
SSH workflow or robot mode is changed by it.

Stopping the gateway does not move the robot:

```powershell
C:\SDL_Tools\nssm.exe stop opentrons-flex-http
C:\SDL_Tools\nssm.exe remove opentrons-flex-http confirm
Remove-NetFirewallRule -DisplayName "opentrons-flex-http 8071"
```

Keep `C:\SDL_State\opentrons-flex-http` for inspection. Removing it would erase
the tip, plate, deck, and stop-latch record and is not part of rollback.
