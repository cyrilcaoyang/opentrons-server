"""Read-only equipment guidance shared by HTTP and agent tool surfaces.

Two surfaces live here. :func:`equipment_documentation` and
:func:`action_catalog` generate the JSON guide behind ``GET /docs/agent`` and
the typed catalog behind ``GET /plans/actions``. :data:`router` serves the
lab-standard Markdown documentation shipped under ``opentrons_server/docs/``.
"""

from importlib.resources import files
from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .models import PROTOCOL_VERSION
from .robot_profile import PROFILE
from .plans import PLAN_ACTIONS


def action_catalog() -> dict[str, Any]:
    """Generate schemas from the same models that validate proposed steps."""
    return {
        "actions": [
            {
                "action": name,
                "idempotent": spec.idempotent,
                "args_schema": spec.model.model_json_schema() if spec.model else None,
            }
            for name, spec in sorted(PLAN_ACTIONS.items())
        ]
    }


def equipment_documentation() -> dict[str, Any]:
    """Describe software capabilities without reading or connecting to hardware."""
    return {
        "documentation_version": "1",
        "equipment_kind": "liquid_handler",
        "model": f"Opentrons {PROFILE.model}",
        "protocol_version": PROTOCOL_VERSION,
        "scope": (
            "One robot per gateway. This document describes configured software capabilities, "
            "not the currently installed pipettes, deck, or readiness."
        ),
        "links": {
            "agent_docs": "/docs/agent",
            "openapi": "/openapi.json",
            "swagger": "/docs",
            "redoc": "/redoc",
            "status": "/status",
            "actions": "/plans/actions",
            "plans": "/plans",
        },
        "link_resolution": "Append paths to this gateway's base URL, including any proxy prefix.",
        "discovery": [
            "Read /status for equipment identity, health, activity, allowed_actions and last_error.",
            "Resolve loaded labware and pipette names from details.snapshot.labwares and "
            "details.snapshot.pipettes; details.session_recipe is not authoritative.",
            "Read details.snapshot.deck, details.tip_racks, details.mounted_tips and "
            "details.loaded_plate. Have the operator confirm the physical deck before use.",
            "Read /plans/actions for proposable action names, argument schemas and units. "
            "Catalog membership does not establish live readiness; check allowed_actions too.",
            "Read /openapi.json for all HTTP methods, paths, request and response schemas. "
            "Gateway endpoints expose only a subset of the Python control API.",
        ],
        "conventions": {
            "slots": list(PROFILE.slots),
            "units": "Liquid volumes in microliters, coordinates in millimeters, temperature in Celsius.",
            "deck_declare": "Full-layout replacement, not a patch; omitted slots are cleared.",
            "temperature": "tempmod.set accepts a target without waiting for the block to reach it.",
            "motion": "move_to defaults to an arced path. force_direct=true omits the Z-retract waypoint; constant-height XY travel requires destination Z equal to current Z. Flex absolute gripper moves default to direct; relative gripper dz=0 retains height.",
        },
        "agent_boundary": [
            "Agents may read and propose drafts via POST /plans; a draft does not execute. "
            "The operator reviews, approves and runs it in the device panel.",
            "When login is required, proposal requests need a verified identity. "
            "Agent machine identities use X-Api-Key from OT2_PROPOSER_KEYS; these cannot claim.",
            "Hardware workflows go through lab-skills with claims, preconditions and "
            "validated human-approved plans. Do not call raw /control/* or the robot-server "
            "to bypass an unavailable action.",
            "An unknown_outcome requires operator inspection and reconciliation; never retry "
            "automatically. Stop and report an interlock refusal or unexpected physical state.",
        ],
        "liquid_handling": {
            "blow_out": {"gateway_http_endpoint": "/control/blow-out", "plan_action": "blow_out",
                         "description": "Expel residual liquid at location, or explicitly in_place=true. Dispense does not automatically call blow-out."},
            "touch_tip": "POST /control/touch-tip names loaded labware and a well. The run engine uses labware geometry; existing tip contact tracking is updated.",
            "air_gap": "POST /control/air-gap requires a well and height above its top. Accounts for liquid already held when capacity is known.",
            "dispense": "Optional push_out is plunger air volume in uL, separate from a full blow-out.",
            "speed": "set_speed on HTTP applies to explicit gantry moves; implicit liquid-command moves retain robot defaults.",
        },
        "stop": {
            "endpoint": "/control/stop", "plan_action": None,
            "description": "Claim-gated software stop of this gateway's HTTP run. Success requires stopped readback. No SSH interrupt support or physical emergency-stop guarantee. Inspect, shut down and start a fresh session afterward.",
        },
        "python_api_gaps": {
            "scope": "This catalog is not the whole upstream Python API. Private hardware access and per-axis max speed are not exposed. Flex profile adds native gripper commands but supports full 1/8-channel layouts only.",
        },
        **action_catalog(),
    }


# ---------------------------------------------------------------------------
# Markdown agent documentation, served from resources shipped in the package.
#
# The same shape as every other lab device service (torry-pines-shaker-server,
# agilent-cytation-server, sense-every-zone, mt-xpr-balance-server): a Markdown
# agent guide, a Markdown API reference and a plain-text ``/llms.txt`` index,
# so an agent — or the dashboard's API reference page — can discover how to
# drive this device without reading the repo.
#
# This is additive. The JSON ``GET /docs/agent`` guide above and the typed
# ``GET /plans/actions`` catalog are unchanged and remain the machine-readable
# surface; the Markdown documents are the prose that explains them.
# ---------------------------------------------------------------------------

router = APIRouter(tags=["documentation"])


class MarkdownResponse(PlainTextResponse):
    media_type = "text/markdown"


def _document(name: str) -> str:
    return files("opentrons_server").joinpath("docs", name).read_text(encoding="utf-8")


@router.get("/agent-docs", response_class=MarkdownResponse, summary="Agent guide (Markdown)")
async def agent_guide() -> str:
    return _document("AGENT_GUIDE.md")


@router.get(
    "/agent-docs/api-reference",
    response_class=MarkdownResponse,
    summary="API reference (Markdown)",
)
async def api_reference() -> str:
    return _document("API_REFERENCE.md")


@router.get("/llms.txt", response_class=PlainTextResponse, summary="Discovery index for agents")
async def llms_txt() -> str:
    # Relative links only: the gateway is served behind a proxy prefix
    # (`/ot2/complexation/...`) as well as on its own port, and an absolute
    # path would resolve off the mount.
    return (
        "# Opentrons OT-2 gateway (STATUS_SPEC v1.2 liquid handler)\n\n"
        "## Documentation\n\n"
        "- [Agent guide](agent-docs): health vs activity, claims and gating, run "
        "lifecycle, plan execution, preconditions, refusal codes.\n"
        "- [API reference](agent-docs/api-reference): every route with its gate, "
        "body and refusal codes.\n"
        "- [OpenAPI](openapi.json): request/response schemas.\n\n"
        "## Machine-readable device resources\n\n"
        "- [Equipment guide, JSON](docs/agent): links, discovery order, conventions, "
        "agent boundary and the generated action catalog. No claim, no hardware I/O.\n"
        "- [Plannable actions](plans/actions): every proposable action with its "
        "idempotence flag and argument schema.\n\n"
        "## Live status\n\n"
        "Read the gateway's `GET /status` through the lab-skills SDK or dashboard; "
        "live status is not a documentation-proxy resource. Read allowed_actions "
        "before acting, and never infer deck contents from this documentation.\n"
    )
