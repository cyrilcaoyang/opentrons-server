"""Unit tests for the run-engine HTTP client (control/http_run.py).

All tests run offline against a fake requests.Session that records calls and
replays queued responses — no robot required.
"""

import json

import pytest
import requests

from opentrons_server.control.http_run import (
    OFF_DECK,
    CommandFailed,
    CommandNotCompleted,
    RunEngineClient,
)
from opentrons_server.control.http_run import RunEngineCommands as C
from opentrons_server.control.http_run import (
    RunEngineError,
    RunEngineHTTPError,
    RunEngineUnreachable,
    deck_slot,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Records requests and returns queued responses (or one repeatedly)."""

    def __init__(self, responses=None, raises=None):
        self._responses = list(responses or [])
        self._raises = raises
        self.calls = []
        self.closed = False

    def request(self, method, url, *, json=None, params=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if self._raises is not None:
            raise self._raises
        if not self._responses:
            raise AssertionError("no queued response for request")
        resp = self._responses.pop(0)
        return resp

    def close(self):
        self.closed = True


def _client(session):
    return RunEngineClient("http://robot:31950/", session=session)


# --- command builders -------------------------------------------------------


def test_deck_slot_and_offdeck_literal():
    assert deck_slot(3) == {"slotName": "3"}
    assert deck_slot("7") == {"slotName": "7"}
    assert OFF_DECK == "offDeck"


def test_aspirate_builder_shape():
    ctype, params = C.aspirate("pip-1", "lw-1", "A1", 50.0, 150.0, origin="bottom")
    assert ctype == "aspirate"
    assert params["pipetteId"] == "pip-1"
    assert params["labwareId"] == "lw-1"
    assert params["wellName"] == "A1"
    assert params["volume"] == 50.0
    assert params["flowRate"] == 150.0
    assert params["wellLocation"] == {"origin": "bottom", "offset": {"x": 0, "y": 0, "z": 0}}


def test_dispense_builder_includes_push_out_only_when_set():
    _, p1 = C.dispense("pip", "lw", "B2", 20, 100)
    assert "pushOut" not in p1
    _, p2 = C.dispense("pip", "lw", "B2", 20, 100, push_out=5.0)
    assert p2["pushOut"] == 5.0


def test_load_labware_builder_requires_int_version():
    _, params = C.load_labware("corning_96", "custom", 1, deck_slot(2))
    assert params == {
        "location": {"slotName": "2"},
        "loadName": "corning_96",
        "namespace": "custom",
        "version": 1,
    }


def test_move_labware_offdeck_default_strategy():
    ctype, params = C.move_labware("lw-1", OFF_DECK)
    assert ctype == "moveLabware"
    assert params["newLocation"] == "offDeck"
    assert params["strategy"] == "manualMoveWithoutPause"


def test_move_labware_rejects_pause_and_gripper():
    with pytest.raises(ValueError, match="manualMoveWithoutPause"):
        C.move_labware("lw-1", OFF_DECK, strategy="manualMoveWithPause")
    with pytest.raises(ValueError):
        C.move_labware("lw-1", deck_slot(1), strategy="usingGripper")


def test_home_builder_omits_axes_by_default():
    assert C.home() == ("home", {})
    assert C.home(["x", "y"]) == ("home", {"axes": ["x", "y"]})


# --- lifecycle --------------------------------------------------------------


def test_create_run_stores_id_and_sends_envelope():
    session = FakeSession([FakeResponse(201, {"data": {"id": "run-42"}})])
    client = _client(session)

    run_id = client.create_run()

    assert run_id == "run-42"
    assert client.run_id == "run-42"
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://robot:31950/runs"
    assert call["json"] == {"data": {}}
    assert call["headers"]["Opentrons-Version"] == "3"


def test_create_run_without_id_raises():
    session = FakeSession([FakeResponse(201, {"data": {}})])
    with pytest.raises(RunEngineError):
        _client(session).create_run()


def test_execute_requires_run():
    with pytest.raises(RunEngineError, match="no active run"):
        _client(FakeSession()).execute(C.home())


def test_execute_posts_setup_intent_and_wait_params():
    session = FakeSession(
        [
            FakeResponse(201, {"data": {"id": "run-1"}}),
            FakeResponse(201, {"data": {"id": "cmd-1", "status": "succeeded"}}),
        ]
    )
    client = _client(session)
    client.create_run()

    result = client.execute(C.aspirate("pip", "lw", "A1", 10, 100), timeout_ms=30000)

    assert result["status"] == "succeeded"
    cmd_call = session.calls[1]
    assert cmd_call["url"] == "http://robot:31950/runs/run-1/commands"
    assert cmd_call["params"] == {"waitUntilComplete": "true", "timeout": 30000}
    assert cmd_call["json"]["data"]["intent"] == "setup"
    assert cmd_call["json"]["data"]["commandType"] == "aspirate"
    # read timeout must exceed the server-side wait
    assert cmd_call["timeout"] > 30.0


def test_execute_failed_status_raises_command_failed():
    session = FakeSession(
        [
            FakeResponse(201, {"data": {"id": "run-1"}}),
            FakeResponse(
                201,
                {
                    "data": {
                        "id": "cmd-1",
                        "commandType": "aspirate",
                        "status": "failed",
                        "error": {
                            "errorType": "MustHomeError",
                            "errorCode": "3003",
                            "detail": "Must home first",
                        },
                    }
                },
            ),
        ]
    )
    client = _client(session)
    client.create_run()

    with pytest.raises(CommandFailed) as excinfo:
        client.execute(C.aspirate("pip", "lw", "A1", 10, 100))

    assert excinfo.value.error_type == "MustHomeError"
    assert excinfo.value.error_code == "3003"
    assert "Must home first" in str(excinfo.value)


def test_execute_wait_nonterminal_raises_not_completed():
    # waitUntilComplete timed out server-side: 200 with a still-running command.
    session = FakeSession(
        [
            FakeResponse(201, {"data": {"id": "run-1"}}),
            FakeResponse(
                201,
                {"data": {"id": "cmd-1", "commandType": "aspirate", "status": "running"}},
            ),
        ]
    )
    client = _client(session)
    client.create_run()

    with pytest.raises(CommandNotCompleted) as excinfo:
        client.execute(C.aspirate("pip", "lw", "A1", 10, 100), timeout_ms=30000)

    assert excinfo.value.status == "running"
    assert excinfo.value.timeout_ms == 30000
    # Treated as an unknown outcome (OSError) so the gateway routes a
    # non-idempotent action to UNKNOWN_OUTCOME, like a transport loss.
    assert isinstance(excinfo.value, OSError)


def test_execute_no_wait_tolerates_nonterminal_status():
    # Without wait we accept a queued command and must NOT raise.
    session = FakeSession(
        [
            FakeResponse(201, {"data": {"id": "run-1"}}),
            FakeResponse(201, {"data": {"id": "cmd-1", "status": "queued"}}),
        ]
    )
    client = _client(session)
    client.create_run()
    result = client.execute(C.home(), wait=False)
    assert result["status"] == "queued"


def test_execute_no_wait_omits_wait_params():
    session = FakeSession(
        [
            FakeResponse(201, {"data": {"id": "run-1"}}),
            FakeResponse(201, {"data": {"id": "cmd-1", "status": "queued"}}),
        ]
    )
    client = _client(session)
    client.create_run()
    client.execute(C.home(), wait=False)
    # no wait → no waitUntilComplete/timeout query params (sent as None/empty)
    assert not session.calls[1]["params"]


def test_add_labware_definition_returns_uri():
    session = FakeSession(
        [
            FakeResponse(201, {"data": {"id": "run-1"}}),
            FakeResponse(201, {"data": {"definitionUri": "custom/my_plate/1"}}),
        ]
    )
    client = _client(session)
    client.create_run()

    uri = client.add_labware_definition({"parameters": {"loadName": "my_plate"}})

    assert uri == "custom/my_plate/1"
    assert session.calls[1]["url"] == "http://robot:31950/runs/run-1/labware_definitions"
    assert session.calls[1]["json"] == {"data": {"parameters": {"loadName": "my_plate"}}}


def test_stop_run_is_best_effort_on_error():
    session = FakeSession(raises=requests.ConnectionError("down"))
    client = _client(session)
    client.run_id = "run-1"
    # must not raise even though the transport is down
    client.stop_run()


def test_stop_run_noop_without_run():
    session = FakeSession()
    _client(session).stop_run()
    assert session.calls == []


def _run_status(status):
    return FakeResponse(200, {"data": {"id": "run-1", "status": status}})


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr("opentrons_server.control.http_run.time.sleep", sleeps.append)
    return sleeps


def test_stop_run_waits_until_the_run_reads_stopped(no_sleep):
    # The robot acknowledges stop at once but refuses a new run until the old
    # one leaves stop-requested/finishing (HTE, 2026-10-12).
    session = FakeSession(
        [
            FakeResponse(200, {"data": {}}),
            _run_status("stop-requested"),
            _run_status("finishing"),
            _run_status("stopped"),
        ]
    )
    client = _client(session)
    client.run_id = "run-1"
    client.stop_run()
    assert [c["method"] for c in session.calls] == ["POST", "GET", "GET", "GET"]
    assert session.calls[0]["json"] == {"data": {"actionType": "stop"}}
    assert all(c["url"] == "http://robot:31950/runs/run-1" for c in session.calls[1:])
    assert len(no_sleep) == 2


@pytest.mark.parametrize("status", ["stopped", "failed", "succeeded"])
def test_stop_run_returns_on_any_terminal_status(no_sleep, status):
    session = FakeSession([FakeResponse(200, {"data": {}}), _run_status(status)])
    client = _client(session)
    client.run_id = "run-1"
    client.stop_run()
    assert len(session.calls) == 2
    assert no_sleep == []


def test_stop_run_still_waits_when_the_stop_action_is_refused(no_sleep):
    # A 409 on the stop action is also what "already stopping" looks like.
    session = FakeSession(
        [
            FakeResponse(409, {"errors": [{"detail": "Run is stop-requested"}]}),
            _run_status("stop-requested"),
            _run_status("stopped"),
        ]
    )
    client = _client(session)
    client.run_id = "run-1"
    client.stop_run()
    assert [c["method"] for c in session.calls] == ["POST", "GET", "GET"]


def test_stop_run_gives_up_after_the_settle_timeout(no_sleep, monkeypatch, caplog):
    clock = iter([0.0, 0.1, 5.0])
    monkeypatch.setattr("opentrons_server.control.http_run.time.monotonic", lambda: next(clock))
    session = FakeSession([FakeResponse(200, {"data": {}}), _run_status("stop-requested"),
                           _run_status("stop-requested")])
    client = _client(session)
    client.run_id = "run-1"
    with caplog.at_level("WARNING", logger="opentrons_server.control.http_run"):
        client.stop_run(settle_timeout_s=1.0)  # must not raise
    assert len(session.calls) == 3
    assert "still 'stop-requested'" in caplog.text


def test_stop_run_does_not_wait_when_the_status_read_fails(no_sleep):
    session = FakeSession([FakeResponse(200, {"data": {}}), FakeResponse(404, {"errors": [{"detail": "gone"}]})])
    client = _client(session)
    client.run_id = "run-1"
    client.stop_run()  # must not raise
    assert len(session.calls) == 2
    assert no_sleep == []


def test_stop_run_unreachable_sends_one_request_and_no_status_reads():
    session = FakeSession(raises=requests.ConnectionError("down"))
    client = _client(session)
    client.run_id = "run-1"
    client.stop_run()
    assert len(session.calls) == 1


# --- error mapping ----------------------------------------------------------


def test_connection_error_maps_to_unreachable_and_is_oserror():
    session = FakeSession(raises=requests.ConnectionError("refused"))
    with pytest.raises(RunEngineUnreachable) as excinfo:
        _client(session).create_run()
    # subclasses OSError so the gateway's transport-loss handling catches it
    assert isinstance(excinfo.value, OSError)


def test_timeout_maps_to_unreachable():
    session = FakeSession(raises=requests.Timeout("slow"))
    with pytest.raises(RunEngineUnreachable):
        _client(session).create_run()


def test_http_error_surfaces_detail_from_errors_array():
    session = FakeSession(
        [FakeResponse(409, {"errors": [{"detail": "Run is not idle", "title": "RunNotIdle"}]})]
    )
    with pytest.raises(RunEngineHTTPError) as excinfo:
        _client(session).create_run()
    assert excinfo.value.status_code == 409
    assert "Run is not idle" in excinfo.value.detail


def test_close_leaves_borrowed_session_open():
    # The gateway shares one keep-alive session; close() must never tear it down.
    shared = FakeSession()
    c = RunEngineClient("http://robot:31950", session=shared)
    c.close()
    assert shared.closed is False


def test_close_closes_owned_session(monkeypatch):
    # When no session is passed the client owns the one it creates and closes it.
    owned = FakeSession()
    monkeypatch.setattr("opentrons_server.control.http_run.requests.Session", lambda: owned)
    c = RunEngineClient("http://robot:31950")
    c.close()
    assert owned.closed is True
