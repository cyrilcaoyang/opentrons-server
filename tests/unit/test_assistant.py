"""The optional in-page chat assistant.

Two things are worth pinning. First that it is genuinely optional — a gateway
with no key must behave exactly as it did before, because the repo's promise is
that installing it alone gives you a working device service. Second that it is
a *proposer*: its only write tool is propose_plan, so it comes through the same
human-approval gate as an agent harness and cannot move the robot.

No network and no hardware — the OpenAI client is faked throughout.
"""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from opentrons_server.gateway import assistant as assistant_mod
from opentrons_server.gateway import assistant_claude as claude_mod
from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.assistant import (
    Assistant,
    AssistantCancelled,
    AssistantConfig,
    AssistantDisabled,
    _tool_schemas,
)
from opentrons_server.gateway.plans import PLAN_ACTIONS, PlanStep, PlanStore
from opentrons_server.gateway.service import OT2Service

CLAIM = {"owner": "ada@lab", "session_id": "s1", "ttl_s": 30.0}


@pytest.fixture(autouse=True)
def _isolate_env_file(tmp_path, monkeypatch):
    """Point the ``.env`` lookup at a path that does not exist.

    Without this, a real repo-root ``.env`` — which is the documented way to
    configure the assistant, so it exists on any machine where someone has
    turned it on — silently supplies a key to the tests that assert the
    assistant is *un*configured. They passed until the moment the feature was
    actually used, which is the worst time for a test to start lying.

    Tests that want a file set ``OT2_ENV_FILE`` themselves; a later setenv
    overrides this one.
    """
    monkeypatch.setenv("OT2_ENV_FILE", str(tmp_path / "absent.env"))


def _config(**over):
    base = dict(
        enabled=True,
        api_key="k-test",
        model="test/model",
        base_url="http://llm.invalid/v1",
        max_tokens=256,
        timeout_s=5.0,
    )
    base.update(over)
    return AssistantConfig(**base)


def _fake_openai(monkeypatch, responses):
    """Install a fake OpenAI client that replays `responses` in order."""
    calls = {"sent": []}

    def create(**kwargs):
        calls["sent"].append(kwargs)
        return responses.pop(0)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=Mock())
    calls["client"] = client
    monkeypatch.setattr(
        assistant_mod, "OpenAI", lambda **_kw: client, raising=False
    )
    import openai

    monkeypatch.setattr(openai, "OpenAI", lambda **_kw: client)
    return calls


def _text(content):
    return SimpleNamespace(
        model="test/model",
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))],
    )


def _tool_call(name, args, call_id="c1"):
    return SimpleNamespace(
        model="test/model",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id=call_id,
                            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
                        )
                    ],
                )
            )
        ],
    )


# ---------------------------------------------------------------------------
# Optional by construction
# ---------------------------------------------------------------------------


def test_no_key_means_disabled_not_broken():
    """The repo's promise is that installing it alone gives a working gateway.
    An unconfigured assistant reports why and changes nothing else."""
    reason = _config(api_key=None).unavailable_reason()
    assert reason and "no API key" in reason


def test_kill_switch_wins_over_a_configured_key():
    reason = _config(enabled=False).unavailable_reason()
    assert reason and "disabled" in reason


def test_a_configured_assistant_reports_available():
    assert _config().unavailable_reason() is None


def test_chat_refuses_when_disabled():
    a = Assistant(OT2Service(dry_run=True), PlanStore(), _config(api_key=None))
    with pytest.raises(AssistantDisabled, match="no API key"):
        a.chat([{"role": "user", "content": "hello"}])


def test_health_endpoint_is_open_and_leaks_nothing(monkeypatch):
    """The UI needs this before login to decide whether to render the bubble,
    so it is unauthenticated — and therefore must expose only a boolean and a
    reason, never the key or the provider URL."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))

    body = client.get("/assistant/health").json()

    assert body["configured"] is False
    assert "no API key" in body["reason"]
    assert body["model"] is None
    assert "k-" not in json.dumps(body)


# ---------------------------------------------------------------------------
# It is a proposer, not a driver
# ---------------------------------------------------------------------------


def test_no_tool_can_move_the_robot():
    """The load-bearing absence, mirroring the MCP surface. Approving and
    running are claim-gated clicks; offering them here would only produce
    refusals and tempt the model to claim it had started work."""
    names = {t["function"]["name"] for t in _tool_schemas()}
    assert names == {
        "get_equipment_docs",
        "get_status",
        "get_deck",
        "get_consumables",
        "list_actions",
        "propose_plan",
        "get_plan",
        "list_plans",
        "get_plate_report",  # read-only: plan records only, no robot I/O
    }
    for forbidden in (
        "approve_plan",   # the current name
        "authorize_plan",  # and the pre-rename one, so an old spelling cannot sneak back
        "execute_plan",
        "abort_plan",
        "startup",
        "home",
    ):
        assert forbidden not in names


def test_bookkeeping_corrections_are_proposable():
    """The assistant refused `tips.mark` in the field as "operator-only" — it is
    not, and never was. It is in the catalog, in `allowed_actions`, and in the
    tool the model reads. The refusal came from prose (the prompt's operator-only
    list, and this request model's own docstring, which the model sees as the
    action's schema description), so both now say plainly that proposing a
    correction is not the same as asserting it: approving the draft is.

    This matters because a drifted tracker is exactly when the assistant is most
    useful, and describing the fix in prose leaves an operator to do by hand what
    the panel could have handed them as one reviewable step."""

    catalog = {a["action"] for a in Assistant._actions()["actions"]}
    for correction in ("tips.mark", "tips.reset", "plate.load", "well.update", "deck.declare"):
        assert correction in catalog

    # ...and the model is not told anything that reads as a prohibition on them.
    assert "tips.mark" in assistant_mod._SYSTEM_PROMPT
    schema = json.dumps(
        PLAN_ACTIONS["tips.mark"].model.model_json_schema()
    )
    assert "operator asserting" not in schema


def test_the_operator_only_list_stays_unplannable():
    named = {"startup", "shutdown", "pause", "resume", "reconcile"}
    assert named.isdisjoint(PLAN_ACTIONS)
    for action in named:
        assert f"`{action}`" in assistant_mod._SYSTEM_PROMPT
    for action in ("platebalance.tare", "platebalance.zero"):
        assert action in PLAN_ACTIONS
        assert f"`{action}`" in assistant_mod._SYSTEM_PROMPT


def test_proposing_creates_a_draft_the_operator_must_approve(monkeypatch):
    store = PlanStore()
    a = Assistant(OT2Service(dry_run=True), store, _config())
    _fake_openai(
        monkeypatch,
        [
            _tool_call("propose_plan", {"steps": [{"action": "lights.set", "args": {"on": True}}]}),
            _text("Proposed one step. Approve it in the panel to run it."),
        ],
    )

    result = a.chat([{"role": "user", "content": "turn the lights on"}])

    assert result["plan_id"]
    plan = store.get(result["plan_id"])
    assert plan.status == "draft"          # not approved
    assert plan.approval is None      # and not runnable
    assert plan.created_by == "assistant"  # attributable in the panel


def test_a_bad_proposal_is_returned_to_the_model_to_repair(monkeypatch):
    """A validation error is the model's problem, not the operator's. It comes
    back as a tool result so the next turn can fix the step, rather than
    surfacing a stack trace in the chat box."""
    store = PlanStore()
    a = Assistant(OT2Service(dry_run=True), store, _config())
    calls = _fake_openai(
        monkeypatch,
        [
            _tool_call("propose_plan", {"steps": [{"action": "nuke", "args": {}}]}),
            _text("That action does not exist on this robot."),
        ],
    )

    result = a.chat([{"role": "user", "content": "nuke it"}])

    assert result["plan_id"] is None
    assert store.list() == []
    tool_reply = [m for m in calls["sent"][-1]["messages"] if m.get("role") == "tool"][-1]
    assert "unknown action" in tool_reply["content"]


def test_reads_are_scoped_to_this_service(monkeypatch):
    """The tools close over one service instance — there is no device selector
    to get wrong, which is what makes 'this robot only' a property of the code."""
    service = OT2Service(dry_run=True)
    a = Assistant(service, PlanStore(), _config())
    _fake_openai(monkeypatch, [_tool_call("get_status", {}), _text("It is in dry run.")])

    result = a.chat([{"role": "user", "content": "what's the status"}])

    assert result["tools_used"] == ["get_status"]


def test_assistant_can_read_completed_plan_measurements_without_device_io():
    service = Mock()
    store = PlanStore()
    plan = store.create([PlanStep(action="platebalance.read", args={})], created_by="agent")
    plan.status = "executed"
    plan.results[0].outcome = "ok"
    plan.results[0].reading = {"value": 0.125, "unit": "g", "stable": True,
                               "observed_at": "2026-10-02T00:00:00+00:00"}
    tools = Assistant(service, store, _config())._tools()

    assert tools["get_plan"]({"plan_id": plan.plan_id})["results"][0]["reading"]["value"] == 0.125
    assert tools["list_plans"]({})[0]["plan_id"] == plan.plan_id
    assert tools["list_plans"]({})[0]["readings"][0]["value"] == 0.125
    assert tools["list_plans"]({})[0]["results"][0]["outcome"] == "ok"
    service.assert_not_called()


def test_chat_receives_current_plan_outcome_even_when_history_says_draft(monkeypatch):
    store = PlanStore()
    plan = store.create([PlanStep(action="platebalance.zero", args={})], created_by="agent")
    plan.status = "executed"
    plan.results[0].outcome = "ok"
    plan.results[0].balance_operation = {"action": "zero", "outcome": "sent_unconfirmed"}
    calls = _fake_openai(monkeypatch, [_text("The operator ran the plan; zero was sent unconfirmed.")])

    Assistant(Mock(), store, _config()).chat([
        {"role": "assistant", "content": "I proposed a draft."},
        {"role": "user", "content": "Did it run?"},
    ])

    current = calls["sent"][0]["messages"][1]["content"]
    assert plan.plan_id in current
    assert '"status": "executed"' in current
    assert '"outcome": "sent_unconfirmed"' in current


def test_chat_events_report_tool_progress(monkeypatch):
    a = Assistant(OT2Service(dry_run=True), PlanStore(), _config())
    _fake_openai(monkeypatch, [_tool_call("get_status", {}), _text("It is ready.")])

    events = list(a.chat_events([{"role": "user", "content": "status"}]))

    assert [event["type"] for event in events] == [
        "thinking",
        "tool_started",
        "tool_finished",
        "thinking",
        "complete",
    ]
    assert events[1]["name"] == "get_status"
    assert events[2]["success"] is True
    assert events[-1]["result"]["reply"] == "It is ready."


def test_empty_reply_after_tools_is_nudged_once(monkeypatch):
    """Reasoning models often return no content after a tool round. Complete
    that as a visible sentence only after a bounded nudge fails — never as '…'."""
    a = Assistant(OT2Service(dry_run=True), PlanStore(), _config())
    _fake_openai(
        monkeypatch,
        [
            _tool_call("get_status", {}),
            _text(""),
            _text("Deck is empty; I cannot aspirate yet."),
        ],
    )

    result = a.chat([{"role": "user", "content": "aspirate 20 uL"}])

    assert result["reply"] == "Deck is empty; I cannot aspirate yet."
    assert result["tools_used"] == ["get_status"]


def test_empty_reply_after_nudge_budget_is_truthful(monkeypatch):
    a = Assistant(OT2Service(dry_run=True), PlanStore(), _config())
    _fake_openai(
        monkeypatch,
        [_tool_call("get_status", {}), _text(""), _text(""), _text("")],
    )

    result = a.chat([{"role": "user", "content": "aspirate 20 uL"}])

    assert "returned no reply" in result["reply"]
    assert result["tools_used"] == ["get_status"]


def test_chat_events_mark_a_failed_tool(monkeypatch):
    a = Assistant(OT2Service(dry_run=True), PlanStore(), _config())
    _fake_openai(
        monkeypatch,
        [
            _tool_call("propose_plan", {"steps": [{"action": "nuke", "args": {}}]}),
            _text("That action is unavailable."),
        ],
    )

    events = list(a.chat_events([{"role": "user", "content": "nuke it"}]))
    finished = next(event for event in events if event["type"] == "tool_finished")

    assert finished["name"] == "propose_plan"
    assert finished["success"] is False
    assert "unknown action" in finished["error"]


def test_tool_rounds_are_bounded(monkeypatch):
    """A model that keeps calling tools must not spin against the robot's read
    path. It gets a truthful 'I didn't finish' rather than an invented summary."""
    a = Assistant(OT2Service(dry_run=True), PlanStore(), _config())
    _fake_openai(
        monkeypatch,
        [_tool_call("get_status", {}, f"c{i}") for i in range(Assistant.MAX_TOOL_ROUNDS + 2)],
    )

    result = a.chat([{"role": "user", "content": "loop"}])

    assert "wasn't able to finish" in result["reply"]
    assert len(result["tools_used"]) == Assistant.MAX_TOOL_ROUNDS


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_chat_requires_the_claim(monkeypatch):
    """Not because the assistant can move anything — it cannot — but a proposal
    is only useful to whoever holds the device, and this stops a passer-by
    spending the lab's API budget on a robot they do not control."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-test")
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))

    resp = client.post("/assistant/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 423

    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]
    _fake_openai(monkeypatch, [_text("Hello.")])
    ok = client.post("/assistant/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers={"X-Claim-Token": token})
    assert ok.status_code == 200
    assert ok.json()["reply"] == "Hello."


def test_stream_endpoint_emits_sse_progress(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-test")
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))
    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]
    _fake_openai(monkeypatch, [_tool_call("get_status", {}), _text("Hello.")])

    response = client.post(
        "/assistant/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Claim-Token": token},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert any(event["type"] == "tool_started" for event in events)
    assert events[-1]["type"] == "complete"
    assert events[-1]["result"]["reply"] == "Hello."


@pytest.mark.parametrize("enforce_claims", [False, True])
def test_chat_is_503_when_unconfigured(monkeypatch, enforce_claims):
    """Availability is answered before the claim gate.

    With claims on, the gate used to shadow this: an unconfigured gateway
    replied "take control of the device", advice that leads nowhere because
    taking the claim would not conjure an assistant. The caller gets the reason
    they actually hit.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(create_app(dry_run=True, enforce_claims=enforce_claims, ui=False))

    resp = client.post("/assistant/chat", json={"messages": [{"role": "user", "content": "hi"}]})

    assert resp.status_code == 503
    assert "API key" in resp.json()["detail"]


def test_message_history_is_not_capped_but_must_not_be_empty():
    """No length limit on a conversation (decided 2026-10-05): a long one
    passes validation — here it reaches "assistant not configured" (503) —
    and fit_history trims what the model sees. Empty is still refused."""
    client = TestClient(create_app(dry_run=True, enforce_claims=False, ui=False))
    many = {"messages": [{"role": "user", "content": "x" * 9000} for _ in range(41)]}
    assert client.post("/assistant/chat", json=many).status_code != 422
    assert client.post("/assistant/chat", json={"messages": []}).status_code == 422


# ---------------------------------------------------------------------------
# .env fallback
#
# It exists so switching the assistant on needs no `nssm set
# AppEnvironmentExtra`, which replaces the ENTIRE variable block — get that
# wrong and you wipe the three state paths that stop two robots from
# overwriting each other's plate and tip records.
# ---------------------------------------------------------------------------


def _write_env(tmp_path, body: str):
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


def test_missing_env_file_is_a_no_op(tmp_path):
    assert assistant_mod.load_env_file(tmp_path / "nope.env") == {}


def test_reads_an_allowlisted_key(tmp_path):
    env = _write_env(tmp_path, "OPENROUTER_API_KEY=k-from-file\n")
    assert assistant_mod.load_env_file(env) == {"OPENROUTER_API_KEY": "k-from-file"}


def test_ignores_comments_blanks_export_and_quotes(tmp_path):
    env = _write_env(
        tmp_path,
        "\n# a comment\n\nexport OPENROUTER_API_KEY=\"k-quoted\"\nnot-a-pair\n",
    )
    assert assistant_mod.load_env_file(env) == {"OPENROUTER_API_KEY": "k-quoted"}


def test_non_allowlisted_keys_are_refused(tmp_path):
    """The load-bearing restriction. This file sits at the repo root, which BOTH
    gateway instances share, so it must not be able to hand two robots the same
    host alias or the same state path — shared state files corrupt each other.
    Anything instance-specific stays in the NSSM env, which a file cannot reach.
    """
    env = _write_env(
        tmp_path,
        "OT2_HOST_ALIAS=wrong-robot\n"
        "OT2_TIP_STATE_PATH=/shared/tips.json\n"
        "OT2_EQUIPMENT_ID=ot2\n"
        "OPENROUTER_API_KEY=k-ok\n",
    )
    assert assistant_mod.load_env_file(env) == {"OPENROUTER_API_KEY": "k-ok"}


def test_environment_beats_the_file(tmp_path, monkeypatch):
    _write_env(tmp_path, "OPENROUTER_API_KEY=k-file\nOT2_ASSISTANT_MODEL=file/model\n")
    monkeypatch.setenv("OT2_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-env")
    monkeypatch.delenv("OT2_ASSISTANT_MODEL", raising=False)

    cfg = AssistantConfig.from_env()

    assert cfg.api_key == "k-env"          # environment wins
    assert cfg.key_source == "environment"
    assert cfg.model == "file/model"       # ...per setting, not all-or-nothing


def test_the_file_alone_configures_the_assistant(tmp_path, monkeypatch):
    _write_env(tmp_path, "OPENROUTER_API_KEY=k-file\n")
    monkeypatch.setenv("OT2_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    cfg = AssistantConfig.from_env()

    assert cfg.unavailable_reason() is None
    assert cfg.key_source == "file"


def test_health_reports_where_the_key_came_from_but_never_the_key(tmp_path, monkeypatch):
    """The only question an operator asks when the bubble stays hidden after
    they dropped a key somewhere: did it see it?"""
    _write_env(tmp_path, "OPENROUTER_API_KEY=k-secret-value\n")
    monkeypatch.setenv("OT2_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))

    body = client.get("/assistant/health").json()

    assert body["configured"] is True
    assert body["key_source"] == "file"
    assert "k-secret-value" not in json.dumps(body)


def test_a_malformed_file_does_not_take_the_gateway_down(tmp_path, monkeypatch):
    _write_env(tmp_path, "\x00\x01 garbage ===== \nOPENROUTER_API_KEY\n")
    monkeypatch.setenv("OT2_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))

    assert client.get("/status").status_code == 200
    assert client.get("/assistant/health").json()["configured"] is False


def test_searched_paths_are_deduped(monkeypatch):
    """Run from a checkout — how the NSSM services run — and both candidates
    are the same file. /assistant/health publishes this list, and one path
    listed twice reads like a bug to whoever is hunting for where the key goes."""
    monkeypatch.delenv("OT2_ENV_FILE", raising=False)
    paths = assistant_mod.env_file_candidates()
    assert len(paths) == len(set(paths))


def test_explicit_env_file_is_the_only_candidate(monkeypatch, tmp_path):
    monkeypatch.setenv("OT2_ENV_FILE", str(tmp_path / "custom.env"))
    assert assistant_mod.env_file_candidates() == [tmp_path / "custom.env"]


@pytest.mark.parametrize("field,value", [("MAX_TOKENS", "bad"), ("MAX_TOKENS", "-1"),
                                        ("TIMEOUT_S", "nan"), ("TIMEOUT_S", "0")])
def test_bad_numeric_config_disables_only_assistant(monkeypatch, field, value):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
    monkeypatch.setenv(f"OT2_ASSISTANT_{field}", value)
    client = TestClient(create_app(dry_run=True, auto_reconnect=False))
    health = client.get("/assistant/health")
    assert health.status_code == 200
    assert health.json()["configured"] is False
    assert field in health.json()["reason"]
    assert client.get("/status").status_code == 200


def test_openai_key_without_explicit_provider_is_not_sent_to_openrouter(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OT2_ASSISTANT_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    assert "OT2_ASSISTANT_BASE_URL" in AssistantConfig.from_env().unavailable_reason()


def test_environment_key_precedes_other_provider_key_from_file(tmp_path, monkeypatch):
    _write_env(tmp_path, "OPENROUTER_API_KEY=file-key\n")
    monkeypatch.setenv("OT2_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setenv("OT2_ASSISTANT_BASE_URL", "http://provider.invalid/v1")
    config = AssistantConfig.from_env()
    assert config.api_key == "environment-key" and config.key_source == "environment"


def test_model_client_closed_after_completion_and_exception(monkeypatch):
    calls = _fake_openai(monkeypatch, [_text("hello")])
    assistant = Assistant(OT2Service(dry_run=True), PlanStore(), _config())
    assistant.chat([{"role": "user", "content": "hi"}])
    calls["client"].close.assert_called_once()
    with pytest.raises(IndexError):
        assistant.chat([{"role": "user", "content": "hi again"}])
    assert calls["client"].close.call_count == 2


def test_claim_loss_after_model_response_cannot_create_draft(monkeypatch):
    calls = _fake_openai(monkeypatch, [_tool_call("propose_plan", {"steps": [{"action": "home", "args": {}}]})])
    authorization = Mock(side_effect=[None, RuntimeError("claim lost")])
    plans = PlanStore()
    assistant = Assistant(OT2Service(dry_run=True), plans, _config(), ensure_authorized=authorization)
    with pytest.raises(RuntimeError, match="claim lost"):
        assistant.chat([{"role": "user", "content": "home"}])
    assert plans.list() == []
    calls["client"].close.assert_called_once()


def test_flex_prompt_does_not_assume_ot2_fixed_trash(monkeypatch):
    from opentrons_server.gateway.robot_profile import profile_for
    monkeypatch.setattr(assistant_mod, "PROFILE", profile_for("Flex"))
    monkeypatch.setattr(assistant_mod, "IS_FLEX", True)
    calls = _fake_openai(monkeypatch, [_text("hello")])
    Assistant(OT2Service(dry_run=True), PlanStore(), _config()).chat([{"role": "user", "content": "hi"}])
    prompt = calls["sent"][0]["messages"][0]["content"]
    assert "Opentrons Flex" in prompt and "no assumed fixed trash" in prompt
    assert "force_direct=true" in prompt
    assert "`stop`" in prompt


# ---------------------------------------------------------------------------
# Operator-selectable model
# ---------------------------------------------------------------------------


def test_claude_code_subscription_is_a_separate_model_choice(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OT2_ASSISTANT_CLAUDE_PATH", "/opt/claude")
    monkeypatch.setattr(assistant_mod.shutil, "which", lambda path: path)
    monkeypatch.setattr(assistant_mod, "claude_code_authenticated", lambda _: True)

    config = AssistantConfig.from_env()
    assert config.model == "claude-sonnet-5-5"
    assert config.choices == ("claude-sonnet-5-5",)
    assert config.unavailable_reason() is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-test")
    with_openrouter = AssistantConfig.from_env()
    assert with_openrouter.model == assistant_mod.DEFAULT_MODEL
    assert "claude-sonnet-5-5" in with_openrouter.choices
    assert with_openrouter.with_model("claude-sonnet-5-5").unavailable_reason() is None


def test_claude_code_unavailable_for_an_unauthenticated_service_account(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OT2_ASSISTANT_CLAUDE_PATH", "/opt/claude")
    monkeypatch.setattr(assistant_mod.shutil, "which", lambda path: path)
    monkeypatch.setattr(assistant_mod, "claude_code_authenticated", lambda _: False)

    config = AssistantConfig.from_env()
    assert config.model == "claude-sonnet-5-5"
    assert "not authenticated" in config.unavailable_reason()


def test_claude_code_still_rejects_bad_timeout(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OT2_ASSISTANT_CLAUDE_PATH", "/opt/claude")
    monkeypatch.setenv("OT2_ASSISTANT_TIMEOUT_S", "nan")
    monkeypatch.setattr(assistant_mod.shutil, "which", lambda path: path)
    monkeypatch.setattr(assistant_mod, "claude_code_authenticated", lambda _: True)

    assert "TIMEOUT_S" in AssistantConfig.from_env().unavailable_reason()


def test_claude_code_can_run_when_openai_key_has_no_provider(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OT2_ASSISTANT_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unused-key")
    monkeypatch.setenv("OT2_ASSISTANT_CLAUDE_PATH", "/opt/claude")
    monkeypatch.setattr(assistant_mod.shutil, "which", lambda path: path)
    monkeypatch.setattr(assistant_mod, "claude_code_authenticated", lambda _: True)

    config = AssistantConfig.from_env()
    assert config.model == "claude-sonnet-5-5"
    assert config.choices == ("claude-sonnet-5-5",)
    assert config.unavailable_reason() is None


def test_claude_code_runs_without_builtin_tools_or_persisted_session(monkeypatch):
    captured = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")

    def fake_run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "structured_output": {"reply": "The balance is ready.", "steps": []},
        }))

    monkeypatch.setattr(claude_mod.subprocess, "run", fake_run)
    result = claude_mod.run_claude_code(
        "/opt/claude", "You may only propose drafts.", {"messages": []}, 60,
    )
    assert result == {"reply": "The balance is ready.", "steps": []}
    assert captured["command"][:4] == ["/opt/claude", "-p", "--model", "claude-sonnet-5-5"]
    assert captured["command"][captured["command"].index("--tools") + 1] == ""
    assert {"--safe-mode", "--strict-mcp-config", "--no-session-persistence"} <= set(captured["command"])
    assert captured["kwargs"]["input"] == '{"messages": []}'
    assert "ANTHROPIC_API_KEY" not in captured["kwargs"]["env"]


def test_claude_code_pipes_are_utf8_on_every_platform(monkeypatch):
    """Windows decodes subprocess text with the ANSI code page unless told
    otherwise, which turned the CLI's UTF-8 `↔` into `â†”` for the operator."""
    captured = {}

    def fake_run(command, **kwargs):
        captured["run"] = kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "structured_output": {"reply": "A1↔H12", "steps": []},
        }))

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["popen"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        returncode = 0

        def poll(self):
            return 0

        def communicate(self, **_kwargs):
            return json.dumps({"structured_output": {"reply": "A1↔H12", "steps": []}}), ""

    monkeypatch.setattr(claude_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(claude_mod.subprocess, "Popen", FakePopen)
    assert claude_mod.run_claude_code("/opt/claude", "s", {"messages": []}, 5)["reply"] == "A1↔H12"
    assert claude_mod.run_claude_code("/opt/claude", "s", {"messages": []}, 5, threading.Event())["reply"] == "A1↔H12"
    claude_mod.claude_code_authenticated("/opt/claude")
    for kwargs in captured.values():
        assert kwargs["text"] is True
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"


def test_claude_code_rejects_an_api_key_login(monkeypatch):
    monkeypatch.setattr(claude_mod.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps({
                            "loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty",
                        })))
    assert claude_mod.claude_code_authenticated("/opt/claude") is False


def test_claude_code_timeout_is_reported_clearly(monkeypatch):
    import subprocess
    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("claude", 12)
    monkeypatch.setattr(claude_mod.subprocess, "run", timed_out)
    with pytest.raises(TimeoutError, match="did not reply within 12 seconds"):
        claude_mod.run_claude_code("/opt/claude", "test", {"messages": []}, 12)


def test_cancel_kills_the_claude_code_process(monkeypatch):
    import subprocess
    import threading

    canceled = threading.Event()

    class FakeClaudeProcess:
        killed = False
        returncode = -9

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

        def communicate(self, **_kwargs):
            if self.killed:
                return "", ""
            canceled.set()
            raise subprocess.TimeoutExpired("claude", 0.25)

    process = FakeClaudeProcess()
    monkeypatch.setattr(claude_mod.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(InterruptedError, match="stopped"):
        claude_mod.run_claude_code("/opt/claude", "test", {"messages": []}, 12, canceled)
    assert process.killed


def test_claude_code_can_only_create_a_validated_draft(monkeypatch):
    monkeypatch.setattr(assistant_mod, "run_claude_code", lambda *_args: {
        "reply": "I proposed a draft for approval.",
        "steps": [{"action": "comment", "args": {"message": "test draft"}}],
    })
    config = _config(api_key=None, model="claude-sonnet-5-5",
                     claude_code_path="/opt/claude", claude_code_ready=True)
    plans = PlanStore()
    result = Assistant(OT2Service(dry_run=True), plans, config).chat([
        {"role": "user", "content": "propose a comment"},
    ])
    assert result["plan_id"] is not None
    assert plans.get(result["plan_id"]).status == "draft"
    assert result["tools_used"][-1] == "propose_plan"


def test_cancel_before_claude_proposal_creates_no_draft(monkeypatch):
    def stopped_before_proposal(_path, _system, _context, _timeout, cancel_event):
        cancel_event.set()
        return {"reply": "draft", "steps": [{"action": "comment", "args": {"message": "no"}}]}

    monkeypatch.setattr(assistant_mod, "run_claude_code", stopped_before_proposal)
    config = _config(api_key=None, model="claude-sonnet-5-5",
                     claude_code_path="/opt/claude", claude_code_ready=True)
    plans = PlanStore()
    with pytest.raises(AssistantCancelled):
        Assistant(OT2Service(dry_run=True), plans, config).chat([
            {"role": "user", "content": "propose a comment"},
        ])
    assert plans.list() == []


def test_health_offers_the_default_model_choices_first(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-test")
    monkeypatch.setenv("OT2_ASSISTANT_MODEL", "z-ai/glm-5.3-flash")
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))

    body = client.get("/assistant/health").json()

    assert body["model"] == "z-ai/glm-5.3-flash"
    assert body["models"][0] == "z-ai/glm-5.3-flash"
    assert set(body["models"]) == {"z-ai/glm-5.3-flash", *assistant_mod.DEFAULT_MODEL_CHOICES}
    assert len(body["models"]) == len(set(body["models"]))


def test_models_setting_replaces_the_default_choices(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-test")
    monkeypatch.setenv("OT2_ASSISTANT_MODELS", " a/one , ,b/two")

    assert AssistantConfig.from_env().choices == (assistant_mod.DEFAULT_MODEL, "a/one", "b/two")


def test_custom_provider_offers_only_its_configured_model(monkeypatch):
    """OpenRouter slugs mean nothing to another provider."""
    monkeypatch.setenv("OPENAI_API_KEY", "k-test")
    monkeypatch.setenv("OT2_ASSISTANT_BASE_URL", "http://provider.invalid/v1")
    monkeypatch.setenv("OT2_ASSISTANT_MODEL", "local/model")

    assert AssistantConfig.from_env().choices == ("local/model",)


def test_health_offers_no_models_when_unconfigured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))

    assert client.get("/assistant/health").json()["models"] == []


def test_chat_uses_the_requested_model(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-test")
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))
    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]
    calls = _fake_openai(monkeypatch, [_text("Hello.")])

    resp = client.post(
        "/assistant/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "model": "z-ai/glm-5.3-flash"},
        headers={"X-Claim-Token": token},
    )

    assert resp.status_code == 200
    assert calls["sent"][0]["model"] == "z-ai/glm-5.3-flash"


@pytest.mark.parametrize("path", ["/assistant/chat", "/assistant/chat/stream"])
def test_a_model_outside_the_allowlist_is_refused_not_substituted(monkeypatch, path):
    """Free text would let whoever holds the claim pick a model with no tool
    support, one the guardrail blocks, or one that bills far more."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-test")
    client = TestClient(create_app(dry_run=True, enforce_claims=True, ui=False))
    token = client.post("/control/claim", json=CLAIM).json()["claim_token"]
    calls = _fake_openai(monkeypatch, [_text("Hello.")])

    resp = client.post(
        path,
        json={"messages": [{"role": "user", "content": "hi"}], "model": "openai/o9-ultra"},
        headers={"X-Claim-Token": token},
    )

    assert resp.status_code == 422
    assert "not offered" in resp.json()["detail"]
    assert calls["sent"] == []


def test_plate_report_tool_reads_plan_records_only():
    import json as _json
    from pathlib import Path as _Path
    from opentrons_server.gateway.plans import Plan, PlanStore
    store = PlanStore()
    fixture = _Path(__file__).resolve().parent.parent / "fixtures" / "plans_balance_run_split.json"
    for raw in _json.loads(fixture.read_text()):
        plan = Plan.model_validate(raw)
        store._plans[plan.plan_id] = plan
    service = Mock()
    assistant = assistant_mod.Assistant.__new__(assistant_mod.Assistant)
    assistant._service, assistant._plans = service, store
    from opentrons_server.gateway.run_access import RunReader
    assistant._reader = RunReader(unrestricted=True)  # built without __init__
    out = assistant._tools()["get_plate_report"]({"plan_ids": ["gS0maKvThQsH_p2S", "C5B-GLbrcj7Q6rII"]})
    assert out["stats"]["n"] == 31
    assert service.method_calls == []  # no robot or gateway call at all
    context = assistant._plate_reports_for_context()
    assert {r["plan_id"] for r in context} == {"gS0maKvThQsH_p2S", "C5B-GLbrcj7Q6rII"}


# ── conversation length (2026-10-05: "messages.7.content: String should have
#    at most 8000 characters" broke every turn after one long reply) ──────


def test_a_long_message_and_a_long_conversation_are_accepted():
    from opentrons_server.gateway.assistant import AssistantChatRequest

    long_reply = "x" * 50_000
    messages = [{"role": "user", "content": "q"}, {"role": "assistant", "content": long_reply}] * 40
    request = AssistantChatRequest(messages=messages + [{"role": "user", "content": "next?"}])
    assert len(request.messages) == 81


def test_fit_history_keeps_the_newest_and_says_what_it_left_out():
    from opentrons_server.gateway.assistant import fit_history

    history = [{"role": "user", "content": "a" * 100}, {"role": "assistant", "content": "b" * 100},
               {"role": "user", "content": "c" * 100}]
    assert fit_history(history, budget=1000) == history  # all fit: unchanged
    fitted = fit_history(history, budget=250)
    assert [m["content"][:1] for m in fitted[1:]] == ["b", "c"]
    assert "1 earlier message" in fitted[0]["content"]
    huge = [{"role": "user", "content": "old"}, {"role": "user", "content": "z" * 10_000}]
    fitted = fit_history(huge, budget=100)
    assert fitted[-1]["content"] == "z" * 10_000  # the newest is kept whole
