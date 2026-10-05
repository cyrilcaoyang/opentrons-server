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

    def get_bytes(self, path: str, params: Any = None) -> bytes:
        resp = requests.get(f"{self.base_url}{path}", headers=self._headers,
                            params=params, timeout=DEFAULT_TIMEOUT_S)
        if not resp.ok:
            raise RuntimeError(f"gateway {resp.status_code}: {resp.text[:500]}")
        return resp.content


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
        notably startup, shutdown, pause, resume, stop, and reconcile.
        platebalance.tare/zero are included as non-idempotent reference writes.
        """
        return gateway.get("/plans/actions")

    # ---------------- propose: phase 2, "agree the steps" ----------------

    @mcp.tool()
    def propose_plan(steps: list[dict] | None = None, notes: str | None = None,
                     for_each_well: dict | None = None, prelude: list[dict] | None = None,
                     epilogue: list[dict] | None = None) -> dict:
        """Propose an ordered plan for a human to review. Does NOT run it.

        Either `steps` — a list of {"action": str, "args": {...}} drawn from
        `list_actions` — or, for work repeated over wells, `for_each_well`:
        {"labware_nickname", "wells": "A1:H12" | [...], "order": "column"|"row",
        "steps": [template steps; {well}/{row}/{column}/{index} in args],
        "overrides": {well: {step_id: partial args}}}, with optional `prelude`
        and `epilogue` steps around the loop. The gateway expands the pattern
        and validates every step; a malformed step comes back as an error here
        rather than failing at the robot.

        The operator sees the proposal in the gateway UI and decides. You
        cannot approve or run it — say so plainly rather than implying the
        work is underway.
        """
        body: dict = {"created_by": "agent:hermes", "notes": notes}
        if for_each_well is not None:
            body.update(for_each_well=for_each_well, prelude=prelude or [], epilogue=epilogue or [])
        else:
            body["steps"] = steps or []
        return gateway.post("/plans", body)

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
        run — the reason why, in `blocked_reason`. A completed platebalance.read
        step has its measured weight in `results[].reading`."""
        return gateway.get(f"/plans/{plan_id}")

    @mcp.tool()
    def list_plans() -> list:
        """Every plan this gateway is holding, newest first. Use it to check
        whether the operator has approved or run something you proposed."""
        return gateway.get("/plans")

    def _report_params(plan_ids: list[str], labware: str | None, density_g_per_ml: float | None) -> list:
        params: list = [("plan_id", pid) for pid in plan_ids]
        if labware:
            params.append(("labware", labware))
        if density_g_per_ml is not None:
            params.append(("density_g_per_ml", density_g_per_ml))
        return params

    @mcp.tool()
    def get_plate_report(plan_ids: list[str], labware: str | None = None,
                         density_g_per_ml: float | None = None) -> dict:
        """Balance results of one or more executed plans on a 96-well grid:
        per-well mass (reading minus the balance reference), deviation from the
        mean, status (weighed / dispensed_unweighed / failed / not_run), and
        mean/SD/CV. List a run split across plans in run order. Pass a density
        only if the operator gave one. Read-only."""
        q = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in _report_params(plan_ids, labware, density_g_per_ml))
        return gateway.get(f"/plans/plate-report?{q}")

    @mcp.tool()
    def download_plate_spreadsheet(plan_ids: list[str], path: str, labware: str | None = None,
                                   density_g_per_ml: float | None = None) -> dict:
        """Save the plate report as an Excel workbook at `path` on this machine:
        summary, mass and deviation plate grids with heatmap colouring, a
        per-well table and the full weighing log. Read-only on the gateway."""
        data = gateway.get_bytes("/plans/plate-report.xlsx",
                                 params=_report_params(plan_ids, labware, density_g_per_ml))
        out = os.path.abspath(os.path.expanduser(path))
        if not out.lower().endswith(".xlsx"):
            raise ValueError("path must end in .xlsx")
        with open(out, "wb") as fh:
            fh.write(data)
        return {"path": out, "bytes": len(data)}

    @mcp.tool()
    def plate_report_url(plan_ids: list[str]) -> dict:
        """Links to the interactive heatmap and the spreadsheet, for an operator
        who can reach this gateway in a browser."""
        q = "&".join(f"plan_id={requests.utils.quote(pid)}" for pid in plan_ids)
        return {"html": f"{gateway.base_url}/plans/plate-report.html?{q}",
                "xlsx": f"{gateway.base_url}/plans/plate-report.xlsx?{q}"}

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
