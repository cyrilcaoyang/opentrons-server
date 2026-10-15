"""Every declared deck item is in the run before a planned move.

The robot flies between labware at the height of the tallest item loaded in
its run. The gateway used to load a declared slot only when a step addressed
it, so on 2026-10-14 (Complexation) a drop_tip move from slot 2 to the trash
planned around the slot 2 rack and slot 7 tip rack alone and flew into the
balance lift in slot 9. These tests pin the rule that replaced lazy loading:
before any action that can plan an arc, the whole declaration — and an
envelope for the balance rig — is loaded, or the action is refused.
"""

import json
import pathlib
from unittest.mock import Mock

import pytest

from opentrons_server.control import RunEngineHTTPError
from opentrons_server.control.ot2_control import OT2Control
from opentrons_server.gateway.deck import DeckDeclarationStore
from opentrons_server.gateway.models import (
    CoordinateLocation,
    LiquidMoveRequest,
    MoveToRequest,
    WellLocation,
)
from opentrons_server.gateway.platebalance import (
    PlateBalanceConfig,
    PlateBalanceV1,
    balance_obstacle_definition,
)
from opentrons_server.gateway.plate_state import PlateStateStore
from opentrons_server.gateway.service import (
    DeckObstaclesNotLoaded,
    OT2Service,
    OT2ServiceState,
)
from opentrons_server.gateway.tip_state import TipStateStore

# Complexation's declaration on 2026-10-14, minus the balance plate.
_DECK = {
    "1": "agilent_96_2ml_deep_square",
    "3": "opentrons_6_tuberack_falcon_50ml_conical",
    "4": "opentrons_96_filtertiprack_10ul",
    "5": "opentrons_96_tiprack_20ul",
    "7": "opentrons_96_tiprack_300ul",
    "8": "opentrons_96_tiprack_300ul",
    "10": "temperature_module",
    "11": "opentrons_calibrationblock_short_side_right",
    "12": "opentrons_1_trash_3200ml_fixed",
}


def _service(tmp_path, *, obstacle_height_mm=132.0, balance=True):
    platebalance = None
    if balance:
        config = PlateBalanceConfig(com_port="COM3", obstacle_height_mm=obstacle_height_mm)
        platebalance = PlateBalanceV1(config, driver_factory=Mock(return_value=Mock()))
    service = OT2Service(
        dry_run=False,
        platebalance=platebalance,
        decks=DeckDeclarationStore(state_path=tmp_path / "deck.json"),
        plates=PlateStateStore(state_path=tmp_path / "plate.json"),
        tips=TipStateStore(state_path=tmp_path / "tips.json"),
    )
    control = Mock()
    service.control = control
    service.state = OT2ServiceState.READY
    service.refresh_snapshot = Mock()
    return service, control


def _move(service):
    service.move_to(MoveToRequest(pipette="p300", coordinates=CoordinateLocation(x=10, y=20, z=150)))


def _loaded_labware_slots(control):
    return {c.args[0]["location"]: c.args[0] for c in control.load_labware.call_args_list}


def test_a_planned_move_loads_every_declared_item_first(tmp_path):
    service, control = _service(tmp_path)
    service.declare_deck(_DECK)
    calls = []
    control.load_labware.side_effect = lambda cfg: calls.append(("load", cfg["location"]))
    control.load_module.side_effect = lambda cfg: calls.append(("module", cfg["location"]))
    control.move_to_pip.side_effect = lambda *a, **k: calls.append(("move", None))

    _move(service)

    loaded = {slot for kind, slot in calls if kind == "load"}
    # Every declared labware slot plus the balance envelope; the fixed trash
    # is a deck fixture the robot already models, never loadable labware.
    assert loaded == {"1", "3", "4", "5", "7", "8", "9", "11"}
    assert ("module", "10") in calls
    assert calls[-1] == ("move", None), "nothing may move before the deck is loaded"
    assert service.state == OT2ServiceState.READY


def test_the_balance_envelope_is_the_configured_height_and_unaddressable(tmp_path):
    service, control = _service(tmp_path)
    _move(service)

    cfg = _loaded_labware_slots(control)["9"]
    assert cfg["ot_default"] is False
    assert cfg["nickname"] == "balance_obstacle_9"
    assert cfg["config"]["dimensions"]["zDimension"] == 132.0
    assert cfg["config"] == balance_obstacle_definition(132.0)

    with pytest.raises(ValueError, match="planning obstacle"):
        service._resolve_session_labware("balance_obstacle_9")


def test_the_envelope_definition_is_valid_schema_2_labware():
    jsonschema = pytest.importorskip("jsonschema")
    shared = pytest.importorskip("opentrons_shared_data")
    schema = json.loads((pathlib.Path(shared.__file__).parent / "data/labware/schemas/2.json").read_text())
    definition = balance_obstacle_definition(132.0)
    jsonschema.validate(definition, schema)
    assert definition["parameters"]["loadName"] == "ac_balance_obstacle_1320"


def test_the_envelope_nickname_is_a_valid_repl_identifier():
    # The SSH transport binds every load to a Python name in the robot REPL.
    assert OT2Control._validated_identifier("balance_obstacle_9") == "balance_obstacle_9"


def test_loading_happens_once_per_session(tmp_path):
    service, control = _service(tmp_path)
    service.declare_deck(_DECK)
    _move(service)
    labware_loads, module_loads = control.load_labware.call_count, control.load_module.call_count

    _move(service)

    assert control.load_labware.call_count == labware_loads
    assert control.load_module.call_count == module_loads


def test_exempt_actions_load_nothing(tmp_path):
    service, control = _service(tmp_path)
    service.declare_deck(_DECK)

    service.home()

    control.load_labware.assert_not_called()
    control.load_module.assert_not_called()
    control.home.assert_called_once()


def test_missing_obstacle_height_is_a_run_blocking_fault(tmp_path):
    service, control = _service(tmp_path, obstacle_height_mm=None)

    status = service.get_status()
    # STATUS_SPEC §2.2: known run-blocking fault, not a top-level ready.
    assert status.equipment_status == "error"
    assert "obstacle_height_mm" in status.message
    assert status.required_actions == ["Set platebalance obstacle_height_mm and restart the gateway"]
    for action in ("move_to", "pick_up_tip", "aspirate", "dispense", "drop_tip", "move_labware", "mix"):
        assert action not in status.allowed_actions
    for action in ("home", "shutdown", "platebalance.read", "deck.declare"):
        assert action in status.allowed_actions
    assert "obstacle_height_mm" in status.details["platebalance"]["obstacle"]["error"]

    with pytest.raises(RuntimeError, match="not allowed"):
        _move(service)
    with pytest.raises(RuntimeError, match="obstacle_height_mm"):
        service._load_declared_obstacles()
    control.move_to_pip.assert_not_called()
    control.load_labware.assert_not_called()


def test_a_failed_load_blocks_motion_and_survives_reconcile(tmp_path):
    service, control = _service(tmp_path)
    service.declare_deck({"3": "opentrons_6_tuberack_falcon_50ml_conical"})

    def refuse_slot_3(cfg):
        if cfg["location"] == "3":
            raise RuntimeError("labware definition not found")

    control.load_labware.side_effect = refuse_slot_3

    with pytest.raises(DeckObstaclesNotLoaded, match="slot|definition not found"):
        _move(service)
    control.move_to_pip.assert_not_called()
    assert service.state == OT2ServiceState.ERROR
    assert "refused before motion" in service.last_error.message

    # Acknowledging the error does not acknowledge the missing obstacle: the
    # next planned move tries again and is refused again.
    service.reconcile()
    assert service.state == OT2ServiceState.READY
    with pytest.raises(DeckObstaclesNotLoaded):
        _move(service)
    control.move_to_pip.assert_not_called()


def test_a_transport_error_while_loading_is_not_the_moves_unknown_outcome(tmp_path):
    service, control = _service(tmp_path)
    control.load_labware.side_effect = OSError("connection reset")

    with pytest.raises(DeckObstaclesNotLoaded):
        service.aspirate(LiquidMoveRequest(
            pipette="p300", volume_ul=10,
            location=WellLocation(labware_nickname="1", position="A1"),
        ))

    control.aspirate.assert_not_called()
    assert service.state == OT2ServiceState.ERROR  # not UNKNOWN_OUTCOME: nothing was aspirated


def test_a_lost_run_still_reaches_dead_session_recovery(tmp_path):
    service, control = _service(tmp_path)
    control.load_labware.side_effect = RunEngineHTTPError(409, "Run abc is not the current run", path="/runs/abc/commands")

    with pytest.raises(RunEngineHTTPError):
        _move(service)

    assert service.control is None
    assert service.state == OT2ServiceState.REQUIRES_INIT


def test_balance_slot_shows_the_declaration_not_a_conflict(tmp_path):
    service, control = _service(tmp_path)
    _move(service)
    # The run now reports the envelope as labware in slot 9.
    service._last_run_labware = {
        "labware": [{"id": "balance_obstacle_9", "loadName": "ac_balance_obstacle_1320",
                     "location": {"slotName": "9"}}],
        "modules": [],
    }

    slot = service._build_deck_state().slots["9"]

    assert slot.slot_state != "mismatch"
    assert slot.module is not None and slot.module.module_name == "platebalanceV1"
    assert service._balance_placement_valid()
    obstacle = service.get_status().details["platebalance"]["obstacle"]
    assert obstacle == {"height_mm": 132.0, "loaded_as": "envelope", "error": None}


def test_a_loaded_slot_cannot_change_but_an_identical_redeclare_can(tmp_path):
    service, control = _service(tmp_path, balance=False)
    service.declare_deck({"4": "opentrons_96_tiprack_20ul"})
    _move(service)

    service.declare_deck({"4": "opentrons_96_tiprack_20ul"})  # unchanged full layout
    with pytest.raises(ValueError, match="slot 4 is loaded"):
        service.declare_deck({"4": "opentrons_96_tiprack_300ul"})
    with pytest.raises(ValueError, match="slot 4 is loaded"):
        service.declare_deck({})


def test_an_addition_to_an_empty_slot_is_loaded_by_the_next_move(tmp_path):
    service, control = _service(tmp_path, balance=False)
    service.declare_deck({"4": "opentrons_96_tiprack_20ul"})
    _move(service)

    service.declare_deck({"4": "opentrons_96_tiprack_20ul", "6": "opentrons_96_tiprack_300ul"})
    _move(service)

    assert [c.args[0]["location"] for c in control.load_labware.call_args_list] == ["4", "6"]


def test_the_plate_on_the_balance_may_change_under_the_envelope(tmp_path):
    service, control = _service(tmp_path)
    _move(service)
    plate = {"load_name": "custom_test_plate", "support_module": "platebalanceV1", "definition": {
        "schemaVersion": 2, "parameters": {"loadName": "custom_test_plate", "isTiprack": False},
        "metadata": {"displayCategory": "wellPlate", "displayName": "Test plate"},
        "dimensions": {"xDimension": 127.76, "yDimension": 85.48, "zDimension": 19},
        "ordering": [["A1"]], "wells": {"A1": {"x": 10, "y": 10, "z": 1, "depth": 5}}}}

    service.declare_deck({"9": plate})
    service.declare_deck({})


def test_setup_cannot_place_over_a_slot_loaded_from_the_declaration(tmp_path):
    service, control = _service(tmp_path, balance=False)
    service.declare_deck({"4": "opentrons_96_tiprack_20ul"})
    _move(service)

    with pytest.raises(ValueError, match="already loaded from the deck declaration"):
        service.setup_protocol({"labware": [
            {"nickname": "tips", "loadname": "opentrons_96_tiprack_20ul", "location": "4", "ot_default": True},
        ]})


def test_slots_a_setup_recipe_loaded_are_not_loaded_again(tmp_path):
    service, control = _service(tmp_path, balance=False)
    service.declare_deck({"4": "opentrons_96_tiprack_20ul", "5": "opentrons_96_tiprack_20ul"})
    service.setup_protocol({"labware": [
        {"nickname": "tips", "loadname": "opentrons_96_tiprack_20ul", "location": "4", "ot_default": True},
    ]})

    _move(service)

    assert [c.args[0]["location"] for c in control.load_labware.call_args_list] == ["5"]


def test_a_new_session_loads_the_deck_again(tmp_path):
    service, control = _service(tmp_path, balance=False)
    service.declare_deck({"4": "opentrons_96_tiprack_20ul"})
    _move(service)
    service._drop_dead_session("robot restarted")
    service.control = control
    service.state = OT2ServiceState.READY

    _move(service)

    assert [c.args[0]["location"] for c in control.load_labware.call_args_list] == ["4", "4"]


def test_declared_labware_cannot_be_moved_off_its_slot(tmp_path):
    # Off deck it would leave the run's obstacles while its declaration and
    # the physical deck still have it in place.
    from opentrons_server.gateway.models import MoveLabwareRequest

    service, control = _service(tmp_path, balance=False)
    service.declare_deck({"3": "opentrons_6_tuberack_falcon_50ml_conical"})
    _move(service)

    for ref in ("3", "slot_3"):
        with pytest.raises(ValueError, match="comes from the deck declaration"):
            service.move_labware(MoveLabwareRequest(labware_nickname=ref, new_location="offDeck"))
    control.move_labware.assert_not_called()


def test_a_recipe_loaded_slot_cannot_change_its_declaration(tmp_path):
    service, control = _service(tmp_path, balance=False)
    service.setup_protocol({"labware": [
        {"nickname": "plate", "loadname": "corning_96_wellplate_360ul_flat", "location": "3", "ot_default": True},
    ]})

    with pytest.raises(ValueError, match="slot 3 is loaded"):
        service.declare_deck({"3": "opentrons_6_tuberack_falcon_50ml_conical"})


def test_a_recipe_from_a_lost_run_blocks_moves_until_setup_or_shutdown(tmp_path):
    service, control = _service(tmp_path, balance=False)
    recipe = {"labware": [
        {"nickname": "plate", "loadname": "corning_96_wellplate_360ul_flat", "location": "3", "ot_default": True},
    ]}
    service.setup_protocol(recipe)
    service._drop_dead_session("robot restarted")
    service.control = control
    service.state = OT2ServiceState.READY

    # The new run is empty, and nothing declares slot 3 any more.
    assert service.session_recipe["labware"] == []
    status = service.get_status()
    assert status.equipment_status == "error"
    assert "slots 3" in status.message
    with pytest.raises(RuntimeError, match="not allowed"):
        _move(service)

    service.setup_protocol(recipe)  # the operator restores the run
    assert service.get_status().equipment_status == "ready"
    _move(service)
    control.move_to_pip.assert_called_once()

    service._drop_dead_session("robot restarted again")
    service.control = control
    service.state = OT2ServiceState.READY
    service.shutdown()  # or closes the session: declarations are the truth now
    assert service._deck_obstacle_fault() is None


def test_declared_labware_cannot_be_moved_even_as_the_first_command(tmp_path):
    from opentrons_server.gateway.models import MoveLabwareRequest

    service, control = _service(tmp_path, balance=False)
    service.declare_deck({"3": "opentrons_6_tuberack_falcon_50ml_conical"})

    with pytest.raises(ValueError, match="comes from the deck declaration"):
        service.move_labware(MoveLabwareRequest(labware_nickname="3", new_location="offDeck"))
    control.move_labware.assert_not_called()


def _lose_recipe(service, control, recipe):
    service.setup_protocol(recipe)
    service._drop_dead_session("robot restarted")
    service.control = control
    service.state = OT2ServiceState.READY


_PLATE_IN_3 = {"labware": [
    {"nickname": "plate", "loadname": "corning_96_wellplate_360ul_flat", "location": "3", "ot_default": True},
]}


def test_a_setup_that_restores_nothing_keeps_the_lost_slot_blocked(tmp_path):
    service, control = _service(tmp_path, balance=False)
    _lose_recipe(service, control, _PLATE_IN_3)

    service.setup_protocol({"instruments": [
        {"nickname": "p300", "instrument_name": "p300_single_gen2", "mount": "left", "ot_default": True},
    ]})

    assert service._recipe_slots_lost == {"3"}
    assert service.get_status().equipment_status == "error"


def test_declaring_a_lost_slot_gives_it_a_representation_again(tmp_path):
    service, control = _service(tmp_path, balance=False)
    _lose_recipe(service, control, _PLATE_IN_3)

    service.declare_deck({"3": "corning_96_wellplate_360ul_flat"})
    _move(service)

    assert [c.args[0]["location"] for c in control.load_labware.call_args_list] == ["3"]


def test_a_full_layout_redeclare_may_repeat_a_recipe_slot(tmp_path):
    service, control = _service(tmp_path, balance=False)
    service.declare_deck({"4": "opentrons_96_tiprack_20ul"})
    service.setup_protocol(_PLATE_IN_3)

    service.declare_deck({"4": "opentrons_96_tiprack_20ul"})  # the standing layout, unchanged
    # What the panel reads back for slot 3 is the recipe's item.
    service.declare_deck({"3": "corning_96_wellplate_360ul_flat", "4": "opentrons_96_tiprack_20ul"})
    with pytest.raises(ValueError, match="slot 3 is loaded"):
        service.declare_deck({"3": "nest_96_wellplate_200ul_flat", "4": "opentrons_96_tiprack_20ul"})
