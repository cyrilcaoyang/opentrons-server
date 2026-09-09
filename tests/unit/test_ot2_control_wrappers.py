"""OT2Control remote-call formatting: typed readbacks, kwargs, new wrappers.

Uses a connection-less subclass that records every invoked code string, so the
tests assert on the exact remote Python the SSH REPL would receive.
"""

import pytest

from opentrons_server.control.ot2_control import OT2Control


class RecordingControl(OT2Control):
    """OT2Control without a connection; records invokes, replays canned output."""

    def __init__(self, responses=None):  # no super().__init__: no SSH, no protocol
        self.invoked = []
        self._responses = list(responses or [])

    def invoke(self, code: str) -> str:
        self.invoked.append(code)
        if self._responses:
            return self._responses.pop(0)
        return f">>> {code}\r\n>>> "


def _transcript(value: str) -> str:
    # Echoed command, printed value, trailing prompt — the shape the SSH
    # transport hands back for a single-expression invoke.
    return f"cmd\r\n{value}\r\n>>> "


def test_setup_strings_stay_literals_in_generated_python():
    import ast
    control = RecordingControl()
    supplied = "plate'); forbidden() #"
    control.load_labware({"nickname": "plate", "ot_default": True, "loadname": supplied, "location": "1"})
    statement, = ast.parse(control.invoked[0]).body
    assert isinstance(statement, ast.Assign)
    assert ast.literal_eval(statement.value.keywords[0].value) == supplied
    control._load_custom_labware("custom", {"parameters": {"loadName": supplied}}, "2")
    statement, = ast.parse(control.invoked[-1]).body
    assert isinstance(statement, ast.Assign)
    assert ast.literal_eval(statement.value.keywords[0].value)["parameters"]["loadName"] == supplied


def test_setup_identifiers_cannot_inject_remote_python():
    control = RecordingControl()
    with pytest.raises(ValueError, match="identifier"):
        control.load_module({"nickname": "m; forbidden() #", "module_name": "temperature module", "location": "1"})
    assert control.invoked == []


# ---- typed readback ------------------------------------------------------


def test_invoke_float_and_bool_parse_scalar_line():
    control = RecordingControl(responses=[_transcript("6.85"), _transcript("True")])
    assert control.well_diameter("plate", "A1") == pytest.approx(6.85)
    assert control.has_tip("p300") is True
    assert control.invoked == ["plate['A1'].diameter", "p300.has_tip"]


def test_invoke_bool_rejects_garbage():
    control = RecordingControl(responses=[_transcript("maybe")])
    with pytest.raises(ValueError):
        control.get_rail_lights()


def test_tip_length_returns_none_when_undefined():
    # Only two lines (no printed value) -> no tip length defined.
    control = RecordingControl(responses=["cmd\r\n>>> "])
    assert control.tip_length("tips", "A1") is None
    control = RecordingControl(responses=[_transcript("59.3")])
    assert control.tip_length("tips", "A1") == pytest.approx(59.3)


def test_get_flow_rate_reads_three_scalars():
    control = RecordingControl(
        responses=[_transcript("90.0"), _transcript("300.0"), _transcript("100.0")]
    )
    assert control.get_flow_rate("p300") == {
        "aspirate": 90.0,
        "dispense": 300.0,
        "blow_out": 100.0,
    }


# ---- kwargs formatting -----------------------------------------------------


def test_format_kwargs_skips_none_and_emits_location_raw():
    control = RecordingControl()
    control.aspirate("p300", 50)
    assert control.invoked == ["p300.aspirate(volume = 50, location = location)"]

    control.invoked.clear()
    control.aspirate("p300", 50, rate=0.5, flow_rate=90)
    assert control.invoked == [
        "p300.flow_rate.aspirate = 90",
        "p300.aspirate(volume = 50, location = location, rate = 0.5)",
    ]


def test_dispense_omits_none_push_out_and_reprs_values():
    control = RecordingControl()
    control.dispense("p300", 50)
    assert control.invoked == ["p300.dispense(volume = 50, location = location)"]

    control.invoked.clear()
    control.dispense("p300", 50, push_out=2.0)
    assert control.invoked == [
        "p300.dispense(volume = 50, location = location, push_out = 2.0)"
    ]


def test_pick_up_and_drop_tip_optional_kwargs():
    control = RecordingControl()
    control.pick_up_tip("p300")
    control.pick_up_tip("p300", presses=2, prep_after=True)
    control.drop_tip("p300")
    control.drop_tip("p300", home_after=False)
    assert control.invoked == [
        "p300.pick_up_tip(location = location)",
        "p300.pick_up_tip(location = location, presses = 2, prep_after = True)",
        "p300.drop_tip()",
        "p300.drop_tip(home_after = False)",
    ]


def test_move_to_pip_advanced_kwargs():
    control = RecordingControl()
    control.move_to_pip("p300")
    control.move_to_pip("p300", speed=40, force_direct=True, minimum_z_height=20)
    assert control.invoked == [
        "p300.move_to(location = location)",
        "p300.move_to(location = location, speed = 40, force_direct = True, minimum_z_height = 20)",
    ]


def test_comment_reprs_message():
    control = RecordingControl()
    control.comment("hello 'world'")
    assert control.invoked == ['protocol.comment("hello \'world\'")']


# ---- new wrappers -----------------------------------------------------------


def test_mix_air_gap_touch_tip():
    control = RecordingControl()
    control.mix("p300", 3, volume=100)
    control.air_gap("p300", 10)
    control.touch_tip("p300", "plate", "A1")
    assert control.invoked == [
        "p300.mix(repetitions = 3, volume = 100, location = location)",
        "p300.air_gap(volume = 10)",
        "p300.touch_tip(plate['A1'], radius = 1.0, v_offset = -1.0, speed = 60.0)",
    ]


def test_module_wrappers():
    control = RecordingControl()
    control.hs_set_and_wait_temperature("hs", 37.5)
    control.tempmod_set_temperature("tm", 4)
    control.tempmod_start_set_temperature("tm", 4)
    control.magmod_engage("mm", height_from_base=5.0)
    control.magmod_engage("mm")
    control.thermocycler_set_block_temperature("tc", 95, hold_time_seconds=30)
    assert control.invoked == [
        "hs.set_and_wait_for_temperature(celsius = 37.5)",
        "tm.set_temperature(celsius = 4)",
        "tm.start_set_temperature(celsius = 4) if hasattr(tm, 'start_set_temperature') else tm._core.set_target_temperature(4)",
        "mm.engage(height_from_base = 5.0)",
        "mm.engage()",
        "tc.set_block_temperature(temperature = 95, hold_time_seconds = 30)",
    ]


def test_set_temp_and_rpm_band_checks_accept_floats():
    control = RecordingControl()
    control.set_temp("hs", 37.5)  # float in band: must heat, not deactivate
    control.set_temp("hs", 0)
    control.set_rpm("hs", 500)
    control.set_rpm("hs", 0)
    assert control.invoked == [
        "hs.set_and_wait_for_temperature(celsius = 37.5)",
        "hs.deactivate_heater()",
        "hs.set_and_wait_for_shake_speed(rpm = 500)",
        "hs.deactivate_shaker()",
    ]


def test_rail_lights_and_max_speed():
    control = RecordingControl(responses=[])
    control.set_rail_lights(True)
    control.set_max_speed("Z", 100)
    control.clear_max_speed("Z")
    assert control.invoked == [
        "protocol.set_rail_lights(True)",
        "protocol.max_speeds['Z'] = 100",
        "protocol.max_speeds['Z'] = None",
    ]


# ---- well-location defaults ---------------------------------------------


def test_location_defaults_to_the_well_top():
    ctl = RecordingControl()

    ctl.get_location_from_labware("plate", "A1")

    assert ctl.invoked[-1] == "location = plate['A1'].top(0)"


def test_location_honors_a_caller_chosen_default_origin():
    # What an aspirate asks for: the well bottom, 1 mm off the glass.
    ctl = RecordingControl()

    ctl.get_location_from_labware(
        "plate", "A1", default_origin="bottom", default_offset=1
    )

    assert ctl.invoked[-1] == "location = plate['A1'].bottom(1)"


def test_explicit_offset_overrides_the_default_origin():
    ctl = RecordingControl()

    ctl.get_location_from_labware(
        "plate", "A1", top=-2, default_origin="bottom", default_offset=1
    )

    assert ctl.invoked[-1] == "location = plate['A1'].top(-2)"
