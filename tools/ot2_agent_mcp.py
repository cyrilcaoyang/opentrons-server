#!/usr/bin/env python
"""MCP server exposing one OT-2 gateway to an agent harness (Hermes).

Run it, don't import it. Hermes spawns this as a stdio MCP server:

    hermes mcp add ot2-complexation \\
        --command uv \\
        --args "run --with mcp --with requests <abs path>/tools/ot2_agent_mcp.py \\
                --base-url http://sdl2-pc-03-cytation.tail6a1dd7.ts.net:8021"

Two deliberate structural choices, both about blast radius:

**It is standalone.** It imports only ``mcp`` and ``requests`` — never
``opentrons_server`` — and reaches the gateway over its public REST API. So it
runs in a throwaway ``uv run --with`` environment and the gateway service venv
is never touched. That matters here more than usual: two live robots share one
checkout and one ``.venv`` on this PC, and adding ``mcp`` there would mean a
stop/sync/start on both (``mcp`` pulls ~10 packages including pywin32).

**It cannot move the robot.** The tool list below is reads plus
propose/revise. There is no approve, execute, or abort tool — not because
they are hidden, but because the gateway refuses them without the claim token,
which lives in the operator's browser and is never given to an agent. Exposing
them would only produce confusing 423s. An agent drafts; a human at
``…/ui/`` reviews, approves, and runs.

The action catalog is fetched from the device at startup
(``GET /plans/actions``) rather than hard-coded, so a gateway that gains an
action or a field does not silently drift from what this advertises.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests

# mcp 2.0 renamed FastMCP -> MCPServer and moved it. Both expose the same
# `.tool()` decorator and `.run()`, so accept either rather than pinning a
# major: this script runs in a throwaway `uv run --with mcp` environment whose
# resolution we do not control, and an agent harness may already have its own.
try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _McpServer
except ImportError:  # pragma: no cover — mcp 1.x
    from mcp.server.fastmcp import FastMCP as _McpServer

DEFAULT_TIMEOUT_S = 20.0


class Gateway:
    """Thin REST client for one gateway instance."""

    def __init__(self, base_url: str, *, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"X-Api-Key": api_key} if api_key else {}

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=self._headers,
            timeout=DEFAULT_TIMEOUT_S,
            **kwargs,
        )
        if not resp.ok:
            # Surface the gateway's own refusal verbatim. Its 422 messages name
            # the offending field, which is exactly what lets a model repair a
            # bad proposal on the next turn instead of guessing.
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text
            raise RuntimeError(f"gateway {resp.status_code}: {json.dumps(detail, default=str)}")
        return None if resp.status_code == 204 else resp.json()

    def get(self, path: str) -> Any:
        return self._call("GET", path)

    def post(self, path: str, body: Any) -> Any:
        return self._call("POST", path, json=body)

    def put(self, path: str, body: Any) -> Any:
        return self._call("PUT", path, json=body)


def build_server(gateway: Gateway, *, instance: str) -> Any:
    mcp = _McpServer(f"ot2-{instance}")

    # ---------------- reads: phase 1, "what is the state" ----------------

    @mcp.tool()
    def get_status() -> dict:
        """Full status of this OT-2: health, activity, components, deck
        snapshot, loaded plate, tip racks, and who holds the claim.

        Start here. `equipment_status` says whether the robot is fit to run;
        `activity` says whether it is busy right now; `allowed_actions` is the
        device's own list of what it would accept at this moment.
        """
        return gateway.get("/status")

    @mcp.tool()
    def get_deck() -> dict:
        """Normalized robot deck: what labware is on each slot, its kind and
        grid, and where that knowledge came from (a live run, the REPL, or an
        operator declaration)."""
        status = gateway.get("/status")
        return status.get("details", {}).get("snapshot", {}).get("deck", {})

    @mcp.tool()
    def get_consumables() -> dict:
        """Tip racks with per-tip state, and the currently loaded plate with
        per-well sample IDs and volumes.

        Check this before proposing pipetting: a rack with no fresh tips or a
        plate that is not loaded will make the plan fail at the first step.
        """
        details = gateway.get("/status").get("details", {})
        return {
            "tip_racks": details.get("tip_racks"),
            "loaded_plate": details.get("loaded_plate"),
            "mounted_tips": details.get("mounted_tips"),
            "pipette_channels": details.get("pipette_channels"),
        }

    @mcp.tool()
    def get_equipment_docs() -> dict:
        """Read equipment capabilities, argument schemas, naming conventions,
        agent boundaries and remaining Python API gaps. This guide
        describes the gateway software; read get_status for live readiness.
        """
        return gateway.get("/docs/agent")

    @mcp.tool()
    def list_actions() -> dict:
        """The catalog a plan step may draw from, with each action's argument
        schema and whether it is idempotent.

        Read this before proposing. Actions not listed here cannot be planned —
        notably startup, shutdown, pause, resume, stop and reconcile, which are
        operator-only by design.
        """
        return gateway.get("/plans/actions")

    # ---------------- propose: phase 2, "agree the steps" ----------------

    @mcp.tool()
    def propose_plan(steps: list[dict], notes: str | None = None) -> dict:
        """Propose an ordered plan for a human to review. Does NOT run it.

        `steps` is a list of {"action": str, "args": {...}} drawn from
        `list_actions`. Every argument is validated against the device's own
        request model immediately, so a malformed step comes back as an error
        here rather than failing at the robot.

        The operator sees the proposal in the gateway UI and decides. You
        cannot approve or run it — say so plainly rather than implying the
        work is underway.
        """
        return gateway.post(
            "/plans", {"steps": steps, "created_by": "agent:hermes", "notes": notes}
        )

    @mcp.tool()
    def revise_plan(plan_id: str, steps: list[dict]) -> dict:
        """Replace a plan's steps after operator feedback.

        Any existing approval is discarded and the plan returns to draft —
        the human approved different steps, so their approval does not carry
        over. Expect to ask them to review again.
        """
        return gateway.put(f"/plans/{plan_id}/steps", {"steps": steps})

    @mcp.tool()
    def get_plan(plan_id: str) -> dict:
        """One plan: its steps, status, per-step outcomes, and — when it cannot
        run — the reason why, in `blocked_reason`."""
        return gateway.get(f"/plans/{plan_id}")

    @mcp.tool()
    def list_plans() -> list:
        """Every plan this gateway is holding, newest first. Use it to check
        whether the operator has approved or run something you proposed."""
        return gateway.get("/plans")

    return mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OT2_MCP_BASE_URL"),
        help="Gateway root on the tailnet, e.g. "
        "http://sdl2-pc-03-cytation.tail6a1dd7.ts.net:8021 (Complexation). NOT the "
        "Caddy edge URL — it runs forward_auth and 401s these paths.",
    )
    parser.add_argument(
        "--instance",
        default=os.environ.get("OT2_MCP_INSTANCE"),
        help="Short label for the server name; inferred from the URL if omitted.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OT2_MCP_API_KEY"),
        help="Sent as X-Api-Key; only needed when the gateway runs with "
        "OT2_REQUIRE_LOGIN. Grants no ability to move the robot.",
    )
    args = parser.parse_args(argv)

    if not args.base_url:
        parser.error("--base-url (or OT2_MCP_BASE_URL) is required")

    instance = args.instance or (args.base_url.rstrip("/").rsplit("/", 1)[-1] or "gateway")
    gateway = Gateway(args.base_url, api_key=args.api_key)

    # Fail loudly at startup rather than on the agent's first tool call: a
    # silent, unreachable MCP server looks to the model like a robot with no
    # state, which is exactly the wrong impression to give it.
    try:
        probe = gateway.get("/")
        print(
            f"[ot2-agent-mcp] {instance}: connected to "
            f"{probe.get('equipment_id')} (spec {probe.get('protocol_version')}) "
            f"at {gateway.base_url}",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"[ot2-agent-mcp] cannot reach {gateway.base_url}: {exc}", file=sys.stderr)
        return 1

    build_server(gateway, instance=instance).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
