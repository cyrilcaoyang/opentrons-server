"""Static guarantees for the isolated Gibbie Flex deployment profile."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[2]
ENV = ROOT / "deploy" / "gibbie_flex_http.env.example"
INSTALLER = ROOT / "tools" / "install-gibbie-flex-http.ps1"


def _environment() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"not an environment assignment: {line}"
        values[key] = value
    return values


def test_flex_profile_is_http_and_cannot_auto_initialize() -> None:
    env = _environment()
    assert env["OT2_EQUIPMENT_ID"] == "gibbie_flex_http"
    assert env["OT2_ROBOT_MODEL"] == "Flex"
    assert env["OT2_TRANSPORT"] == "http"
    assert env["OT2_HTTP_BASE_URL"] == "http://192.168.254.81:31950"
    assert env["OT2_DRY_RUN"] == "false"
    assert env["OT2_AUTO_RECONNECT"] == "false"


def test_every_persistent_store_is_unique_and_scoped_to_this_instance() -> None:
    env = _environment()
    paths = [
        env["OT2_PLATE_STATE_PATH"],
        env["OT2_DECK_STATE_PATH"],
        env["OT2_TIP_STATE_PATH"],
        env["OT2_STOP_STATE_PATH"],
    ]
    assert len(paths) == len(set(paths))
    assert all(path.startswith("C:/SDL_State/opentrons-flex-http/") for path in paths)


def test_assistant_is_enabled_but_its_secret_is_external() -> None:
    env = _environment()
    assert env["OT2_ASSISTANT_ENABLED"] == "true"
    assert env["OT2_ENV_FILE"] == "C:/SDL_State/opentrons-flex-http/assistant.env"
    text = ENV.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=" not in text
    assert "OPENAI_API_KEY=" not in text
    assert "CHANGE_ME" not in text


def test_installer_pins_branch_port_profile_and_no_auto_reconnect() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    for required in (
        '"opentrons_flex_http"',
        "[int]$Port = 8071",
        '"OT2_ROBOT_MODEL=Flex"',
        '"OT2_TRANSPORT=http"',
        '"OT2_AUTO_RECONNECT=false"',
        '"OT2_ASSISTANT_ENABLED=true"',
        "Invoke-RestMethod -Method Get",
    ):
        assert required in text
    assert 'Uri "$base/control/startup"' not in text


def test_page_requires_authenticated_edge() -> None:
    env = _environment()
    assert env["OT2_TRUST_LOCAL_UI"] == "false"
    assert env["OT2_REQUIRE_LOGIN"] == "true"


def test_first_install_reads_preserve_authentication_and_never_connect(tmp_path) -> None:
    """Use the real Flex profile in a fresh process, with all I/O forbidden."""
    env = _environment()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["OT2_HTTP_BASE_URL"] = "http://synthetic-flex.invalid:31950"
    env["OT2_HOST_ALIAS"] = "synthetic-flex.invalid"
    env["OT2_EDGE_SECRET"] = "synthetic-edge-secret"
    for kind in ("PLATE", "TIP", "DECK", "STOP"):
        env[f"OT2_{kind}_STATE_PATH"] = str(tmp_path / f"{kind}.json")
    assistant_file = tmp_path / "assistant.env"
    assistant_file.write_text("OPENROUTER_API_KEY=synthetic-provider-key\n", encoding="utf-8")
    env["OT2_ENV_FILE"] = str(assistant_file)
    code = textwrap.dedent('''
        import threading
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from opentrons_server.gateway.service import OT2Service
        from opentrons_server.gateway.assistant import _tool_schemas

        start_thread = threading.Thread.start
        def guarded_start(thread):
            assert not thread.name.startswith("ot2-"), thread.name
            return start_thread(thread)

        with patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")) as http, \
             patch.object(OT2Service, "startup", side_effect=AssertionError("startup forbidden")) as startup, \
             patch.object(threading.Thread, "start", guarded_start):
            from opentrons_server.gateway.api import app
            with TestClient(app) as client:
                assert client.get("/health").json()["status"] == "healthy"
                for _ in range(3):
                    status = client.get("/status").json()
                    assert status["equipment_id"] == "gibbie_flex_http"
                    assert status["equipment_status"] == "requires_init"
                    assert status["details"]["control_auth"] == "identity"
                    assert status["details"]["ui_mode"] == "edge"
                    assert app.state.service.control is None
                schema = client.get("/openapi.json").json()
                assert "/control/gripper-move-to-absolute" in schema["paths"]
                assert client.get("/ui/").status_code == 404
                edge = {"X-Edge-Key": "synthetic-edge-secret", "X-Auth-User": "test-operator"}
                ui = client.get("/ui/", headers=edge)
                assert ui.status_code == 200
                assert "<title>Opentrons Gateway</title>" in ui.text
                health = client.get("/assistant/health")
                assert health.json()["configured"] is True
                assert "synthetic-provider-key" not in health.text
                assert "synthetic-edge-secret" not in str(status)
                claim = {"owner": "unverified", "session_id": "test", "ttl_s": 30}
                assert client.post("/control/claim", json=claim).status_code == 401
                assert client.post("/control/home", json={}).status_code == 423
                chat = {"messages": [{"role": "user", "content": "Read status"}]}
                assert client.post("/assistant/chat", json=chat).status_code == 401
                assert client.post("/assistant/chat", json=chat, headers=edge).status_code == 423
                for action in ("approve", "execute"):
                    assert client.post(f"/plans/unapproved/{action}", json={}).status_code == 423
                assert {tool["function"]["name"] for tool in _tool_schemas()} == {
                    "get_equipment_docs", "get_status", "get_deck", "get_consumables",
                    "list_actions", "propose_plan",
                }
                http.assert_not_called()
                startup.assert_not_called()
    ''')
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
