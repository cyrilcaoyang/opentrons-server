"""Unit tests for OT2HttpControl (control/http_control.py).

Uses a fake RunEngineClient that records executed (commandType, params) tuples,
so the adapter's command translation is verified offline.
"""

import pytest

from opentrons_server.control.http_control import OT2HttpControl


class FakeClient:
    def __init__(self):
        self.commands = []  # list of (commandType, params)
        self.labware_defs = []
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
        self.labware_defs.append(definition)
        return "custom/plate/1"

    def get_run(self):
        return {"id": "run-1", "labware": []}

    def get_current_state(self):
        return getattr(self, 'current_state', {})

    def stop_run(self):
        self.stopped = True

    def close(self):
        self.closed = True


def _loaded_control():
    """Control with a pipette 'p300' and labware 'plate' + 'tips' loaded."""
    client = FakeClient()
    ctl = OT2HttpControl(client, aspirate_flow_rate=100.0, dispense_flow_rate=200.0)
    ctl.initialize_protocol()
    ctl.load_instrument(
        {
            "ot_default": True,
            "nickname": "p300",
            "instrument_name": "p300_single_gen2",
            "mount": "right",
        }
    )
    ctl.load_labware(
        {
            "ot_default": True,
            "nickname": "plate",
            "loadname": "corning_96_wellplate_360ul_flat",
            "location": "2",
        }
    )
    ctl.load_labware(
        {
            "ot_default": True,
            "nickname": "tips",
            "loadname": "opentrons_96_tiprack_300ul",
            "location": "1",
        }
    )
    return ctl, client


def _last(client):
    return client.commands[-1]


# --- loading ----------------------------------------------------------------


def test_initialize_creates_run():
    client = FakeClient()
    OT2HttpControl(client).initialize_protocol()
    assert client.created is True


def test_load_pipette_and_labware_use_nickname_ids():
    ctl, client = _loaded_control()
    ctype, params = client.commands[0]
    assert ctype == "loadPipette"
    assert params == {"pipetteName": "p300_single_gen2", "mount": "right", "pipetteId": "p300"}

    ctype, params = client.commands[1]
    assert ctype == "loadLabware"
    assert params["location"] == {"slotName": "2"}
    assert params["namespace"] == "opentrons"
    assert params["version"] == 1
    assert params["labwareId"] == "plate"


def test_custom_labware_registers_definition_first():
    client = FakeClient()
    ctl = OT2HttpControl(client)
    ctl.initialize_protocol()
    definition = {"namespace": "custom", "version": 2, "parameters": {"loadName": "weird_plate"}}
    ctl.load_labware({"ot_default": False, "nickname": "wp", "location": "5", "config": definition})

    assert client.labware_defs == [definition]
    ctype, params = _last(client)
    assert ctype == "loadLabware"
    assert (params["loadName"], params["namespace"], params["version"]) == (
        "weird_plate",
        "custom",
        2,
    )


# --- location precedence (must match OT2Control) ----------------------------


def test_location_precedence_bottom():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "A1", bottom=3)
    ctl.aspirate("p300", 50)
    ctype, params = _last(client)
    assert ctype == "aspirate"
    assert params["wellLocation"] == {"origin": "bottom", "offset": {"x": 0, "y": 0, "z": 3}}
    assert params["volume"] == 50
    assert params["flowRate"] == 100.0
    assert params["labwareId"] == "plate"
    assert params["wellName"] == "A1"


def test_location_precedence_top_wins_over_bottom():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "A1", top=1, bottom=3)
    ctl.dispense("p300", 25)
    _, params = _last(client)
    assert params["wellLocation"]["origin"] == "top"
    assert params["wellLocation"]["offset"]["z"] == 1
    assert params["flowRate"] == 200.0


def test_aspirate_flow_rate_override_beats_default():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "A1", bottom=2)
    ctl.aspirate("p300", 50, flow_rate=25.0)
    _, params = _last(client)
    assert params["flowRate"] == 25.0  # not the 100.0 default


def test_dispense_flow_rate_override_beats_default():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "A1", bottom=2)
    ctl.dispense("p300", 50, flow_rate=75.0)
    _, params = _last(client)
    assert params["flowRate"] == 75.0  # not the 200.0 default


@pytest.mark.parametrize("model, expected", [
    ("p20_single_gen2", 7.56),
    ("p300_single_gen2", 92.86),
    ("p1000_single_gen2", 274.7),
    ("p20_multi_gen2", 7.6),
    ("p300_multi_gen2", 94.0),
])
def test_http_default_dispense_matches_documented_ot2_rate(model, expected):
    client = FakeClient()
    ctl = OT2HttpControl(client)
    ctl.load_instrument({
        "ot_default": True, "nickname": "pip", "instrument_name": model, "mount": "right",
    })
    ctl.get_location_absolute(100, 100, 100)
    ctl.dispense("pip", 10)
    assert client.commands[-1][1]["flowRate"] == pytest.approx(expected)
    assert ctl.get_flow_rate("pip")["dispense"] == pytest.approx(expected)


def test_adopted_http_pipette_uses_its_model_default():
    class AdoptedClient(FakeClient):
        def get_run(self):
            return {"id": "run-1", "labware": [], "pipettes": [
                {"id": "existing", "mount": "right", "pipetteName": "p300_single_gen2"}
            ]}

        def get_current_state(self):
            return getattr(self, 'current_state', {})

    ctl = OT2HttpControl(AdoptedClient())
    assert ctl.get_flow_rate("existing")["dispense"] == pytest.approx(92.86)


def test_location_precedence_center():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "B2", center=1)
    ctl.aspirate("p300", 10)
    _, params = _last(client)
    assert params["wellLocation"]["origin"] == "center"


def test_location_default_is_top_zero():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "C3")
    ctl.aspirate("p300", 10)
    _, params = _last(client)
    assert params["wellLocation"] == {"origin": "top", "offset": {"x": 0, "y": 0, "z": 0}}


# --- pending-location lifecycle ---------------------------------------------


def test_aspirate_consumes_pending_location():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "A1", bottom=2)
    ctl.aspirate("p300", 10)
    # second aspirate with no new location must fail (pending consumed)
    with pytest.raises(RuntimeError, match="needs a location"):
        ctl.aspirate("p300", 10)


def test_pick_up_tip_requires_location():
    ctl, client = _loaded_control()
    with pytest.raises(RuntimeError, match="explicit tip location"):
        ctl.pick_up_tip("p300")


def test_pick_up_tip_with_location():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("tips", "A1")
    ctl.pick_up_tip("p300")
    ctype, params = _last(client)
    assert ctype == "pickUpTip"
    assert params["labwareId"] == "tips"
    assert params["wellName"] == "A1"


def test_drop_tip_without_location_uses_drop_in_place():
    ctl, client = _loaded_control()
    ctl.drop_tip("p300")
    ctype, params = _last(client)
    assert ctype == "dropTipInPlace"
    assert params == {"pipetteId": "p300"}


def test_drop_tip_with_location_uses_drop_tip():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("tips", "A1")
    ctl.drop_tip("p300")
    ctype, params = _last(client)
    assert ctype == "dropTip"
    assert params["labwareId"] == "tips"


# --- fixed trash auto-registration (setup_protocol) --------------------------


def test_setup_protocol_registers_the_fixed_trash_area():
    client = FakeClient()
    ctl = OT2HttpControl(client)
    ctl.initialize_protocol()
    recipe = {
        "labware": [
            {
                "ot_default": True,
                "nickname": "tips",
                "loadname": "opentrons_96_tiprack_300ul",
                "location": "1",
            }
        ],
        "instruments": [
            {
                "ot_default": True,
                "nickname": "p300",
                "instrument_name": "p300_single_gen2",
                "mount": "right",
            }
        ],
    }
    ctl.setup_protocol(**recipe)

    # No trash labware is loaded (slot 12 is not loadable on modern
    # robot-servers) — the fixedTrash addressable AREA is registered instead,
    # and a bare drop_tip routes to it (SSH-path behavior), not in place.
    assert all(
        p.get("loadName") != "opentrons_1_trash_1100ml_fixed" for _, p in client.commands
    )
    ctl.drop_tip("p300")
    move, drop = client.commands[-2], client.commands[-1]
    assert move[0] == "moveToAddressableAreaForDropTip"
    assert move[1]["addressableAreaName"] == "fixedTrash"
    assert drop[0] == "dropTipInPlace"


def test_trash_registration_adopts_a_preloaded_fixed_trash():
    # Newer robot-servers preload the OT-2 fixed trash into every run and
    # reserve slot 12 — loading there raises AreaNotInDeckConfigurationError
    # (observed live on ot2_complexation 2026-08-11). The existing labware is
    # adopted instead of loaded.
    client = FakeClient()
    client.get_run = lambda: {
        "id": "run-1",
        "labware": [
            {
                "id": "fixedTrash",
                "loadName": "opentrons_1_trash_1100ml_fixed",
                "location": {"slotName": "12"},
            }
        ],
    }
    ctl = OT2HttpControl(client)
    ctl.initialize_protocol()
    ctl.load_instrument(
        {
            "ot_default": True,
            "nickname": "p300",
            "instrument_name": "p300_single_gen2",
            "mount": "right",
        }
    )
    ctl.load_trash_bin()

    assert all(
        p.get("loadName") != "opentrons_1_trash_1100ml_fixed" for _, p in client.commands
    )
    ctl.drop_tip("p300")
    ctype, params = _last(client)
    assert ctype == "dropTip"
    assert params["labwareId"] == "fixedTrash"


def test_setup_protocol_skips_the_trash_when_slot_12_is_occupied():
    client = FakeClient()
    ctl = OT2HttpControl(client)
    ctl.initialize_protocol()
    ctl.setup_protocol(
        labware=[
            {
                "ot_default": True,
                "nickname": "big_res",
                "loadname": "nest_1_reservoir_195ml",
                "location": "12",
            }
        ],
        instruments=[
            {
                "ot_default": True,
                "nickname": "p300",
                "instrument_name": "p300_single_gen2",
                "mount": "right",
            }
        ],
    )

    # A recipe that deliberately occupies slot 12 opts out of every trash
    # route: the fallback is unchanged (drop where the pipette is), with no
    # positioning move first.
    ctl.drop_tip("p300")
    assert _last(client)[0] == "dropTipInPlace"
    assert all(c != "moveToAddressableAreaForDropTip" for c, _ in client.commands)


def test_setup_protocol_routes_a_recipe_fixed_trash_through_the_trash_registrar():
    # BUG 3 regression: a recipe that explicitly names the OT-2 fixed trash in
    # slot 12 must NOT be sent to load_labware — on a modern robot-server slot
    # 12 is an addressable area, so loadLabware there dies with
    # AreaNotInDeckConfigurationError and half-loads the run. It must go through
    # load_trash_bin (addressable-area route) instead.
    client = FakeClient()
    ctl = OT2HttpControl(client)
    ctl.initialize_protocol()
    ctl.setup_protocol(
        labware=[
            {
                "ot_default": True,
                "nickname": "tips",
                "loadname": "opentrons_96_tiprack_300ul",
                "location": "1",
            },
            {
                "ot_default": True,
                "nickname": "trash",
                "loadname": "opentrons_1_trash_3200ml_fixed",
                "location": "12",
            },
        ],
        instruments=[
            {
                "ot_default": True,
                "nickname": "p300",
                "instrument_name": "p300_single_gen2",
                "mount": "right",
            }
        ],
    )

    # The trash was never load_labware'd (no loadLabware for slot 12 / the trash
    # loadName), so setup did not raise; the tiprack still loaded.
    load_labware = [p for c, p in client.commands if c == "loadLabware"]
    assert all(p.get("loadName") != "opentrons_1_trash_3200ml_fixed" for p in load_labware)
    assert any(p.get("loadName") == "opentrons_96_tiprack_300ul" for p in load_labware)
    # and a bare drop routes to the fixedTrash area the registrar set up.
    ctl.drop_tip("p300")
    move, drop = client.commands[-2], client.commands[-1]
    assert move[0] == "moveToAddressableAreaForDropTip"
    assert move[1]["addressableAreaName"] == "fixedTrash"
    assert drop[0] == "dropTipInPlace"


# --- run-state adoption (self-heal after a partial setup) -------------------


def test_labware_id_resolves_after_adopting_a_run_loaded_it():
    # BUG 2 regression: a name absent from the local id cache but present in the
    # run (a partial setup, or a control object built against an existing run)
    # must resolve after a lazy refresh from the run — not raise "not loaded".
    client = FakeClient()
    client.get_run = lambda: {
        "id": "run-1",
        "labware": [
            {"id": "tiprack300", "location": {"slotName": "9"}},
            {"id": "plate_slot1", "location": {"slotName": "1"}},
        ],
        "pipettes": [{"id": "left_p300", "mount": "left"}],
        "modules": [],
    }
    ctl = OT2HttpControl(client)
    ctl.initialize_protocol()  # fresh empty local cache — nothing loaded here

    # The run holds these ids though load_* never ran locally; the lazy refresh
    # inside _labware_id / _pipette_id recovers them.
    assert ctl._labware_id("tiprack300") == "tiprack300"
    assert ctl._pipette_id("left_p300") == "left_p300"
    assert ctl._pipette_mount("left_p300") == "left"


def test_labware_id_error_lists_the_loaded_ids():
    # A genuinely-absent name reports what IS loaded, so the miss is diagnosable
    # in one round trip rather than guessed at.
    client = FakeClient()
    client.get_run = lambda: {
        "id": "run-1",
        "labware": [
            {"id": "tiprack300", "location": {"slotName": "9"}},
            {"id": "plate_slot1", "location": {"slotName": "1"}},
        ],
    }
    ctl = OT2HttpControl(client)
    ctl.initialize_protocol()

    with pytest.raises(RuntimeError) as exc:
        ctl._labware_id("tiprack9")
    msg = str(exc.value)
    assert "tiprack9" in msg
    assert "plate_slot1" in msg and "tiprack300" in msg


# --- move labware / handoff -------------------------------------------------


def test_move_labware_offdeck_uses_manual_without_pause():
    ctl, client = _loaded_control()
    ctl.move_labware("plate", "OFF_DECK")
    ctype, params = _last(client)
    assert ctype == "moveLabware"
    assert params["labwareId"] == "plate"
    assert params["newLocation"] == "offDeck"
    assert params["strategy"] == "manualMoveWithoutPause"


def test_move_labware_to_slot():
    ctl, client = _loaded_control()
    ctl.move_labware("plate", "6")
    _, params = _last(client)
    assert params["newLocation"] == {"slotName": "6"}


def test_move_unknown_labware_raises():
    ctl, client = _loaded_control()
    with pytest.raises(RuntimeError, match="not loaded"):
        ctl.move_labware("ghost", "3")


# --- robot / lifecycle ------------------------------------------------------


def test_home_emits_home_command():
    ctl, client = _loaded_control()
    ctl.home()
    assert _last(client)[0] == "home"


def test_pause_resume_are_noops():
    ctl, client = _loaded_control()
    before = len(client.commands)
    ctl.pause()
    ctl.resume()
    assert len(client.commands) == before  # no commands emitted


def test_shutdown_stops_and_closes():
    ctl, client = _loaded_control()
    ctl.shutdown()
    assert client.stopped is True
    assert client.closed is True


def test_unknown_pipette_raises():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "A1")
    with pytest.raises(RuntimeError, match="pipette 'p999' is not loaded"):
        ctl.aspirate("p999", 10)
