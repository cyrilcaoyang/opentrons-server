"""Synthetic geometry and mocked transports only; no hardware acceptance here."""

import ast
from copy import deepcopy
from unittest.mock import Mock

import pytest

from opentrons_server.control.http_control import OT2HttpControl
from opentrons_server.control.ot2_control import OT2Control
from opentrons_server.gateway.assemblies import PlateAssembly
from opentrons_server.gateway.deck import DeckDeclarationStore, build_deck, make_slot_labware
from opentrons_server.gateway.models import MoveLabwareRequest
from opentrons_server.gateway.service import OT2Service


def plate(name="synthetic_96_wellplate", height=14):
    ordering = [[f"{r}{c}" for r in "ABCDEFGH"] for c in range(1, 13)]
    return {
        "schemaVersion": 2,
        "version": 1,
        "namespace": "custom",
        "metadata": {
            "displayName": name,
            "displayCategory": "wellPlate",
            "displayVolumeUnits": "µL",
        },
        "brand": {"brand": "Synthetic fixture"},
        "parameters": {
            "loadName": name,
            "format": "96Standard",
            "isTiprack": False,
            "isMagneticModuleCompatible": False,
        },
        "cornerOffsetFromSlot": {"x": 0, "y": 0, "z": 0},
        "dimensions": {"xDimension": 127.76, "yDimension": 85.48, "zDimension": height},
        "ordering": ordering,
        "wells": {
            f"{r}{c}": {
                "x": 14.38 + (c - 1) * 9,
                "y": 74.24 - i * 9,
                "z": 3,
                "depth": height - 3,
                "shape": "circular",
                "diameter": 6,
                "totalLiquidVolume": 200,
            }
            for c in range(1, 13)
            for i, r in enumerate("ABCDEFGH")
        },
        "groups": [{"wells": sum(ordering, []), "metadata": {"wellBottomShape": "flat"}}],
    }


def recipe(stack=False):
    return {
        "schema_version": 1,
        "kind": "filter_stack" if stack else "plate_on_riser",
        "riser_height_mm": 5,
        "top": {"plate_id": "filter" if stack else "plate", "definition": plate()},
        "collector": (
            {"plate_id": "collector", "definition": plate("synthetic_collector", 20)}
            if stack
            else None
        ),
        "nesting_overlap_mm": 2 if stack else 0,
    }


@pytest.mark.parametrize("stack, origin, height", [(False, 5, 19), (True, 23, 37)])
def test_compilation_translates_wells_preserves_depth_capacity_and_input(stack, origin, height):
    raw = recipe(stack)
    before = deepcopy(raw)
    assembly = PlateAssembly.model_validate(raw)
    d = assembly.compile_definition()
    assert raw == before
    assert assembly.top_origin_z_mm == origin
    assert d["dimensions"]["zDimension"] == height
    assert len(d["wells"]) == 96
    for name, well in d["wells"].items():
        assert well == {**raw["top"]["definition"]["wells"][name], "z": 3 + origin}
    assert "assembly" not in d and "plate_id" not in str(d)


def test_compiled_definition_conforms_to_opentrons_schema():
    jsonschema = pytest.importorskip("jsonschema")
    data = pytest.importorskip("opentrons_shared_data.labware")
    jsonschema.validate(
        PlateAssembly.model_validate(recipe(True)).compile_definition(), data.load_schema()
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["top"]["definition"]["parameters"].update(isTiprack=True),
        lambda r: r["top"]["definition"]["metadata"].update(displayCategory="tipRack"),
        lambda r: r.update(riser_height_mm=10),
        lambda r: r.update(nesting_overlap_mm=-1),
        lambda r: r.update(nesting_overlap_mm=float("nan")),
        lambda r: r.update(nesting_overlap_mm=14),
        lambda r: r["collector"].update(plate_id="filter"),
        lambda r: r["top"]["definition"]["wells"]["A1"].update(z=-1),
        lambda r: r["top"]["definition"]["wells"]["A1"].update(depth=50),
        lambda r: r["top"]["definition"]["wells"]["A1"].update(x=15.38),
        lambda r: r["top"]["definition"]["ordering"].pop(),
        lambda r: r["top"]["definition"].update(cornerOffsetFromSlot={"x": 0, "y": 0, "z": 5}),
    ],
)
def test_invalid_or_unsupported_geometry_is_rejected(change):
    raw = recipe(True)
    change(raw)
    with pytest.raises(ValueError):
        PlateAssembly.model_validate(raw)


def test_geometry_hash_changes_with_seating_but_not_plate_identity():
    raw = recipe(True)
    name = PlateAssembly.model_validate(raw).compile_definition()["parameters"]["loadName"]
    raw["top"]["plate_id"] = "another_filter"
    assert PlateAssembly.model_validate(raw).compile_definition()["parameters"]["loadName"] == name
    raw["nesting_overlap_mm"] = 3
    assert PlateAssembly.model_validate(raw).compile_definition()["parameters"]["loadName"] != name


def service(tmp_path):
    return OT2Service(dry_run=True, decks=DeckDeclarationStore(state_path=tmp_path / "deck.json"))


def test_persistence_recompiles_and_full_layout_preserves_components(tmp_path):
    s = service(tmp_path)
    s.declare_deck({"2": {"assembly": recipe(True), "definition": {"wrong": "ignored"}}})
    item = s.decks.get()["2"]
    s.declare_deck({"2": item.model_dump(), "3": "corning_96_wellplate_360ul_flat"})
    reloaded = DeckDeclarationStore(state_path=tmp_path / "deck.json").get()["2"]
    assert reloaded == item
    assert reloaded.assembly.collector.plate_id == "collector"
    assert reloaded.definition["wells"]["A1"]["z"] == 26


def test_status_preserves_declared_provenance_and_detects_wrong_geometry(tmp_path):
    s = service(tmp_path)
    s.declare_deck({"2": {"assembly": recipe(True)}})
    item = s.decks.get()["2"]
    deck = build_deck(declared={"2": item}, run={"2": make_slot_labware(item.load_name)})
    assert deck.slots["2"].slot_state == "occupied"
    assert deck.slots["2"].labware.assembly is None
    assert deck.slots["2"].labware.definition == item.definition
    assert deck.slots["2"].declared.assembly == item.assembly
    deck = build_deck(declared={"2": item}, run={"2": make_slot_labware("ac_assembly_wrong")})
    assert deck.slots["2"].slot_state == "mismatch"


def test_loaded_assembly_cannot_be_changed_cleared_or_moved(tmp_path):
    s = service(tmp_path)
    s.declare_deck({"2": {"assembly": recipe(True)}})
    s.control = Mock()
    assert s._resolve_session_labware("filter") == "slot_2"
    config = s.control.load_labware.call_args.args[0]["config"]
    assert config["wells"]["A1"]["z"] == 26
    with pytest.raises(ValueError, match="covered"):
        s._resolve_session_labware("collector")
    for mapping in ({}, {"2": "corning_96_wellplate_360ul_flat"}):
        with pytest.raises(ValueError, match="loaded"):
            s.declare_deck(mapping)
    with pytest.raises(ValueError, match="fixed assemblies"):
        s.move_labware(MoveLabwareRequest(labware_nickname="slot_2", new_location="3"))
    # An unrelated slot edit remains possible.
    s.declare_deck({"2": s.decks.get()["2"].model_dump(), "3": "corning_96_wellplate_360ul_flat"})
    s.control.load_labware.assert_called_once()


def test_cannot_add_riser_under_already_loaded_plate(tmp_path):
    s = service(tmp_path)
    s._session_labware["2"] = "slot_2"
    with pytest.raises(ValueError, match="loaded"):
        s.declare_deck({"2": {"assembly": recipe()}})
    assert s.decks.get() == {}


def test_duplicate_plate_id_refused_atomically(tmp_path):
    s = service(tmp_path)
    with pytest.raises(ValueError, match="multiple assemblies"):
        s.declare_deck({"2": {"assembly": recipe()}, "3": {"assembly": recipe()}})
    assert s.decks.get() == {}


def test_identical_compiled_geometry_reaches_http_and_ssh():
    d = PlateAssembly.model_validate(recipe(True)).compile_definition()
    client = Mock()
    client.execute.return_value = {"status": "succeeded"}
    http = OT2HttpControl(client)
    ssh = object.__new__(OT2Control)
    ssh.invoke = Mock()
    config = {"nickname": "stack", "location": "2", "ot_default": False, "config": d}
    http.load_labware(config)
    ssh.load_labware(config)
    statement = ast.parse(ssh.invoke.call_args.args[0]).body[0]
    sent = {kw.arg: ast.literal_eval(kw.value) for kw in statement.value.keywords}
    assert sent["labware_def"] == client.add_labware_definition.call_args.args[0] == d
    assert sent["location"] == "2"


def test_setup_compiles_assembly_and_keeps_components(tmp_path):
    s = service(tmp_path)
    s.setup_protocol(
        {"labware": [{"nickname": "stack", "location": "2", "assembly": recipe(True)}]}
    )
    item = s.session_recipe["labware"][0]
    assert item["config"]["wells"]["A1"]["z"] == 26
    assert item["ot_default"] is False
    assert s._declared_slots()["2"].assembly.collector.plate_id == "collector"


def test_successful_shutdown_allows_redeclaration_without_stale_geometry(tmp_path):
    s = service(tmp_path)
    s.declare_deck({"2": {"assembly": recipe(True)}})
    item = s.decks.get()["2"]
    s._last_run_labware = {
        "labware": [{"id": "stack", "loadName": item.load_name, "location": {"slotName": "2"}}]
    }
    s.control = Mock()
    # An observed assembly is adopted instead of loaded twice.
    assert s._resolve_session_labware("filter") == "stack"
    s.control.load_labware.assert_not_called()
    s.shutdown()
    s.clear_deck()
    s.declare_deck({"2": "corning_96_wellplate_360ul_flat"})
    assert s._last_run_labware is None
    assert s.session_recipe["labware"] == []


def test_failed_shutdown_does_not_allow_editing_loaded_assembly(tmp_path):
    s = service(tmp_path)
    s.declare_deck({"2": {"assembly": recipe(True)}})
    s._session_labware["2"] = "stack"
    s.control = Mock()
    s.control.shutdown.side_effect = RuntimeError("transport lost")
    with pytest.raises(RuntimeError, match="transport lost"):
        s.shutdown()
    with pytest.raises(ValueError, match="loaded"):
        s.clear_deck()


def test_modules_and_assemblies_in_same_declaration_are_rejected(tmp_path):
    s = service(tmp_path)
    with pytest.raises(ValueError, match="without modules"):
        s.declare_deck({"2": {"assembly": recipe()}, "3": "temperature_module"})
    assert s.decks.get() == {}


def test_setup_cannot_replace_declared_stack_with_bare_plate(tmp_path):
    s = service(tmp_path)
    s.declare_deck({"2": {"assembly": recipe(True)}})
    with pytest.raises(ValueError, match="preserve its declared assembly"):
        s.setup_protocol(
            {
                "labware": [
                    {
                        "nickname": "plain",
                        "location": "2",
                        "ot_default": True,
                        "loadname": "corning_96_wellplate_360ul_flat",
                    }
                ]
            }
        )


def test_overall_height_limit_and_no_riser_filter_stack():
    raw = recipe(True)
    raw["riser_height_mm"] = 0
    assert PlateAssembly.model_validate(raw).total_height_mm == 32
    raw["top"]["definition"] = plate(height=190)
    with pytest.raises(ValueError, match="assembled height"):
        PlateAssembly.model_validate(raw)


def test_collector_samples_never_appear_as_filter_samples(tmp_path):
    from opentrons_server.gateway.models import LoadedPlate

    s = service(tmp_path)
    s.declare_deck({"2": {"assembly": recipe(True)}})
    plate_state = LoadedPlate(
        plate_id="collector",
        model="synthetic_collector",
        wells=[],
        loaded_at="2026-01-01T00:00:00Z",
    )
    deck = build_deck(
        declared=s.decks.get(), loaded_plate=plate_state, nickname_to_slot={"collector": "2"}
    )
    assert deck.slots["2"].labware.plate_id == "filter"
    assert deck.slots["2"].labware.wells is None


def test_preview_and_declaration_endpoints_are_metadata_only(tmp_path):
    from fastapi.testclient import TestClient
    from opentrons_server.gateway.api import create_app

    app = create_app(dry_run=True, auto_reconnect=False, enforce_claims=False)
    app.state.service.decks = DeckDeclarationStore(state_path=tmp_path / "deck.json")
    app.state.service.control = Mock()
    with TestClient(app) as client:
        preview = client.post("/labware/assemblies/preview", json=recipe(True))
        assert preview.status_code == 200, preview.text
        assert preview.json()["top_origin_z_mm"] == 23
        assert app.state.service.decks.get() == {}
        response = client.post(
            "/control/deck/declare", json={"slots": {"2": {"assembly": recipe(True)}}}
        )
        assert response.status_code == 200, response.text
        assert (
            response.json()["slots"]["2"]["declared"]["assembly"]["collector"]["plate_id"]
            == "collector"
        )
        app.state.service.control.load_labware.assert_not_called()
        app.state.service._session_labware["2"] = "slot_2"
        assert client.delete("/control/deck/declare").status_code == 422
