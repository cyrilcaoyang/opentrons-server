"""Read-only equipment guidance shared by HTTP and agent tool surfaces."""

from typing import Any

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
