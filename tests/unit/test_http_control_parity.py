"""Unit tests for the SSH-parity surface of OT2HttpControl.

Covers the methods added to bring the HTTP transport to full OT2Control
method parity (see docs/HTTP_SSH_PARITY.md): protocol-level controls,
absolute-coordinate liquid handling, emulated mix/air_gap/return_tip,
client-tracked readbacks, module verbs, and the explicit NotImplementedError
surface. Command translation is verified offline via a fake RunEngineClient.
"""

import pytest

from opentrons_server.control.http_control import OT2HttpControl
from opentrons_server.control.http_run import RunEngineError


CORNING_URI = "opentrons/corning_96_wellplate_360ul_flat/1"
TIPRACK_URI = "opentrons/opentrons_96_tiprack_300ul/1"

CORNING_DEF = {
    "namespace": "opentrons",
    "version": 1,
    "parameters": {"loadName": "corning_96_wellplate_360ul_flat", "isTiprack": False},
    "wells": {
        "A1": {"depth": 10.67, "diameter": 6.86},
        "B1": {"depth": 10.67, "xDimension": 6.86, "yDimension": 6.86},
    },
}

TIPRACK_DEF = {
    "namespace": "opentrons",
    "version": 1,
    "parameters": {
        "loadName": "opentrons_96_tiprack_300ul",
        "isTiprack": True,
        "tipLength": 59.3,
    },
    "wells": {"A1": {"depth": 59.3}},
}


class FakeClient:
    def __init__(self):
        self.commands = []  # list of (commandType, params)
        self.labware_defs = []
        self.created = False
        self.stopped = False
        self.closed = False
        self.run_id = None
        self.lights_on = False
        self.lights_calls = []
        self.modules = []  # GET /modules payload

    def create_run(self):
        self.created = True
        self.run_id = "run-1"
        return "run-1"

    def execute(self, command, **kwargs):
        self.commands.append(command)
        ctype, params = command
        if ctype == "loadModule":
            return {
                "status": "succeeded",
                "result": {"serialNumber": f"serial-{params.get('moduleId')}"},
            }
        return {"status": "succeeded"}

    def add_labware_definition(self, definition):
        self.labware_defs.append(definition)
        return "custom/plate/1"

    def get_run(self):
        return {
            "id": "run-1",
            "labware": [
                {"id": "plate", "definitionUri": CORNING_URI},
                {"id": "tips", "definitionUri": TIPRACK_URI},
            ],
        }

    def get_loaded_labware_definitions(self):
        return [CORNING_DEF, TIPRACK_DEF]

    def get_modules(self):
        return self.modules

    def get_lights(self):
        return self.lights_on

    def set_lights(self, on):
        self.lights_calls.append(on)
        self.lights_on = on

    def stop_run(self):
        self.stopped = True

    def close(self):
        self.closed = True


def _loaded_control():
    client = FakeClient()
    ctl = OT2HttpControl(
        client,
        aspirate_flow_rate=100.0,
        dispense_flow_rate=200.0,
        blow_out_flow_rate=50.0,
    )
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


def _types(client):
    return [c[0] for c in client.commands]


# --- protocol-level controls --------------------------------------------------


def test_comment_and_delay():
    ctl, client = _loaded_control()
    ctl.comment("hello")
    assert _last(client) == ("comment", {"message": "hello"})
    ctl.delay(seconds=2, minutes=1)
    assert _last(client) == ("waitForDuration", {"seconds": 62.0})


def test_rail_lights_roundtrip():
    ctl, client = _loaded_control()
    ctl.set_rail_lights(True)
    assert client.lights_calls == [True]
    assert ctl.get_rail_lights() is True
    ctl.set_rail_lights(False)
    assert ctl.get_rail_lights() is False


def test_max_speed_unsupported():
    ctl, _ = _loaded_control()
    with pytest.raises(NotImplementedError):
        ctl.set_max_speed("X", 100)
    with pytest.raises(NotImplementedError):
        ctl.clear_max_speed("X")


def test_invoke_unsupported():
    ctl, _ = _loaded_control()
    with pytest.raises(NotImplementedError):
        ctl.invoke("protocol.home()")


def test_close_session_homes_then_shuts_down():
    ctl, client = _loaded_control()
    ctl.close_session()
    assert _types(client)[-1] == "home"
    assert client.stopped is True and client.closed is True


# --- motion --------------------------------------------------------------------


def test_move_to_pip_peeks_pending_well():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "A1", bottom=2)
    ctl.move_to_pip("p300", speed=40, force_direct=True, minimum_z_height=10)
    ctype, params = _last(client)
    assert ctype == "moveToWell"
    assert params["wellLocation"] == {"origin": "bottom", "offset": {"x": 0, "y": 0, "z": 2}}
    assert params["speed"] == 40.0
    assert params["forceDirect"] is True
    assert params["minimumZHeight"] == 10.0
    # pending survives the move: the next aspirate targets the same well
    ctl.aspirate("p300", 50)
    ctype, params = _last(client)
    assert ctype == "aspirate"
    assert params["wellName"] == "A1"


def test_move_to_pip_uses_set_speed_default():
    ctl, client = _loaded_control()
    ctl.set_speed("p300", 75)
    ctl.get_location_from_labware("plate", "A1")
    ctl.move_to_pip("p300")
    assert _last(client)[1]["speed"] == 75.0


def test_move_to_pip_requires_location():
    ctl, _ = _loaded_control()
    with pytest.raises(RuntimeError):
        ctl.move_to_pip("p300")


def test_absolute_location_aspirate_moves_then_aspirates_in_place():
    ctl, client = _loaded_control()
    ctl.get_location_absolute(10, 20, 30)
    ctl.aspirate("p300", 25)
    assert _types(client)[-2:] == ["moveToCoordinates", "aspirateInPlace"]
    move_params = client.commands[-2][1]
    assert move_params["coordinates"] == {"x": 10.0, "y": 20.0, "z": 30.0}
    in_place = client.commands[-1][1]
    assert in_place == {"pipetteId": "p300", "volume": 25.0, "flowRate": 100.0}


def test_absolute_location_dispense_and_blow_out():
    ctl, client = _loaded_control()
    ctl.get_location_absolute(1, 2, 3)
    ctl.dispense("p300", 25, push_out=2)
    assert _types(client)[-2:] == ["moveToCoordinates", "dispenseInPlace"]
    assert client.commands[-1][1]["pushOut"] == 2.0
    ctl.get_location_absolute(1, 2, 3)
    ctl.blow_out("p300")
    assert _types(client)[-2:] == ["moveToCoordinates", "blowOutInPlace"]
    assert client.commands[-1][1]["flowRate"] == 50.0


def test_blow_out_in_place():
    ctl, client = _loaded_control()
    ctl.blow_out_in_place("p300")
    assert _last(client) == ("blowOutInPlace", {"pipetteId": "p300", "flowRate": 50.0})


def test_prepare_aspirate():
    ctl, client = _loaded_control()
    ctl.prepare_aspirate("p300")
    assert _last(client) == ("prepareToAspirate", {"pipetteId": "p300"})


# --- flow rates ------------------------------------------------------------------


def test_rate_multiplier_applies_to_default():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "A1")
    ctl.aspirate("p300", 50, rate=0.5)
    assert _last(client)[1]["flowRate"] == 50.0  # 100 * 0.5


def test_set_flow_rate_overrides_default_and_explicit_beats_both():
    ctl, client = _loaded_control()
    ctl.set_flow_rate("p300", aspirate=80, dispense=150, blow_out=25)
    assert ctl.get_flow_rate("p300") == {
        "aspirate": 80.0,
        "dispense": 150.0,
        "blow_out": 25.0,
    }
    ctl.get_location_from_labware("plate", "A1")
    ctl.aspirate("p300", 50)
    assert _last(client)[1]["flowRate"] == 80.0
    ctl.get_location_from_labware("plate", "A1")
    ctl.aspirate("p300", 50, flow_rate=120)
    assert _last(client)[1]["flowRate"] == 120.0


def test_get_flow_rate_defaults():
    ctl, _ = _loaded_control()
    assert ctl.get_flow_rate("p300") == {
        "aspirate": 100.0,
        "dispense": 200.0,
        "blow_out": 50.0,
    }


def test_well_bottom_clearance_stored_readback():
    ctl, _ = _loaded_control()
    assert ctl.get_well_bottom_clearance("p300") == {"aspirate": 1.0, "dispense": 1.0}
    ctl.set_well_bottom_clearance("p300", aspirate=3, dispense=5)
    assert ctl.get_well_bottom_clearance("p300") == {"aspirate": 3.0, "dispense": 5.0}


# --- emulated liquid handling ------------------------------------------------------


def test_mix_emits_aspirate_dispense_pairs():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "A1")
    before = len(client.commands)
    ctl.mix("p300", repetitions=3, volume=40)
    emitted = _types(client)[before:]
    assert emitted == ["aspirate", "dispense"] * 3
    assert all(c[1]["wellName"] == "A1" for c in client.commands[before:])


def test_mix_requires_volume():
    ctl, _ = _loaded_control()
    ctl.get_location_from_labware("plate", "A1")
    with pytest.raises(ValueError):
        ctl.mix("p300", repetitions=2)


def test_air_gap_moves_above_last_well_then_aspirates_in_place():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("plate", "A1")
    ctl.aspirate("p300", 50)
    ctl.air_gap("p300", 20)
    assert _types(client)[-2:] == ["moveToWell", "aspirateInPlace"]
    move_params = client.commands[-2][1]
    assert move_params["wellName"] == "A1"
    assert move_params["wellLocation"] == {
        "origin": "top",
        "offset": {"x": 0, "y": 0, "z": 5.0},
    }
    assert client.commands[-1][1]["volume"] == 20.0


def test_air_gap_custom_height_and_no_prior_well_raises():
    ctl, client = _loaded_control()
    with pytest.raises(RuntimeError):
        ctl.air_gap("p300", 20)
    ctl.get_location_from_labware("plate", "B1", bottom=1)
    ctl.dispense("p300", 10)
    ctl.air_gap("p300", 10, height=12)
    assert client.commands[-2][1]["wellLocation"]["offset"]["z"] == 12.0


def test_touch_tip_params():
    ctl, client = _loaded_control()
    ctl.touch_tip("p300", "plate", "A1", radius=0.8, v_offset=-2.0, speed=40)
    ctype, params = _last(client)
    assert ctype == "touchTip"
    assert params["radius"] == 0.8
    assert params["speed"] == 40.0
    assert params["wellLocation"] == {
        "origin": "top",
        "offset": {"x": 0, "y": 0, "z": -2.0},
    }


# --- tips ---------------------------------------------------------------------------


def test_pick_up_tip_tracks_origin_and_has_tip():
    ctl, client = _loaded_control()
    assert ctl.has_tip("p300") is False
    ctl.get_location_from_labware("tips", "A1")
    ctl.pick_up_tip("p300")
    assert ctl.has_tip("p300") is True
    ctl.get_location_from_labware("plate", "A1")
    ctl.drop_tip("p300")
    assert ctl.has_tip("p300") is False


def test_pick_up_tip_rejects_repl_only_kwargs():
    ctl, _ = _loaded_control()
    ctl.get_location_from_labware("tips", "A1")
    with pytest.raises(NotImplementedError):
        ctl.pick_up_tip("p300", presses=2)


def test_return_tip_drops_at_origin():
    ctl, client = _loaded_control()
    ctl.get_location_from_labware("tips", "B1")
    ctl.pick_up_tip("p300")
    ctl.return_tip("p300")
    ctype, params = _last(client)
    assert ctype == "dropTip"
    assert params["labwareId"] == "tips"
    assert params["wellName"] == "B1"
    assert ctl.has_tip("p300") is False


def test_return_tip_without_pick_raises():
    ctl, _ = _loaded_control()
    with pytest.raises(RuntimeError):
        ctl.return_tip("p300")


def test_drop_tip_defaults_to_registered_trash():
    # A run without a preloaded trash labware registers the fixedTrash
    # addressable AREA (slot 12 is not loadable on modern robot-servers):
    # a bare drop is then move-over-the-area + drop-in-place, exactly the
    # protocol API's own ≥2.16 sequence.
    ctl, client = _loaded_control()
    ctl.load_trash_bin()
    assert all(
        p.get("loadName") != "opentrons_1_trash_1100ml_fixed" for _, p in client.commands
    )
    ctl.drop_tip("p300")
    move, drop = client.commands[-2], client.commands[-1]
    assert move[0] == "moveToAddressableAreaForDropTip"
    assert move[1]["addressableAreaName"] == "fixedTrash"
    assert move[1]["alternateDropLocation"] is True
    assert drop[0] == "dropTipInPlace"


def test_drop_tip_without_trash_drops_in_place_with_home_after():
    ctl, client = _loaded_control()
    ctl.drop_tip("p300", home_after=False)
    assert _last(client) == (
        "dropTipInPlace",
        {"pipetteId": "p300", "homeAfter": False},
    )


def test_tip_tracking_methods_unsupported():
    ctl, _ = _loaded_control()
    with pytest.raises(NotImplementedError):
        ctl.set_starting_tip("p300", "tips", "A1")
    with pytest.raises(NotImplementedError):
        ctl.reset_tipracks("p300")


# --- client-tracked volume ------------------------------------------------------------


def test_current_volume_ledger():
    ctl, _ = _loaded_control()
    assert ctl.current_volume("p300") == 0.0
    ctl.get_location_from_labware("plate", "A1")
    ctl.aspirate("p300", 100)
    assert ctl.current_volume("p300") == 100.0
    ctl.get_location_from_labware("plate", "B1")
    ctl.dispense("p300", 60)
    assert ctl.current_volume("p300") == 40.0
    ctl.get_location_from_labware("plate", "B1")
    ctl.blow_out("p300")
    assert ctl.current_volume("p300") == 0.0


# --- pipette homing ---------------------------------------------------------------------


def test_home_pipette_and_plunger_axes():
    ctl, client = _loaded_control()
    ctl.home_pipette("p300")
    assert _last(client) == ("home", {"axes": ["rightZ", "rightPlunger"]})
    ctl.home_plunger("p300")
    assert _last(client) == ("home", {"axes": ["rightPlunger"]})


# --- labware management ----------------------------------------------------------------


def test_remove_labware_moves_off_deck():
    ctl, client = _loaded_control()
    ctl.remove_labware("plate")
    ctype, params = _last(client)
    assert ctype == "moveLabware"
    assert params["newLocation"] == "offDeck"
    assert params["strategy"] == "manualMoveWithoutPause"


def test_move_labware_w_gripper_aliases_manual_move():
    ctl, client = _loaded_control()
    ctl.move_labware_w_gripper("plate", "5")
    ctype, params = _last(client)
    assert ctype == "moveLabware"
    assert params["newLocation"] == {"slotName": "5"}
    assert params["strategy"] == "manualMoveWithoutPause"


# --- geometry readbacks -------------------------------------------------------------------


def test_well_diameter_and_depth_from_definition():
    ctl, _ = _loaded_control()
    assert ctl.well_diameter("plate", "A1") == pytest.approx(6.86)
    assert ctl.well_depth("plate", "A1") == pytest.approx(10.67)


def test_well_diameter_rectangular_raises():
    ctl, _ = _loaded_control()
    with pytest.raises(RunEngineError):
        ctl.well_diameter("plate", "B1")


def test_tip_length_tiprack_and_plate():
    ctl, _ = _loaded_control()
    assert ctl.tip_length("tips", "A1") == pytest.approx(59.3)
    assert ctl.tip_length("plate", "A1") is None


def test_unknown_well_raises():
    ctl, _ = _loaded_control()
    with pytest.raises(RunEngineError):
        ctl.well_depth("plate", "H12")


# --- modules -------------------------------------------------------------------------------


def _control_with_module(module_name="heaterShakerModuleV1", adapter=None):
    ctl, client = _loaded_control()
    config = {
        "nickname": "hs",
        "module_name": module_name,
        "location": "3",
        "adapter": adapter,
    }
    ctl.load_module(config)
    return ctl, client


def test_load_module_captures_serial_and_loads_adapter():
    ctl, client = _control_with_module(adapter="opentrons_96_flat_bottom_adapter")
    types = _types(client)
    assert types[-2:] == ["loadModule", "loadLabware"]
    adapter_params = client.commands[-1][1]
    assert adapter_params["location"] == {"moduleId": "hs"}
    assert adapter_params["labwareId"] == "hs_adapter"
    # adapter is addressable as labware afterwards
    ctl.get_location_from_labware("hs_adapter", "A1")


def test_hs_verbs_and_bands():
    ctl, client = _control_with_module()
    ctl.hs_latch_open("hs")
    assert _last(client) == ("heaterShaker/openLabwareLatch", {"moduleId": "hs"})
    ctl.hs_latch_close("hs")
    assert _last(client)[0] == "heaterShaker/closeLabwareLatch"
    ctl.set_rpm("hs", 500)
    assert _last(client) == (
        "heaterShaker/setAndWaitForShakeSpeed",
        {"moduleId": "hs", "rpm": 500.0},
    )
    ctl.set_rpm("hs", 100)  # below band -> deactivate
    assert _last(client)[0] == "heaterShaker/deactivateShaker"
    ctl.set_temp("hs", 50)
    assert _types(client)[-2:] == [
        "heaterShaker/setTargetTemperature",
        "heaterShaker/waitForTemperature",
    ]
    ctl.set_temp("hs", 20)  # below band -> deactivate heater
    assert _last(client)[0] == "heaterShaker/deactivateHeater"
    ctl.hs_deactivate("hs")
    assert _types(client)[-2:] == [
        "heaterShaker/deactivateShaker",
        "heaterShaker/deactivateHeater",
    ]


def test_module_live_readbacks_via_serial():
    ctl, client = _control_with_module()
    client.modules = [
        {
            "serialNumber": "serial-hs",
            "data": {"currentSpeed": 480, "currentTemperature": 41.5},
        }
    ]
    assert ctl.get_rpm("hs") == 480.0
    assert ctl.get_temp("hs") == 41.5


def test_module_readback_without_attached_module_raises():
    ctl, client = _control_with_module()
    client.modules = []
    with pytest.raises(RunEngineError):
        ctl.get_rpm("hs")


def test_tempmod_set_temperature_sets_and_waits():
    ctl, client = _loaded_control()
    ctl.load_module({"nickname": "tm", "module_name": "temperatureModuleV2", "location": "4"})
    ctl.tempmod_set_temperature("tm", 4)
    assert _types(client)[-2:] == [
        "temperatureModule/setTargetTemperature",
        "temperatureModule/waitForTemperature",
    ]
    ctl.tempmod_deactivate("tm")
    assert _last(client)[0] == "temperatureModule/deactivate"


def test_tempmod_start_set_does_not_wait():
    """The gateway operator path must not block on the ramp."""
    ctl, client = _loaded_control()
    ctl.load_module({"nickname": "tm", "module_name": "temperatureModuleV2", "location": "4"})
    ctl.tempmod_start_set_temperature("tm", 4)
    assert _last(client) == (
        "temperatureModule/setTargetTemperature",
        {"moduleId": "tm", "celsius": 4.0},
    )
    assert "temperatureModule/waitForTemperature" not in _types(client)


def test_magmod_engage_validation():
    ctl, client = _loaded_control()
    ctl.load_module({"nickname": "mag", "module_name": "magneticModuleV2", "location": "6"})
    ctl.magmod_engage("mag", height_from_base=8)
    assert _last(client) == ("magneticModule/engage", {"moduleId": "mag", "height": 8.0})
    with pytest.raises(NotImplementedError):
        ctl.magmod_engage("mag", offset=2)
    with pytest.raises(ValueError):
        ctl.magmod_engage("mag")
    ctl.magmod_disengage("mag")
    assert _last(client)[0] == "magneticModule/disengage"


def test_thermocycler_block_temperature_with_hold():
    ctl, client = _loaded_control()
    ctl.load_module({"nickname": "tc", "module_name": "thermocyclerModuleV1", "location": "7"})
    ctl.thermocycler_set_block_temperature(
        "tc", 95, hold_time_seconds=30, hold_time_minutes=1, block_max_volume=25
    )
    set_cmd = client.commands[-2]
    assert set_cmd[0] == "thermocycler/setTargetBlockTemperature"
    assert set_cmd[1]["holdTimeSeconds"] == 90.0
    assert set_cmd[1]["blockMaxVolumeUl"] == 25.0
    assert _last(client)[0] == "thermocycler/waitForBlockTemperature"
    with pytest.raises(NotImplementedError):
        ctl.thermocycler_set_block_temperature("tc", 95, ramp_rate=2)
    ctl.thermocycler_set_lid_temperature("tc", 105)
    assert _types(client)[-2:] == [
        "thermocycler/setTargetLidTemperature",
        "thermocycler/waitForLidTemperature",
    ]
    ctl.thermocycler_deactivate("tc")
    assert _types(client)[-2:] == [
        "thermocycler/deactivateBlock",
        "thermocycler/deactivateLid",
    ]
    with pytest.raises(NotImplementedError):
        ctl.thermocycler_open_labware_latch("tc")


def test_unknown_module_raises():
    ctl, _ = _loaded_control()
    with pytest.raises(RuntimeError):
        ctl.hs_latch_open("nope")


def test_default_origin_matches_the_ssh_transport():
    """The caller-chosen default must land identically on both transports:
    SSH emits ``.bottom(1)``, the run engine an origin + z offset."""
    ctl, client = _loaded_control()

    ctl.get_location_from_labware(
        "plate", "A1", default_origin="bottom", default_offset=1
    )
    ctl.aspirate("p300", 50)

    ctype, params = _last(client)
    assert ctype == "aspirate"
    assert params["wellLocation"] == {"origin": "bottom", "offset": {"x": 0, "y": 0, "z": 1.0}}


def test_unqualified_location_still_defaults_to_the_well_top():
    ctl, client = _loaded_control()

    ctl.get_location_from_labware("plate", "A1")
    ctl.dispense("p300", 50)

    assert _last(client)[1]["wellLocation"] == {
        "origin": "top",
        "offset": {"x": 0, "y": 0, "z": 0.0},
    }


@pytest.mark.parametrize("direct", [True, False])
def test_absolute_pipette_motion_preserves_direct_flag_without_extra_retract(direct):
    ctl, client = _loaded_control()
    client.commands.clear()
    ctl.get_location_absolute(100, 200, 30)
    ctl.move_to_pip("p300", force_direct=direct)
    assert len(client.commands) == 1
    name, params = client.commands[0]
    assert name == "moveToCoordinates"
    assert params["forceDirect"] is direct
    assert params["coordinates"]["z"] == 30


def test_explicit_air_gap_moves_above_requested_well_once():
    ctl, client = _loaded_control()
    client.commands.clear()
    ctl.get_location_from_labware("plate", "B2")
    ctl.air_gap("p300", 10, height=4)
    assert [name for name, _ in client.commands] == ["moveToWell", "aspirateInPlace"]
    move = client.commands[0][1]
    assert move["wellName"] == "B2"
    assert move["wellLocation"] == {"origin":"top", "offset":{"x":0, "y":0, "z":4}}
    assert ctl._pending is None
