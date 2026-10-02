"""Claude Code CLI transport for the optional, proposal-only assistant.

The CLI receives gateway reads on stdin and has no built-in or MCP tools. Its
output is data for the existing PlanStore validator, never an action to run.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from typing import Any


_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"action": {"type": "string"}, "args": {"type": "object"}},
                "required": ["action", "args"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reply", "steps"],
    "additionalProperties": False,
}


def _subscription_env() -> dict[str, str]:
    """Keep the service's Claude.ai login without routing through an API key."""
    env = os.environ.copy()
    for name in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    ):
        env.pop(name, None)
    return env


def claude_code_authenticated(executable: str) -> bool:
    """Check the identity the gateway process itself can use."""
    try:
        result = subprocess.run(
            [executable, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=5, check=False,
            env=_subscription_env(),
        )
        status = json.loads(result.stdout)
        return (result.returncode == 0 and status.get("loggedIn") is True
                and status.get("authMethod") == "claude.ai"
                and status.get("apiProvider") == "firstParty")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def run_claude_code(
    executable: str, system_prompt: str, context: dict[str, Any], timeout_s: float,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Ask the authenticated Claude Code CLI for a structured reply and draft."""
    command = [
        executable, "-p", "--model", "claude-sonnet-5-5",
        "--output-format", "json", "--json-schema", json.dumps(_REPLY_SCHEMA),
        "--system-prompt", system_prompt,
        "--tools", "", "--strict-mcp-config", "--safe-mode",
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--disable-slash-commands", "--no-session-persistence",
    ]
    # An empty working directory prevents the CLI from inheriting any gateway
    # checkout context. Credentials still belong to the service account.
    payload = json.dumps(context, default=str)
    try:
        with tempfile.TemporaryDirectory(prefix="ot2-assistant-") as directory:
            if cancel_event is None:
                result = subprocess.run(
                    command, input=payload, capture_output=True, text=True,
                    timeout=timeout_s, cwd=directory, check=False,
                    env=_subscription_env(),
                )
                return_code, output = result.returncode, result.stdout
            else:
                if cancel_event.is_set():
                    raise InterruptedError("Claude Code reply stopped")
                with subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, cwd=directory,
                    env=_subscription_env(),
                ) as process:
                    deadline = time.monotonic() + timeout_s
                    pending_input: str | None = payload
                    try:
                        while True:
                            if cancel_event.is_set():
                                raise InterruptedError("Claude Code reply stopped")
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise subprocess.TimeoutExpired(command, timeout_s)
                            try:
                                stdout, _stderr = process.communicate(
                                    input=pending_input, timeout=min(0.25, remaining),
                                )
                                break
                            except subprocess.TimeoutExpired:
                                pending_input = None
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate()
                    return_code, output = process.returncode, stdout
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Claude Code did not reply within {timeout_s:g} seconds") from exc
    if return_code != 0:
        raise RuntimeError(f"Claude Code exited with status {return_code}")
    try:
        envelope = json.loads(output)
        if envelope.get("is_error"):
            raise ValueError("Claude Code reported an error")
        structured = envelope.get("structured_output")
        if structured is None:
            structured = json.loads(envelope["result"])
        if not isinstance(structured, dict):
            raise ValueError("Claude Code returned no structured reply")
        reply, steps = structured["reply"], structured["steps"]
        if not isinstance(reply, str) or not isinstance(steps, list):
            raise ValueError("Claude Code returned an invalid reply")
        return {"reply": reply, "steps": steps}
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Claude Code returned no valid structured reply") from exc
