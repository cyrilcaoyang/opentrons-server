"""Flex source-schema translations, capability refusal and isolated profile tests."""
import os
import subprocess
import sys
from unittest.mock import Mock

import pytest

from opentrons_server.control.flex_control import FlexHttpControl
from opentrons_server.control.http_run import RunEngineError
from opentrons_server.gateway.robot_profile import profile_for

COMMANDS = ("moveLabware", "robot/moveTo", "robot/openGripperJaw", "robot/closeGripperJaw", "robot/moveAxesRelative", "robot/moveAxesTo")


@pytest.fixture
def control():
    client = Mock()
    client.request_timeout_s = 10
    def read(method, path, **kwargs):
        if path == "/health":
            return {"robot_model": "OT-3 Standard"}
        if path == "/openapi.json":
            return {"components": {"schemas": {name: {"properties": {"commandType": {"const": name}}} for name in COMMANDS}}}
        raise AssertionError(path)
    client._request_raw.side_effect = read
    client.get_run.return_value = {"labware": [], "pipettes": [], "modules": []}
    client.execute.return_value = {"status": "succeeded", "result": {}}
    ctl = FlexHttpControl(client)
    ctl.initialize_protocol()
    return ctl


def test_model_profiles_do_not_expand_ot2_limits():
    assert profile_for("OT-2").max_y == 347.5
    assert profile_for("Flex").max_y == 493.8
    assert "A1" not in profile_for("OT-2").slots
    assert "12" not in profile_for("Flex").slots
    with pytest.raises(ValueError):
        profile_for("anything")


def test_wrong_robot_identity_is_refused_before_creating_run(control):
    control.client.create_run.reset_mock()
    control.client._request_raw.side_effect = None
    control.client._request_raw.return_value = {"robot_model": "OT-2 Standard"}
    with pytest.raises(RunEngineError, match="observed Flex"):
        control.initialize_protocol()
    control.client.create_run.assert_not_called()


def test_missing_native_api_is_refused_before_creating_run(control):
    control.client.create_run.reset_mock()
    control.client._request_raw.side_effect = [{"robot_model":"OT-3 Standard"}, {"components":{"schemas":{}}}]
    with pytest.raises(RunEngineError, match="missing required commands"):
        control.initialize_protocol()
    control.client.create_run.assert_not_called()


def test_no_fixed_trash_assumption_and_unsupported_heads_refused(control):
    control.setup_protocol(instruments=[{"nickname":"p1000", "instrument_name":"flex_1channel_1000", "mount":"left"}])
    assert control._trash_area is None
    control.client.execute.reset_mock()
    with pytest.raises(ValueError, match="explicit drop"):
        control.drop_tip("p1000")
    with pytest.raises(ValueError, match="96-channel"):
        control.setup_protocol(instruments=[{"nickname":"p96", "instrument_name":"flex_96channel_1000", "mount":"left"}])
    control.client.execute.assert_not_called()


def test_gripper_move_uses_module_objects_offsets_and_native_strategy(control):
    control._labware_ids["plate"] = "plate-id"
    control._module_ids["hs"] = "hs-id"
    pickup, drop = {"x":0,"y":0,"z":-5}, {"x":0,"y":0,"z":-10}
    control.move_labware_w_gripper("plate", "hs", pickup, drop)
    control.client.execute.assert_called_with(("moveLabware", {
        "labwareId":"plate-id", "newLocation":{"moduleId":"hs-id"}, "strategy":"usingGripper",
        "pickUpOffset":pickup, "dropOffset":drop,
    }))
    with pytest.raises(ValueError, match="physical destination"):
        control.move_labware_w_gripper("plate", "OFF_DECK")


def test_manual_gripper_commands_match_public_robot_api(control):
    control.gripper_open_jaw()
    control.client.execute.assert_called_with(("robot/openGripperJaw", {}))
    control.gripper_close_jaw()
    control.client.execute.assert_called_with(("robot/closeGripperJaw", {}))
    control.gripper_move_to_absolute(100, 200, 150, 50)
    control.client.execute.assert_called_with(("robot/moveAxesTo", {"axis_map":{"x":100,"y":200,"extensionZ":150}, "speed":50}))
    control.gripper_move_to_absolute(100, 200, 150, 50, force_direct=False)
    control.client.execute.assert_called_with(("robot/moveTo", {"mount":"extension", "destination":{"x":100,"y":200,"z":150}, "speed":50}))
    control.gripper_move_to_relative(dz=-5)
    control.client.execute.assert_called_with(("robot/moveAxesRelative", {"axis_map":{"x":0,"y":0,"extensionZ":-5},"speed":50}))
    control.home_gripper()
    control.client.execute.assert_called_with(("home", {"axes":["extensionZ"]}))


def test_trash_configuration_is_explicit(control):
    control.load_trash_bin(location="A3")
    assert control._trash_area == "movableTrashA3"
    with pytest.raises(ValueError):
        control.load_trash_bin(location="12")


def test_flex_profile_routes_limits_deck_and_tracking_in_separate_process(tmp_path):
    env = {**os.environ, "OT2_ROBOT_MODEL":"Flex", "OT2_TRANSPORT":"http",
           "OT2_DRY_RUN":"true", "OT2_AUTO_RECONNECT":"false"}
    for name in ("TIP","PLATE","DECK"):
        env[f"OT2_{name}_STATE_PATH"] = str(tmp_path / f"{name}.json")
    code = '''
from fastapi.testclient import TestClient
from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.models import MoveLabwareRequest, CoordinateLocation
from opentrons_server.gateway.plans import PLAN_ACTIONS
app = create_app(dry_run=True, auto_reconnect=False)
with TestClient(app) as c:
    assert '/control/gripper-move-to-absolute' in c.get('/openapi.json').json()['paths']
    assert 'gripper_move_to_absolute' in PLAN_ACTIONS
    assert 'magmod_engage' not in PLAN_ACTIONS
    assert c.get('/docs/agent').json()['model'] == 'Opentrons Flex'
    assert app.state.service.declare_deck({'A2':'opentrons_flex_96_tiprack_1000ul'})
    assert 'A2' in app.state.service.tips.racks()
    assert 'A2' in app.state.service.get_status().details['snapshot']['deck']['slots']
    CoordinateLocation(x=450,y=450,z=200)
    MoveLabwareRequest(labware_nickname='plate',new_location='hs',use_gripper=True)
'''
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_nested_labware_locations_resolve_without_stale_recipe(monkeypatch):
    import opentrons_server.gateway.deck as deck
    monkeypatch.setattr(deck, "SLOTS", list(profile_for("Flex").slots))
    doc = {"modules":[{"id":"hs", "model":"heaterShakerModuleV1", "location":{"slotName":"D1"}}],
           "labware":[{"id":"adapter", "loadName":"adapter", "location":{"moduleId":"hs"}},
                      {"id":"plate", "loadName":"corning_96_wellplate_360ul_flat", "location":{"labwareId":"adapter"}}]}
    slots = deck.normalize_run_slots(doc)
    assert slots["D1"].nickname == "plate"
    assert slots["D1"].kind == "96-well"
    assert deck.normalize_run_modules(doc)["D1"].module_name == "heaterShakerModuleV1"
