"""The agent-facing MCP surface (``tools/ot2_agent_mcp.py``).

The property worth a regression test is what the surface does *not* have. An
agent's reach must end at the proposal, so a tool that approves, runs, or
aborts a plan must never appear here — and it must stay absent as tools are
added later.

`mcp` is a dev-only dependency: the server is standalone and runs in a
throwaway environment spawned by the agent harness, never inside the gateway
service venv (see the script's module docstring).
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("mcp", reason="agent MCP server is dev/agent-side only")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import ot2_agent_mcp  # noqa: E402
from ot2_agent_mcp import Gateway, build_server  # noqa: E402


async def _tool_names() -> set[str]:
    server = build_server(Gateway("http://gateway.invalid"), instance="test")
    return {t.name for t in await server.list_tools()}


@pytest.mark.asyncio
async def test_no_tool_can_move_the_robot():
    """The load-bearing absence.

    Approving and running are claim-gated in the gateway, and an agent never
    holds the claim — so these would only ever return 423. Exposing them would
    invite the model to try, and to report to the operator that it had started
    work it cannot start.
    """
    names = await _tool_names()
    for forbidden in (
        "approve_plan",   # the current name
        "authorize_plan",  # and the pre-rename one, so an old spelling cannot sneak back
        "execute_plan",
        "abort_plan",
        "run_plan",
    ):
        assert forbidden not in names


@pytest.mark.asyncio
async def test_exposes_reads_and_proposal_only():
    assert await _tool_names() == {
        "get_equipment_docs",
        "get_status",
        "get_deck",
        "get_consumables",
        "list_actions",
        "propose_plan",
        "revise_plan",
        "get_plan",
        "list_plans",
    }


@pytest.mark.asyncio
async def test_every_tool_has_a_docstring():
    """Tool descriptions are the model's only instructions. An undocumented
    tool is one it will use wrongly."""
    server = build_server(Gateway("http://gateway.invalid"), instance="test")
    for tool in await server.list_tools():
        assert tool.description and len(tool.description) > 40, tool.name


@pytest.mark.asyncio
async def test_equipment_docs_only_reads_the_guide():
    gateway = Mock(spec=Gateway)
    gateway.get.return_value = {"documentation_version": "1"}
    server = build_server(gateway, instance="test")
    await server.call_tool("get_equipment_docs", {})
    gateway.get.assert_called_once_with("/docs/agent")
    gateway.post.assert_not_called()


def test_gateway_surfaces_the_devices_own_refusal(monkeypatch):
    """A 422 from the gateway names the offending field, and that text has to
    reach the model verbatim — it is what lets it repair a bad proposal on the
    next turn instead of guessing."""
    resp = Mock(ok=False, status_code=422)
    resp.json.return_value = {"detail": "unknown action 'nuke'; allowed: ['aspirate', ...]"}
    monkeypatch.setattr(ot2_agent_mcp.requests, "request", lambda *a, **k: resp)

    with pytest.raises(RuntimeError, match="unknown action"):
        Gateway("http://gateway.invalid").post("/plans", {"steps": []})


def test_api_key_header_is_sent_only_when_configured(monkeypatch):
    seen: dict = {}

    def capture(method, url, **kwargs):
        seen.update(kwargs.get("headers") or {})
        return Mock(ok=True, status_code=200, json=lambda: {})

    monkeypatch.setattr(ot2_agent_mcp.requests, "request", capture)

    Gateway("http://gateway.invalid").get("/status")
    assert "X-Api-Key" not in seen

    Gateway("http://gateway.invalid", api_key="k-1").get("/status")
    assert seen["X-Api-Key"] == "k-1"


def test_base_url_trailing_slash_does_not_double_up(monkeypatch):
    """The edge URL operators paste ends in '/' (…/ot2/complexation/); a naive
    join would request '…//status'."""
    seen: dict = {}

    def capture(method, url, **kwargs):
        seen["url"] = url
        return Mock(ok=True, status_code=200, json=lambda: {})

    monkeypatch.setattr(ot2_agent_mcp.requests, "request", capture)
    Gateway("http://host/ot2/complexation/").get("/status")
    assert seen["url"] == "http://host/ot2/complexation/status"
