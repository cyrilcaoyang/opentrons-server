"""Wiring tests for the opt-in HTTP (run-engine) transport in OT2Service.

The default transport is SSH and is covered elsewhere; these tests exercise only
the ``transport="http"`` branch, with a fake RunEngineClient so no network or
robot is touched.
"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from opentrons_server.control.http_control import OT2HttpControl
from opentrons_server.gateway.service import OT2Service, OT2ServiceState

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_state_files(tmp_path, monkeypatch):
    """Anchor the default plate/deck stores under tmp (they resolve relative
    paths against the repo root, not cwd — see test_deck_status)."""
    from opentrons_server.gateway import deck as deck_mod
    from opentrons_server.gateway import plate_state as plate_mod

    def _isolated(state_path):
        return tmp_path / Path(state_path).name

    monkeypatch.setattr(deck_mod, "_resolve_state_path", _isolated)
    monkeypatch.setattr(plate_mod, "_resolve_state_path", _isolated)


class FakeClient:
    def __init__(self, base_url=None):
        self.base_url = base_url
        self.commands = []
        self.created = False
        self.stopped = False
        self.closed = False
        self.run_id = None

    def create_run(self):
        self.created = True
        self.run_id = "run-1"
        return "run-1"

    def execute(self, command, **kwargs):
        self.commands.append(command)
        return {"status": "succeeded"}

    def add_labware_definition(self, definition):
        return "custom/plate/1"

    def get_run(self):
        return {"id": "run-1", "labware": []}

    def get_current_state(self):
        return getattr(self, 'current_state', {})

    def stop_run(self):
        self.stopped = True

    def close(self):
        self.closed = True


@pytest.fixture
def fake_http(monkeypatch):
    fake = FakeClient()
    monkeypatch.setenv("OT2_HTTP_BASE_URL", "http://robot:31950")
    monkeypatch.setattr("opentrons_server.gateway.service.RunEngineClient", lambda base_url: fake)
    # keep _refresh_identity hermetic (it would otherwise hit the network)
    monkeypatch.setattr(OT2Service, "probe_robot", lambda self: {"reachable": False})
    return fake


def test_default_transport_is_ssh():
    assert OT2Service(dry_run=True).transport == "ssh"


def test_http_startup_uses_http_control_and_creates_run(fake_http):
    service = OT2Service(dry_run=False, transport="http")

    service.startup()

    assert service.state == OT2ServiceState.READY
    assert isinstance(service.control, OT2HttpControl)
    assert fake_http.created is True
    # snapshot came from the run engine, not a REPL invoke
    assert service.last_snapshot.get("run_id") == "run-1"


def test_http_transport_requires_a_base_url(monkeypatch):
    monkeypatch.delenv("OT2_HTTP_BASE_URL", raising=False)
    monkeypatch.setattr(OT2Service, "probe_robot", lambda self: {"reachable": False})
    service = OT2Service(dry_run=False, transport="http", host_alias=None)

    with pytest.raises(Exception):
        service.startup()
    assert service.state == OT2ServiceState.ERROR


def test_http_aspirate_flows_through_to_a_command(fake_http):
    service = OT2Service(dry_run=False, transport="http")
    service.startup()
    service.state = OT2ServiceState.READY

    # load a pipette + labware, then aspirate through the normal service surface
    service.control.load_instrument(
        {
            "ot_default": True,
            "nickname": "p300",
            "instrument_name": "p300_single_gen2",
            "mount": "right",
        }
    )
    service.control.load_labware(
        {
            "ot_default": True,
            "nickname": "plate",
            "loadname": "corning_96_wellplate_360ul_flat",
            "location": "2",
        }
    )

    from opentrons_server.gateway.models import LiquidMoveRequest, WellLocation

    service.aspirate(
        LiquidMoveRequest(
            pipette="p300",
            volume_ul=50,
            location=WellLocation(labware_nickname="plate", position="A1", bottom=2),
        )
    )

    command_types = [c[0] for c in fake_http.commands]
    assert "aspirate" in command_types
    aspirate = next(c for c in fake_http.commands if c[0] == "aspirate")
    assert aspirate[1]["labwareId"] == "plate"
    assert aspirate[1]["wellName"] == "A1"


def _http_service_with_pipette(fake_http):
    service = OT2Service(dry_run=False, transport="http")
    service.startup()
    service.state = OT2ServiceState.READY
    service.control.load_instrument(
        {"ot_default": True, "nickname": "p300", "instrument_name": "p300_single_gen2", "mount": "right"}
    )
    return service


def test_http_drop_tip_with_location_targets_that_labware(fake_http):
    # An explicit labware+well on /control/drop-tip (e.g. a loaded trash) makes
    # HTTP drop INTO that well rather than dropTipInPlace — the drop-to-trash path.
    from opentrons_server.gateway.models import TipRequest

    service = _http_service_with_pipette(fake_http)
    service.control.load_labware(
        {"ot_default": True, "nickname": "trash", "loadname": "opentrons_1_trash_1100ml_fixed", "location": "12"}
    )

    service.drop_tip(TipRequest(pipette="p300", labware_nickname="trash", position="A1"))

    drop = next(c for c in fake_http.commands if c[0] in ("dropTip", "dropTipInPlace"))
    assert drop[0] == "dropTip"
    assert drop[1]["labwareId"] == "trash"
    assert drop[1]["wellName"] == "A1"


def test_http_drop_tip_without_location_routes_to_the_fixed_trash(fake_http):
    # No location -> the fixed trash, which startup registered as the
    # fixedTrash addressable area (this fake, like modern robot-servers,
    # neither preloads a trash labware nor allows loading into slot 12).
    from opentrons_server.gateway.models import TipRequest

    service = _http_service_with_pipette(fake_http)

    service.drop_tip(TipRequest(pipette="p300"))

    move = next(c for c in fake_http.commands if c[0] == "moveToAddressableAreaForDropTip")
    assert move[1]["addressableAreaName"] == "fixedTrash"
    drop = next(c for c in fake_http.commands if c[0] in ("dropTip", "dropTipInPlace"))
    assert drop[0] == "dropTipInPlace"


def test_http_pick_refused_when_the_engine_reports_a_tip(fake_http):
    """Live failure on ot2_complexation 2026-10-02: the engine still held a tip
    from before a restart, the ledger showed none, and the first pickUpTip of a
    192-step plan failed with UnexpectedTipAttachError. Now refused before any
    command is sent, with the state left READY."""
    from opentrons_server.gateway.models import TipRequest
    from opentrons_server.gateway.tip_state import TipUnavailable

    service = _http_service_with_pipette(fake_http)
    service.control.load_labware(
        {"ot_default": True, "nickname": "tips", "loadname": "opentrons_96_tiprack_300ul", "location": "8"}
    )
    fake_http.current_state = {"tipStates": {"p300": {"hasTip": True}}}
    before = list(fake_http.commands)

    with pytest.raises(TipUnavailable) as excinfo:
        service.pick_up_tip(TipRequest(pipette="p300", labware_nickname="tips", position="A1"))

    assert excinfo.value.body["robot_reports_tip"] is True
    assert fake_http.commands == before  # nothing moved
    assert service.state == OT2ServiceState.READY
    assert service.last_error is None

    # The documented recovery: a plain drop clears the engine's record.
    service.drop_tip(TipRequest(pipette="p300"))
    assert fake_http.commands[-1][0] == "dropTipInPlace"

    # And the mirror image: the engine reports a bare head, so a drop is refused.
    fake_http.current_state = {"tipStates": {"p300": {"hasTip": False}}}
    before = list(fake_http.commands)
    with pytest.raises(TipUnavailable) as excinfo:
        service.drop_tip(TipRequest(pipette="p300"))
    assert excinfo.value.body["robot_reports_tip"] is False
    assert fake_http.commands == before


def test_http_snapshot_carries_the_engines_tip_belief(fake_http):
    service = _http_service_with_pipette(fake_http)
    fake_http.get_run = lambda: {
        "id": "run-1", "labware": [],
        "pipettes": [{"id": "p300", "mount": "right", "pipetteName": "p300_single_gen2"}],
    }
    fake_http.current_state = {"tipStates": {"p300": {"hasTip": True}}}
    assert service.refresh_snapshot()["pipettes"]["right"]["has_tip"] is True
    fake_http.current_state = {}
    assert "has_tip" not in service.refresh_snapshot()["pipettes"]["right"]
    assert service.last_error is None


def test_http_snapshot_populates_deck_parity_from_run(fake_http):
    # The run resource carries loaded labware; the deck tile should reflect it
    # via the `run` source (build_deck precedence run > repl > declared).
    run_doc = json.loads((_FIXTURES / "robot_run_labware.json").read_text())
    fake_http.get_run = lambda: {"id": "run-1", "pipettes": [], **run_doc}

    service = OT2Service(dry_run=False, transport="http")
    service.startup()
    service.state = OT2ServiceState.READY
    service.refresh_snapshot()

    deck = service.get_status().details["snapshot"]["deck"]
    assert deck["source"] == "run"
    assert deck["slots"]["1"]["labware"] is not None  # tiprack
    assert deck["slots"]["2"]["labware"] is not None  # reaction plate
    # and the raw passthrough carries the run's labware list
    assert service.last_snapshot["run_id"] == "run-1"
    assert len(service.last_snapshot["labwares"]) == len(run_doc["labware"])
    # container-shape parity with the SSH snapshot: slot-keyed dicts, not lists
    assert isinstance(service.last_snapshot["labwares"], dict)
    assert isinstance(service.last_snapshot["pipettes"], dict)
    assert isinstance(service.last_snapshot["modules"], dict)


def test_failed_setup_leaves_session_recipe_unchanged(fake_http):
    # BUG 1 regression: setup_protocol commits session_recipe before the load
    # runs. If the load fails, the recipe must roll back — otherwise the gateway
    # describes labware the run never loaded (the divergence that made the
    # assistant propose ghost nicknames the run engine 409s).
    service = OT2Service(dry_run=False, transport="http")
    service.startup()
    service.state = OT2ServiceState.READY

    good = {
        "labware": [
            {"ot_default": True, "nickname": "tips", "loadname": "opentrons_96_tiprack_300ul", "location": "1"}
        ],
        "instruments": [
            {"ot_default": True, "nickname": "p300", "instrument_name": "p300_single_gen2", "mount": "right"}
        ],
        "modules": [],
    }
    service.setup_protocol(good)
    assert service.session_recipe["labware"][0]["nickname"] == "tips"
    committed = service.session_recipe

    # The next setup's load fails at the run engine (e.g. LocationIsOccupied on a
    # slot a partial run already holds).
    def _boom(command, **kwargs):
        raise RuntimeError("409: LocationIsOccupiedError")

    fake_http.execute = _boom
    with pytest.raises(Exception):
        service.setup_protocol(
            {
                "labware": [
                    {"ot_default": True, "nickname": "ghost", "loadname": "corning_96_wellplate_360ul_flat", "location": "2"}
                ],
                "instruments": [],
                "modules": [],
            }
        )

    # Recipe is exactly what the successful setup committed — no "ghost".
    assert service.session_recipe == committed
    assert all(lw["nickname"] != "ghost" for lw in service.session_recipe["labware"])
    assert service.state == OT2ServiceState.ERROR


def test_http_snapshot_keys_entries_by_slot_with_id_fallback(fake_http):
    fake_http.get_run = lambda: {
        "id": "run-1",
        "pipettes": [{"id": "p300", "mount": "right", "pipetteName": "p300_single_gen2"}],
        "labware": [
            {"id": "tips", "location": {"slotName": "1"}},
            {"id": "plate", "location": "offDeck"},  # slotless -> keyed by id
        ],
        "modules": [],
    }

    service = OT2Service(dry_run=False, transport="http")
    service.startup()
    service.refresh_snapshot()

    snapshot = service.last_snapshot
    assert snapshot["pipettes"]["right"]["pipetteName"] == "p300_single_gen2"
    assert snapshot["labwares"]["1"]["id"] == "tips"
    assert snapshot["labwares"]["plate"]["location"] == "offDeck"


def test_http_transport_reports_no_ssh_session(fake_http):
    """Under the HTTP transport there is no SSH session, so the component
    named after one must not claim to be connected.

    It used to: `connected` was `self.control is not None`, so the pill read
    "SSH connected" on a gateway that had never opened an SSH socket. The
    transport in use is now carried by the `control` component instead.
    """
    service = OT2Service(dry_run=False, transport="http")
    service.startup()

    components = service.get_status().components
    assert components["ssh"].connected is False
    assert components["ssh"].state == "disconnected"
    assert "run engine" in (components["ssh"].message or "")
    assert "no SSH session" in components["ssh"].message

    assert components["control"].state == "http"
    # The `fake_http` fixture stubs probe_robot to unreachable, and the HTTP
    # transport holds no session — so the robot answering a probe is the only
    # liveness evidence there is, and here there is none. Reporting `False`
    # rather than "an object exists, so we're fine" is the whole point.
    assert components["control"].connected is False


def test_ssh_transport_ssh_component_message_names_repl():
    service = OT2Service(dry_run=False)  # default transport: ssh
    components = service.get_status().components
    assert "SSH REPL" in (components["ssh"].message or "")
    assert components["control"].state == "disconnected"  # no session yet


def test_control_component_reports_observed_ssh_liveness():
    """A dropped session must read as down. The object outlives the socket, so
    asking paramiko is the only honest answer."""
    service = OT2Service(dry_run=False)
    service.control = Mock()
    service.control.client.is_alive.return_value = True
    assert service.get_status().components["control"].connected is True
    assert service.get_status().components["ssh"].state == "connected"

    service.control.client.is_alive.return_value = False
    assert service.get_status().components["control"].connected is False
    assert service.get_status().components["ssh"].state == "disconnected"


def test_dry_run_reports_its_simulation_as_the_transport():
    """A simulated device behaves identically on every axis (STATUS_SPEC
    Appendix B.1), so the session reads connected — but `control` says plainly
    that it is a simulation rather than a real backend."""
    components = OT2Service(dry_run=True).get_status().components
    assert components["control"].state == "dry_run"
    assert components["ssh"].connected is True


def test_http_shutdown_stops_and_closes(fake_http):
    service = OT2Service(dry_run=False, transport="http")
    service.startup()

    service.shutdown()

    assert fake_http.stopped is True
    assert fake_http.closed is True
    assert service.control is None
