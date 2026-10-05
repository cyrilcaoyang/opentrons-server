"""Tip lifecycle folded into the gateway: setup registration, pick validation,
sample marking on aspirate/dispense, empty on drop, /status surfacing, 412s."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.deck import DeckDeclarationStore
from opentrons_server.gateway.models import (
    LiquidMoveRequest,
    MoveToRequest,
    TipRequest,
    WellLocation,
    WellSample,
)
from opentrons_server.gateway.plate_state import PlateStateStore
from opentrons_server.gateway.service import (
    OT2Service,
    OT2ServiceState,
    UnknownOutcomeError,
)
from opentrons_server.gateway.tip_state import TipStateStore, TipUnavailable

RECIPE = {
    "labware": [
        {"nickname": "plate_D", "loadname": "corning_96_wellplate_360ul_flat", "location": "1", "ot_default": True},
        {"nickname": "tips_300", "loadname": "opentrons_96_tiprack_300ul", "location": "4", "ot_default": True},
    ],
    "instruments": [
        {"nickname": "p300", "instrument_name": "p300_single_gen2", "mount": "left", "ot_default": True}
    ],
    "modules": [],
}

# Both mounts loaded, as on ot2_complexation: a single-channel p300 and an
# 8-channel p20. `channels` is declared explicitly here — the robot's own
# GET /instruments report is the other source (see the by-mount test).
MULTI_RECIPE = {
    "labware": [
        {"nickname": "plate_D", "loadname": "corning_96_wellplate_360ul_flat", "location": "1", "ot_default": True},
        {"nickname": "tips_300", "loadname": "opentrons_96_tiprack_300ul", "location": "4", "ot_default": True},
        {"nickname": "tips_20", "loadname": "opentrons_96_tiprack_20ul", "location": "5", "ot_default": True},
    ],
    "instruments": [
        {"nickname": "p300", "instrument_name": "p300_single_gen2", "mount": "left", "channels": 1},
        {"nickname": "p20", "instrument_name": "p20_multi_gen2", "mount": "right", "channels": 8},
    ],
    "modules": [],
}


def _recipe_without_channels() -> dict:
    """``MULTI_RECIPE`` with the explicit counts stripped, leaving only mounts."""

    return {
        **MULTI_RECIPE,
        "instruments": [
            {k: v for k, v in inst.items() if k != "channels"}
            for inst in MULTI_RECIPE["instruments"]
        ],
    }


@pytest.fixture
def service(tmp_path):
    svc = OT2Service(
        dry_run=False,
        plates=PlateStateStore(state_path=tmp_path / "plate.json"),
        decks=DeckDeclarationStore(state_path=tmp_path / "deck.json"),
        tips=TipStateStore(state_path=tmp_path / "tips.json"),
    )
    control = Mock()
    svc.control = control
    svc.refresh_snapshot = Mock(return_value={})
    svc.state = OT2ServiceState.READY
    return svc


def _pick(service, well=None, sample_id=None, force=False, pipette="p300", rack="tips_300"):
    service.pick_up_tip(
        TipRequest(
            pipette=pipette,
            labware_nickname=rack,
            position=well,
            sample_id=sample_id,
            force=force,
        )
    )


def _move(service, kind, labware, well, pipette="p300"):
    request = LiquidMoveRequest(
        pipette=pipette,
        volume_ul=50,
        location=WellLocation(labware_nickname=labware, position=well),
    )
    getattr(service, kind)(request)


def test_pick_refused_before_motion_when_the_robot_reports_a_tip(service):
    """A tip the engine still believes is on (picked before a restart, or
    pulled off by hand after a halted plan) used to surface mid-plan as
    UnexpectedTipAttachError and ERROR. Ask the robot first: 412, state and
    ledger untouched, and the body says how to clear it."""
    service.setup_protocol(RECIPE)
    service.control.has_tip.return_value = True

    with pytest.raises(TipUnavailable) as excinfo:
        _pick(service, "A1")

    body = excinfo.value.body
    assert body["robot_reports_tip"] is True
    assert "drop_tip" in body["detail"] and '"pipette": "p300"' in body["detail"]
    assert "no tip" in body["detail"]  # the ledger's side of the disagreement
    assert service.state == OT2ServiceState.READY
    assert service.last_error is None
    assert service.tips.status("4", "A1") == "new"  # the pick was never marked
    assert service.tips.get_mount("p300") is None
    service.control.pick_up_tip.assert_not_called()


def test_drop_refused_before_motion_when_the_robot_reports_no_tip(service):
    service.setup_protocol(RECIPE)
    service.control.has_tip.return_value = False
    _pick(service, "A1")  # ledger: tip on p300 from 4/A1

    with pytest.raises(TipUnavailable) as excinfo:
        service.drop_tip(TipRequest(pipette="p300"))

    body = excinfo.value.body
    assert body["robot_reports_tip"] is False
    assert body["rack"] == "4" and body["well"] == "A1"
    assert "tips.mark" in body["detail"]
    assert service.state == OT2ServiceState.READY
    assert service.tips.get_mount("p300") is not None  # the ledger keeps its claim
    service.control.drop_tip.assert_not_called()


def test_tip_actions_proceed_when_the_robot_agrees_or_cannot_say(service):
    service.setup_protocol(RECIPE)
    # Mock() is neither True nor False: no readback, the engine stays the judge.
    _pick(service, "A1")
    service.control.pick_up_tip.assert_called_once()
    service.control.has_tip.return_value = True  # robot agrees a tip is on
    service.drop_tip(TipRequest(pipette="p300"))
    service.control.drop_tip.assert_called_once()
    service.control.has_tip.return_value = False  # robot agrees the head is bare
    _pick(service, "B1")
    assert service.control.pick_up_tip.call_count == 2


def test_setup_registers_tipracks_only(service):
    service.setup_protocol(RECIPE)

    # A rack is identified by the deck slot it sits in: tips_300 is on slot 4.
    assert service.tips.has_rack("4")
    assert not service.tips.has_rack("1")  # slot 1 holds the plate


def test_setup_registration_survives_used_tips(service):
    service.setup_protocol(RECIPE)
    service.tips.set_status("4", "A1", "sample_X")

    service.setup_protocol(RECIPE)  # e.g. re-setup after a gateway restart
    assert service.tips.status("4", "A1") == "sample_X"


def test_pick_aspirate_dispense_drop_lifecycle(service):
    service.setup_protocol(RECIPE)

    _pick(service)  # auto-pick -> A1 (column-major first well)
    mounted = service._mounted_tips["p300"]
    assert (mounted["rack"], mounted["well"]) == ("4", "A1")
    service.control.get_location_from_labware.assert_called_with("tips_300", "A1")
    # The rack reflects the pick immediately: A1 is a hole from the moment the
    # tip leaves it, not from the moment it is thrown away.
    assert service.tips.status("4", "A1") == "on_pipette"
    assert mounted["contacted_liquid"] is False

    # What the tip touches is recorded on the mount — the well is a hole, and
    # stamping a sample id there would make it read as a re-pickable tip.
    _move(service, "aspirate", "reservoir", "A1")
    assert service._mounted_tips["p300"]["last_sample"] == "reservoir_A1"
    assert service._mounted_tips["p300"]["contacted_liquid"] is True
    assert service.tips.status("4", "A1") == "on_pipette"

    _move(service, "dispense", "plate_D", "B2")
    assert service._mounted_tips["p300"]["last_sample"] == "plate_D_B2"

    service.drop_tip(TipRequest(pipette="p300"))
    assert service.tips.status("4", "A1") == "empty"
    assert "p300" not in service._mounted_tips

    # Next auto-pick skips the emptied well.
    _pick(service)
    assert service._mounted_tips["p300"]["well"] == "B1"


def test_pick_refuses_cross_sample_reuse(service):
    service.setup_protocol(RECIPE)
    service.tips.set_status("4", "A1", "sample_X")

    with pytest.raises(TipUnavailable):
        _pick(service, well="A1", sample_id="sample_Y")
    # Same sample is allowed, and the tip keeps its history while it is up.
    _pick(service, well="A1", sample_id="sample_X")
    assert service._mounted_tips["p300"]["origin_status"] == "sample_X"

    # Returned to its own well, the history comes back with it — so `force`
    # still has something to override on the next pick.
    service.drop_tip(TipRequest(pipette="p300", labware_nickname="tips_300", position="A1"))
    assert service.tips.status("4", "A1") == "sample_X"
    _pick(service, well="A1", force=True)


def test_pick_refuses_a_well_whose_tip_is_already_on_a_head(service):
    """Force overrides the contamination guard, never the absence of a tip."""

    service.setup_protocol(RECIPE)
    _pick(service, well="A1")

    with pytest.raises(TipUnavailable) as excinfo:
        _pick(service, well="A1", force=True)
    body = excinfo.value.body
    assert body["tip_status"] == "on_pipette"
    assert body["held_by"] == "p300"
    assert "on pipette p300" in body["detail"]


def test_auto_pick_advances_past_a_mounted_tip(service):
    """The old bug: with A1 still reading "new", a second pick re-targeted it."""

    service.setup_protocol(RECIPE)
    _pick(service)
    assert service._mounted_tips["p300"]["well"] == "A1"

    _pick(service, pipette="p300")  # no drop in between
    assert service._mounted_tips["p300"]["well"] == "B1"


def test_sample_id_resolves_from_loaded_plate(service):
    service.setup_protocol(RECIPE)
    service.load_plate(
        plate_id="plate_D",
        model="corning_96_wellplate_360ul_flat",
        wells=[WellSample(well="B2", sample_id="caffeine-001")],
    )

    _pick(service)
    _move(service, "dispense", "plate_D", "B2")
    assert service._mounted_tips["p300"]["last_sample"] == "caffeine-001"

    # ...and it lands in the rack when the tip is put back.
    service.drop_tip(TipRequest(pipette="p300", labware_nickname="tips_300", position="A1"))
    assert service.tips.status("4", "A1") == "caffeine-001"


def test_touching_the_rack_itself_does_not_mark(service):
    service.setup_protocol(RECIPE)
    _pick(service)
    _move(service, "aspirate", "tips_300", "A1")
    mounted = service._mounted_tips["p300"]
    assert mounted["last_sample"] is None
    assert mounted["contacted_liquid"] is False

    # A tip that never met liquid returns to the rack fresh, not "used".
    service.drop_tip(TipRequest(pipette="p300", labware_nickname="tips_300", position="A1"))
    assert service.tips.status("4", "A1") == "new"


def test_untracked_rack_behaves_as_before(service):
    # No setup -> nothing registered; explicit position passes straight through.
    service.pick_up_tip(
        TipRequest(pipette="p300", labware_nickname="tips_300", position="C5")
    )
    service.control.get_location_from_labware.assert_called_with("tips_300", "C5")
    assert service._mounted_tips == {}


# ---- the gateway supplies the tip location itself ----------------------------
#
# A caller that names no rack (an agent plan, a bare curl) still gets the next
# tip: the tracker, not the caller, owns "which rack, which well". And a pick
# that cannot be answered is refused BEFORE any hardware addressing — as a
# TipUnavailable (412), never by flipping the device to ERROR on a transport
# exception that fires mid-action.


def test_pick_with_no_rack_auto_selects_a_tracked_rack(service):
    service.setup_protocol(RECIPE)

    _pick(service, rack=None)

    mounted = service._mounted_tips["p300"]
    assert (mounted["rack"], mounted["well"]) == ("4", "A1")
    # Addressed by the recipe nickname, which is what the transport understands.
    service.control.get_location_from_labware.assert_called_with("tips_300", "A1")


def test_pick_with_no_rack_skips_an_exhausted_rack(service):
    two_racks = {
        **RECIPE,
        "labware": [
            *RECIPE["labware"],
            {
                "nickname": "tips_300_b",
                "loadname": "opentrons_96_tiprack_300ul",
                "location": "5",
                "ot_default": True,
            },
        ],
    }
    service.setup_protocol(two_racks)
    rack_4 = service.tips.racks()["4"]
    service.tips.set_statuses("4", list(rack_4.tips.keys()), "empty")

    _pick(service, rack=None)

    assert service._mounted_tips["p300"]["rack"] == "5"
    service.control.get_location_from_labware.assert_called_with("tips_300_b", "A1")


def test_pick_with_no_rack_skips_a_rack_whose_tips_do_not_fit(service):
    # MULTI_RECIPE: slot 4 holds 300 µL tips, slot 5 holds 20 µL tips. The
    # p20 must not be sent onto the 300 µL rack just because slot 4 sorts
    # first — a wrong-size pick is silent physical wrongness.
    service.setup_protocol(MULTI_RECIPE)

    _pick(service, rack=None, pipette="p20")

    mounted = service._mounted_tips["p20"]
    assert (mounted["rack"], mounted["well"]) == ("5", "A1")
    service.control.get_location_from_labware.assert_called_with("tips_20", "A1")


def test_pick_with_no_rack_and_no_usable_tip_is_a_precondition_refusal(service):
    service.setup_protocol(RECIPE)
    rack_4 = service.tips.racks()["4"]
    service.tips.set_statuses("4", list(rack_4.tips.keys()), "empty")

    with pytest.raises(TipUnavailable) as exc_info:
        _pick(service, rack=None)

    assert "slot 4" in exc_info.value.body["detail"]
    # The refusal happened before any hardware addressing, and it is not an
    # operational error: the device stays ready, last_error untouched (§6.3).
    service.control.pick_up_tip.assert_not_called()
    assert service.state == OT2ServiceState.READY
    assert service.last_error is None


def test_declared_deck_pick_provisions_the_session_on_demand(service):
    """The 2026-08-11 live failure: a declared deck (no /control/setup) has no
    session nicknames, so every tracked rack was 'not loaded in the control
    session' and a bare pick had nothing to address. The declared labware and
    the probed pipette are now loaded into the session at the point of use."""

    service.declare_deck({"9": "opentrons_96_tiprack_300ul"})
    service._last_probe = {
        "instruments": [
            {"mount": "left", "name": "p300_single_gen2", "channels": 1}
        ]
    }

    service.pick_up_tip(TipRequest(pipette="left"))

    service.control.load_instrument.assert_called_once_with(
        {
            "ot_default": True,
            "nickname": "left",
            "instrument_name": "p300_single_gen2",
            "mount": "left",
        }
    )
    service.control.load_labware.assert_called_once_with(
        {
            "ot_default": True,
            "nickname": "slot_9",
            "loadname": "opentrons_96_tiprack_300ul",
            "location": "9",
        }
    )
    service.control.get_location_from_labware.assert_called_with("slot_9", "A1")
    service.control.pick_up_tip.assert_called_with("left")
    assert service._mounted_tips["left"]["rack"] == "9"


def test_declared_deck_slot_and_mount_load_only_once(service):
    service.declare_deck({"9": "opentrons_96_tiprack_300ul"})
    service._last_probe = {
        "instruments": [
            {"mount": "left", "name": "p300_single_gen2", "channels": 1}
        ]
    }

    service.pick_up_tip(TipRequest(pipette="left", labware_nickname="9"))
    service.drop_tip(TipRequest(pipette="left"))
    service.pick_up_tip(TipRequest(pipette="left", labware_nickname="9"))

    assert service.control.load_labware.call_count == 1
    assert service.control.load_instrument.call_count == 1
    # Second pick skipped the emptied A1.
    assert service._mounted_tips["left"]["well"] == "B1"


def test_declared_deck_pick_without_an_attached_pipette_is_honest(service):
    service.declare_deck({"9": "opentrons_96_tiprack_300ul"})
    service._last_probe = {"instruments": []}

    with pytest.raises(Exception, match="no pipette known on mount 'left'"):
        service.pick_up_tip(TipRequest(pipette="left"))


def test_force_drop_is_refused_off_the_http_transport(service):
    from opentrons_server.gateway.models import DropTipRequest

    service.setup_protocol(RECIPE)
    _pick(service, "A1")

    with pytest.raises(ValueError, match="HTTP"):
        service.drop_tip(DropTipRequest(pipette="p300", force_drop=True))

    service.control.drop_tip.assert_not_called()
    assert service.tips.get_mount("p300") is not None


def test_drop_into_a_tracked_rack_well_relocates_the_tip(service):
    """The 2026-08-11 bench ask: move the tip from H12 into the rack's empty
    A1. The robot did it, but the tracker recorded only "H12 is gone" — A1
    never showed available. A drop into a tracked rack well is a relocation:
    the destination now holds the tip, keeping its history."""

    service.setup_protocol(RECIPE)
    service.tips.set_status("4", "A1", "empty")  # the hole being refilled
    _pick(service, well="H12")

    service.drop_tip(
        TipRequest(pipette="p300", labware_nickname="tips_300", position="A1")
    )

    assert service.tips.status("4", "H12") == "empty"
    # A relocated never-used tip is fresh — A1 counts as available again.
    assert service.tips.status("4", "A1") == "new"
    assert "p300" not in service._mounted_tips
    # The drop descends into the well so the tip seats, rather than being
    # released at the well top and landing crooked.
    service.control.get_location_from_labware.assert_called_with(
        "tips_300", "A1", bottom=10.0
    )


def test_relocated_used_tip_carries_its_sample(service):
    service.setup_protocol(RECIPE)
    service.tips.set_status("4", "A1", "empty")
    _pick(service, well="H12")
    _move(service, "aspirate", "reservoir", "A1")

    service.drop_tip(
        TipRequest(pipette="p300", labware_nickname="tips_300", position="A1")
    )

    assert service.tips.status("4", "A1") == "reservoir_A1"


def test_returning_a_tip_to_its_own_well_is_legal(service):
    service.setup_protocol(RECIPE)
    _pick(service, well="H12")

    service.drop_tip(
        TipRequest(pipette="p300", labware_nickname="tips_300", position="H12")
    )

    assert service.tips.status("4", "H12") == "new"


def test_drop_onto_a_seated_tip_is_refused_pre_motion(service):
    service.setup_protocol(RECIPE)
    _pick(service, well="H12")

    with pytest.raises(TipUnavailable, match="already hold tips"):
        service.drop_tip(
            TipRequest(pipette="p300", labware_nickname="tips_300", position="B5")
        )
    service.control.drop_tip.assert_not_called()
    # The tip is still on the head; nothing moved and nothing was recorded.
    assert "p300" in service._mounted_tips
    assert service.last_error is None


def test_drop_tip_accepts_trash_aliases_as_the_default_target(service):
    # The assistant naturally proposes drop_tip {"labware_nickname": "12"} for
    # "drop it in waste". Slot 12 IS the trash — route to the default rather
    # than trying to resolve labware that does not exist there.
    service.setup_protocol(RECIPE)
    _pick(service)
    location_calls = service.control.get_location_from_labware.call_count

    service.drop_tip(TipRequest(pipette="p300", labware_nickname="12"))

    service.control.drop_tip.assert_called_once_with("p300")
    # No explicit-location addressing happened for the alias.
    assert service.control.get_location_from_labware.call_count == location_calls


def test_drop_tip_refuses_a_half_specified_location(service):
    # Silently ignoring a lone labware_nickname is how "drop in slot 12" once
    # became a drop wherever the head happened to be. Refused pre-motion.
    service.setup_protocol(RECIPE)
    _pick(service)

    with pytest.raises(ValueError, match="both labware_nickname and position"):
        service.drop_tip(TipRequest(pipette="p300", labware_nickname="plate_D"))
    service.control.drop_tip.assert_not_called()
    assert service.last_error is None


def test_pick_untracked_rack_without_position_is_a_precondition_refusal(service):
    service.setup_protocol(RECIPE)

    with pytest.raises(TipUnavailable) as exc_info:
        _pick(service, rack="mystery_rack")

    assert "not a tracked tip rack" in exc_info.value.body["detail"]
    service.control.pick_up_tip.assert_not_called()
    assert service.state == OT2ServiceState.READY
    assert service.last_error is None


# ---- multi-channel pipettes -------------------------------------------------
#
# An 8-channel head sent to a row-A well removes the whole column, so tracking
# only the addressed well under-counts by seven. These pin the service side of
# that: binding the channel count, consuming the covered set, and refusing the
# picks a column-wide head cannot make.

COLUMN_1 = [f"{row}1" for row in "ABCDEFGH"]


def _column(number: int) -> list:
    return [f"{row}{number}" for row in "ABCDEFGH"]


def _statuses(service, wells, rack="5"):
    return {service.tips.status(rack, well) for well in wells}


def test_setup_binds_channel_count_from_the_recipe(service):
    service.setup_protocol(MULTI_RECIPE)

    assert service._pipette_channels == {"p300": 1, "p20": 8}
    assert service._channels_for("p300") == 1
    assert service._channels_for("p20") == 8


def test_setup_binds_channel_count_from_the_robot_probe_by_mount(service):
    # No `channels` in the recipe: the count comes from GET /instruments, joined
    # to the nickname by the mount the recipe declares.
    service._last_probe = {
        "instruments": [
            {"mount": "left", "model": "p300_single_gen2", "channels": 1},
            {"mount": "right", "model": "p20_multi_gen2", "channels": 8},
        ]
    }
    service.setup_protocol(_recipe_without_channels())

    assert service._pipette_channels == {"p300": 1, "p20": 8}


def test_unbound_pipette_falls_back_to_single_channel(service):
    # Neither an explicit count nor a reachable probe: behave exactly as before
    # multi-channel tracking existed rather than refusing picks outright.
    service.setup_protocol(_recipe_without_channels())

    assert service._channels_for("p20") == 1
    _pick(service, well="A1", pipette="p20", rack="tips_20")
    assert service._mounted_tips["p20"]["wells"] == ["A1"]


def test_multichannel_pick_and_drop_empties_the_whole_column(service):
    """The 2026-08-04 ot2_complexation run: p300 at A1 (1ch) + p20 at A1 (8ch)."""

    service.setup_protocol(MULTI_RECIPE)

    _pick(service, well="A1")
    _pick(service, well="A1", pipette="p20", rack="tips_20")
    assert service._mounted_tips["p20"]["wells"] == COLUMN_1
    assert service._mounted_tips["p20"]["channels"] == 8

    service.drop_tip(TipRequest(pipette="p300"))
    service.drop_tip(TipRequest(pipette="p20"))

    summary = service.tips.summary()
    # 9 tips left the deck, and the rack now says so: 88, not the 95 the
    # addressed-well-only tracker reported.
    assert summary["5"]["available"] == 88
    assert sorted(summary["5"]["tips"]) == COLUMN_1
    assert _statuses(service, COLUMN_1) == {"empty"}
    # The single-channel rack is unaffected.
    assert summary["4"]["available"] == 95
    assert summary["4"]["tips"] == {"A1": "empty"}


def test_multichannel_auto_pick_advances_by_column(service):
    service.setup_protocol(MULTI_RECIPE)

    _pick(service, pipette="p20", rack="tips_20")
    assert service._mounted_tips["p20"]["well"] == "A1"
    service.drop_tip(TipRequest(pipette="p20"))

    # A2, never B1 — B1's tip is already on channel two, so a B1 start would
    # descend on seven empty holes.
    _pick(service, pipette="p20", rack="tips_20")
    assert service._mounted_tips["p20"]["well"] == "A2"
    assert service._mounted_tips["p20"]["wells"] == _column(2)


def test_multichannel_marking_covers_the_whole_column(service):
    """One mount speaks for all 8 tips: the column moves as a unit, never
    into a mixed state where some wells still look pickable."""

    service.setup_protocol(MULTI_RECIPE)
    _pick(service, well="A1", pipette="p20", rack="tips_20")
    assert _statuses(service, COLUMN_1) == {"on_pipette"}

    _move(service, "aspirate", "reservoir", "A1", pipette="p20")
    assert service._mounted_tips["p20"]["last_sample"] == "reservoir_A1"
    assert _statuses(service, COLUMN_1) == {"on_pipette"}

    # Pick/drop round-trips: the column goes empty, not back to a mixed state.
    service.drop_tip(TipRequest(pipette="p20"))
    assert _statuses(service, COLUMN_1) == {"empty"}


def test_multichannel_return_restores_the_columns_history(service):
    service.setup_protocol(MULTI_RECIPE)
    _pick(service, well="A1", pipette="p20", rack="tips_20")
    _move(service, "aspirate", "reservoir", "A1", pipette="p20")

    service.drop_tip(
        TipRequest(pipette="p20", labware_nickname="tips_20", position="A1")
    )
    assert _statuses(service, COLUMN_1) == {"reservoir_A1"}


def test_multichannel_pick_refuses_a_partial_column(service):
    service.setup_protocol(MULTI_RECIPE)
    service.tips.set_status("5", "E1", "empty")

    with pytest.raises(TipUnavailable) as exc:
        _pick(service, well="A1", pipette="p20", rack="tips_20")
    body = exc.value.body
    assert body["blocking_well"] == "E1"
    assert body["well"] == "A1"
    assert body["channels"] == 8
    assert body["covered_wells"] == COLUMN_1
    assert "E1" in body["detail"]
    # Nothing moved and nothing was recorded.
    assert "p20" not in service._mounted_tips

    # A single-channel pipette can still take what is left of that column.
    _pick(service, well="A1")
    assert service._mounted_tips["p300"]["wells"] == ["A1"]


def test_multichannel_pick_refuses_a_non_row_a_address(service):
    service.setup_protocol(MULTI_RECIPE)

    with pytest.raises(TipUnavailable) as exc:
        _pick(service, well="B1", pipette="p20", rack="tips_20")
    assert exc.value.body["well"] == "B1"
    assert exc.value.body["channels"] == 8
    assert "row A" in exc.value.body["detail"]
    assert "p20" not in service._mounted_tips

    # ... while any row is fine for a single-channel head.
    _pick(service, well="B1")
    assert service._mounted_tips["p300"]["wells"] == ["B1"]


def test_multichannel_exhaustion_refuses_once_every_column_is_broken(service):
    service.setup_protocol(MULTI_RECIPE)
    for column in range(1, 13):
        service.tips.set_status("5", f"H{column}", "empty")

    with pytest.raises(TipUnavailable) as exc:
        _pick(service, pipette="p20", rack="tips_20")  # auto-pick
    assert exc.value.body["channels"] == 8
    assert exc.value.body["well"] is None

    # 84 tips remain, all reachable by a single-channel head.
    assert service.tips.next_available("5", channels=1) == "A1"


def test_status_surfaces_tip_state(service):
    service.setup_protocol(RECIPE)
    _pick(service)
    _move(service, "aspirate", "reservoir", "A1")

    details = service.get_status().details
    rack = details["tip_racks"]["4"]
    assert rack["available"] == 95
    # The well is a hole while the tip is up, counted apart from both the
    # tips still in the rack and the ones thrown away.
    assert rack["tips"] == {"A1": "on_pipette"}
    assert (rack["on_pipette"], rack["empty"], rack["touched"]) == (1, 0, 0)
    assert rack["held_by"] == {"A1": "p300"}

    mounted = details["mounted_tips"]["p300"]
    assert mounted["well"] == "A1"
    assert mounted["last_sample"] == "reservoir_A1"
    assert mounted["contacted_liquid"] is True
    assert mounted["uncertain"] is False
    assert mounted["picked_at"]


def test_status_surfaces_pipette_channels(service):
    """An unbound 8-channel head silently tracked as 1 is the whole bug, so the
    live binding is published for diagnosis."""

    service.setup_protocol(MULTI_RECIPE)
    _pick(service, well="A1", pipette="p20", rack="tips_20")

    details = service.get_status().details
    assert details["pipette_channels"] == {"p300": 1, "p20": 8}
    assert details["mounted_tips"]["p20"]["wells"] == COLUMN_1


def test_allowed_actions_include_tips_reset(service):
    assert "tips.reset" in service.allowed_actions()


def test_api_pick_up_tip_412_and_tips_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("OT2_TIP_STATE_PATH", str(tmp_path / "tips.json"))
    monkeypatch.setenv("OT2_PLATE_STATE_PATH", str(tmp_path / "plate.json"))
    monkeypatch.setenv("OT2_DECK_STATE_PATH", str(tmp_path / "deck.json"))
    app = create_app(dry_run=True, enforce_claims=False)
    client = TestClient(app)
    service = app.state.service
    # A protocol setup is what maps the nickname the caller sends to the slot
    # the tracker keys by — exercise that resolution, not a pre-seeded rack.
    service.setup_protocol(RECIPE)
    service.tips.set_status("4", "A1", "sample_X")

    response = client.post(
        "/control/pick-up-tip",
        json={
            "pipette": "p300",
            "labware_nickname": "tips_300",
            "position": "A1",
            "sample_id": "sample_Y",
        },
    )
    assert response.status_code == 412
    body = response.json()
    assert body["rack"] == "4"
    assert body["tip_status"] == "sample_X"
    # Precondition refusals never mutate last_error (STATUS_SPEC §6.3).
    assert client.get("/status").json()["last_error"] is None

    reset = client.post("/control/tips/reset", json={"slot": "4"})
    assert reset.status_code == 200
    assert reset.json()["tips"]["A1"] == "new"


def test_api_multichannel_partial_column_412_body(tmp_path, monkeypatch):
    """The 412 body is what a caller branches on, so the multi-channel fields
    (STATUS_SPEC §6.1: distinguishable by shape) have to survive the API layer."""

    monkeypatch.setenv("OT2_TIP_STATE_PATH", str(tmp_path / "tips.json"))
    monkeypatch.setenv("OT2_PLATE_STATE_PATH", str(tmp_path / "plate.json"))
    monkeypatch.setenv("OT2_DECK_STATE_PATH", str(tmp_path / "deck.json"))
    app = create_app(dry_run=True, enforce_claims=False)
    client = TestClient(app)
    service = app.state.service
    service.setup_protocol(MULTI_RECIPE)
    service.tips.set_status("5", "E1", "empty")

    response = client.post(
        "/control/pick-up-tip",
        json={"pipette": "p20", "labware_nickname": "tips_20", "position": "A1"},
    )
    assert response.status_code == 412
    body = response.json()
    assert body["blocking_well"] == "E1"
    assert body["covered_wells"] == [f"{row}1" for row in "ABCDEFGH"]
    assert body["channels"] == 8
    # A precondition refusal never mutates last_error (STATUS_SPEC §6.3).
    assert client.get("/status").json()["last_error"] is None


# ---- slot keying -----------------------------------------------------------
#
# A tip rack's identity is the deck slot it sits in. It carries no sample and
# no history worth naming, and what an operator points at — and refills — is
# "the rack in slot 4".


def test_declaring_a_tiprack_starts_tracking_it(service):
    """The reported bug: three racks declared on the deck, none of them in the
    panel, because registration only ever happened from a /control/setup
    recipe. A declaration is exactly the fact the tracker needs."""

    service.declare_deck(
        {
            "7": "opentrons_96_filtertiprack_10ul",
            "9": "opentrons_96_tiprack_300ul",
            "1": "corning_96_wellplate_360ul_flat",
        }
    )

    assert sorted(service.tips.racks()) == ["7", "9"]  # the plate is not a rack
    assert service.tips.summary()["7"]["available"] == 96


def test_tracking_survives_a_restart_without_a_setup(service, tmp_path):
    """Slot keying is what makes this work: the declared deck is persisted
    while session_recipe is in-memory and lost on every restart."""

    service.declare_deck({"7": "opentrons_96_filtertiprack_10ul"})
    service.tips.set_status("7", "A1", "empty")

    reborn = OT2Service(
        dry_run=False,
        plates=PlateStateStore(state_path=service.plates.state_path),
        decks=DeckDeclarationStore(state_path=service.decks.state_path),
        tips=TipStateStore(state_path=service.tips.state_path),
    )

    assert reborn.tips.has_rack("7")
    assert reborn.tips.status("7", "A1") == "empty"


def test_a_declared_rack_is_picked_up_at_boot_with_an_empty_tip_store(service):
    """The deck outlives the tip store, so boot has to read it.

    Live case on ot2_complexation: the deck had racks on 4 / 7 / 9 declared
    sessions ago, and the tip store held nothing that survived the legacy-key
    drop. Registration only ran on declare / setup, so the panel came back with
    no racks at all for a deck full of them — until the operator re-declared a
    slot they had already declared.
    """

    service.declare_deck(
        {
            "4": "opentrons_96_filtertiprack_20ul",
            "7": "opentrons_96_filtertiprack_10ul",
            "9": "opentrons_96_tiprack_300ul",
        }
    )

    reborn = OT2Service(
        dry_run=False,
        plates=PlateStateStore(state_path=service.plates.state_path),
        decks=DeckDeclarationStore(state_path=service.decks.state_path),
        # A store that knows nothing — as after the legacy nickname-keyed
        # ghosts are dropped on load.
        tips=TipStateStore(state_path=service.tips.state_path.parent / "fresh.json"),
    )

    assert sorted(reborn.tips.racks()) == ["4", "7", "9"]
    assert reborn.tips.summary()["7"]["available"] == 96


def test_a_protocol_call_still_addresses_labware_by_nickname(service):
    # The robot is driven by nickname; only the tracker uses slots. Both must
    # hold at once, or the pick lands on the wrong labware.
    service.setup_protocol(RECIPE)

    _pick(service, well="A1")

    service.control.get_location_from_labware.assert_called_with("tips_300", "A1")
    assert service._mounted_tips["p300"]["rack"] == "4"


def test_refill_accepts_the_slot_or_the_legacy_nickname(service):
    service.setup_protocol(RECIPE)
    service.tips.set_status("4", "A1", "empty")

    service.reset_tip_rack("4")
    assert service.tips.status("4", "A1") == "new"

    service.tips.set_status("4", "B1", "empty")
    service.reset_tip_rack("tips_300")  # resolved through the session recipe
    assert service.tips.status("4", "B1") == "new"


def test_marking_columns_corrects_only_those_columns(service):
    """The case a whole-rack reset cannot express: some columns used, the rest
    full. Overstating it as a refill is what sends the head onto bare holes."""

    service.setup_protocol(RECIPE)
    service.mark_tips("4", status="empty", columns=[1, 2, 3, 10, 11])

    summary = service.tips.summary()["4"]
    assert summary["empty"] == 40
    assert summary["available"] == 56
    assert service.tips.status("4", "A1") == "empty"
    assert service.tips.status("4", "A4") == "new"

    # And back again, one column at a time.
    service.mark_tips("4", status="new", columns=[2])
    assert service.tips.status("4", "A2") == "new"
    assert service.tips.status("4", "A1") == "empty"
    assert service.tips.summary()["4"]["available"] == 64


def test_marking_accepts_the_slot_or_the_legacy_nickname(service):
    service.setup_protocol(RECIPE)

    service.mark_tips("tips_300", status="empty", columns=[1])
    assert service.tips.status("4", "A1") == "empty"


def test_marking_explicit_wells_leaves_the_rest_of_the_column(service):
    service.setup_protocol(RECIPE)

    service.mark_tips("4", status="empty", wells=["A1", "B1"])

    assert service.tips.status("4", "B1") == "empty"
    assert service.tips.status("4", "C1") == "new"


def test_marking_an_unknown_well_changes_nothing(service):
    """`set_statuses` validates the whole set first, so a typo in one column
    cannot leave the rack half-corrected."""

    service.setup_protocol(RECIPE)

    with pytest.raises(ValueError):
        service.mark_tips("4", status="empty", wells=["A1", "Z9"])

    assert service.tips.status("4", "A1") == "new"


def test_marking_an_untracked_slot_is_refused(service):
    with pytest.raises(LookupError):
        service.mark_tips("7", status="empty", columns=[1])


def test_allowed_actions_include_tips_mark(service):
    assert "tips.mark" in service.allowed_actions()


def test_api_tips_mark_partial_correction(tmp_path, monkeypatch):
    monkeypatch.setenv("OT2_TIP_STATE_PATH", str(tmp_path / "tips.json"))
    monkeypatch.setenv("OT2_PLATE_STATE_PATH", str(tmp_path / "plate.json"))
    monkeypatch.setenv("OT2_DECK_STATE_PATH", str(tmp_path / "deck.json"))
    app = create_app(dry_run=True, enforce_claims=False)
    client = TestClient(app)
    app.state.service.setup_protocol(RECIPE)

    marked = client.post(
        "/control/tips/mark", json={"slot": "4", "columns": [1, 12], "status": "empty"}
    )
    assert marked.status_code == 200
    tips = marked.json()["tips"]
    assert tips["A1"] == "empty" and tips["H12"] == "empty"
    assert tips["A2"] == "new"

    racks = client.get("/status").json()["details"]["tip_racks"]
    assert racks["4"]["available"] == 80

    # A slot with no tracked rack is a state problem, not a bad argument.
    assert (
        client.post(
            "/control/tips/mark", json={"slot": "7", "columns": [1], "status": "empty"}
        ).status_code
        == 409
    )
    # Neither wells nor columns marks nothing while reporting success — refuse.
    assert (
        client.post("/control/tips/mark", json={"slot": "4", "status": "new"}).status_code == 422
    )
    # "touched" is not assertable: it carries a sample id the gateway observed.
    assert (
        client.post(
            "/control/tips/mark", json={"slot": "4", "columns": [1], "status": "touched"}
        ).status_code
        == 422
    )


def test_api_tips_mark_repairs_a_single_well(tmp_path, monkeypatch):
    """The one-well repair, which is what a drifted tracker actually needs.

    Modelled on the real case: the gateway recorded A1 as the empty hole when
    the tip had really come from B1. Correcting that means marking two wells to
    *different* statuses — inexpressible in columns, so it forced a raw API call
    even though the endpoint had always accepted `wells`. The operator panel now
    sends this shape (`TipEditor`'s well mode), and nothing else covers it.
    """

    monkeypatch.setenv("OT2_TIP_STATE_PATH", str(tmp_path / "tips.json"))
    monkeypatch.setenv("OT2_PLATE_STATE_PATH", str(tmp_path / "plate.json"))
    monkeypatch.setenv("OT2_DECK_STATE_PATH", str(tmp_path / "deck.json"))
    app = create_app(dry_run=True, enforce_claims=False)
    client = TestClient(app)
    app.state.service.setup_protocol(RECIPE)
    app.state.service.tips.set_status("4", "A1", "empty")  # the wrong well

    assert (
        client.post(
            "/control/tips/mark", json={"slot": "4", "wells": ["A1"], "status": "new"}
        ).status_code
        == 200
    )
    marked = client.post(
        "/control/tips/mark", json={"slot": "4", "wells": ["B1"], "status": "empty"}
    )
    assert marked.status_code == 200

    rack = client.get("/status").json()["details"]["tip_racks"]["4"]
    assert rack["tips"] == {"B1": "empty"}   # the hole moved, it did not multiply
    assert rack["available"] == 95

    # Wells and columns together are ambiguous about precedence, not additive.
    assert (
        client.post(
            "/control/tips/mark",
            json={"slot": "4", "wells": ["A2"], "columns": [3], "status": "empty"},
        ).status_code
        == 422
    )
    # A well the rack does not have is a bad argument (422, per the endpoint's
    # own contract — 409 is reserved for the slot holding no tracked rack), and
    # the whole request is refused: set_statuses validates every well before
    # mutating any, so a typo cannot leave the rack half-corrected.
    assert (
        client.post(
            "/control/tips/mark",
            json={"slot": "4", "wells": ["A2", "Z9"], "status": "empty"},
        ).status_code
        == 422
    )
    assert client.get("/status").json()["details"]["tip_racks"]["4"]["tips"] == {"B1": "empty"}


def test_a_rack_cannot_be_registered_under_a_non_slot_key(service):
    # The loader drops non-slot keys, so accepting one on write would be a
    # silent write-then-lose across the next restart.
    with pytest.raises(ValueError, match="keyed by deck slot"):
        service.tips.register_rack("tips_300")


def test_legacy_nickname_keyed_racks_are_dropped_on_load(tmp_path):
    """Ghost entries from before slot keying: they tracked no deck slot, so
    nothing could join them — they showed in the panel while the racks actually
    on the deck showed nothing."""

    import json

    path = tmp_path / "tips.json"
    path.write_text(
        json.dumps(
            {
                "racks": {
                    "tips_20": {
                        "nickname": "tips_20",
                        "tips": {"A1": "new"},
                        "registered_at": "2026-08-05T00:00:00+00:00",
                    },
                    "4": {
                        "nickname": "4",
                        "tips": {"A1": "empty"},
                        "registered_at": "2026-08-05T00:00:00+00:00",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    store = TipStateStore(state_path=path)

    assert sorted(store.racks()) == ["4"]
    assert store.status("4", "A1") == "empty"  # the real one is untouched


# ---------------------------------------------------------------------------
# Interrupted runs: the tip is on the head and the run stopped there
#
# The reported failure. A protocol picked a tip, a later step raised, and the
# tip could not be put back: the rack still showed its well as holding a tip,
# so the return was refused as "would drop onto a seated tip". Marking the well
# at pick time is what fixes it — the well is a hole from the moment the tip
# leaves, whether or not the run ever reaches a drop.
# ---------------------------------------------------------------------------


def test_return_after_a_failed_step_is_allowed(service):
    service.setup_protocol(RECIPE)
    _pick(service)

    # A later step blows up (e.g. a move to an out-of-range height).
    service.control.move_to_pip.side_effect = RuntimeError("out of range")
    with pytest.raises(RuntimeError):
        service.move_to(
            MoveToRequest(
                pipette="p300",
                location=WellLocation(labware_nickname="plate_D", position="A1"),
            )
        )
    service.control.move_to_pip.side_effect = None

    # The tip goes back where it came from, no operator correction needed.
    service.drop_tip(
        TipRequest(pipette="p300", labware_nickname="tips_300", position="A1")
    )
    assert service.tips.status("4", "A1") == "new"
    assert "p300" not in service._mounted_tips


def test_mount_survives_a_gateway_restart(tmp_path):
    """A tip stays on the head across a restart, so the record of it must too."""

    def build() -> OT2Service:
        svc = OT2Service(
            dry_run=False,
            plates=PlateStateStore(state_path=tmp_path / "plate.json"),
            decks=DeckDeclarationStore(state_path=tmp_path / "deck.json"),
            tips=TipStateStore(state_path=tmp_path / "tips.json"),
        )
        svc.control = Mock()
        svc.refresh_snapshot = Mock(return_value={})
        svc.state = OT2ServiceState.READY
        return svc

    first = build()
    first.setup_protocol(RECIPE)
    _pick(first)
    _move(first, "aspirate", "reservoir", "A1")

    restarted = build()
    restarted.setup_protocol(RECIPE)  # re-setup, as on any restart

    mounted = restarted._mounted_tips["p300"]
    assert (mounted["rack"], mounted["well"]) == ("4", "A1")
    assert mounted["last_sample"] == "reservoir_A1"
    assert mounted["contacted_liquid"] is True
    assert restarted.tips.status("4", "A1") == "on_pipette"

    # And the tip can still be returned, with its exposure intact.
    restarted.drop_tip(
        TipRequest(pipette="p300", labware_nickname="tips_300", position="A1")
    )
    assert restarted.tips.status("4", "A1") == "reservoir_A1"


def test_failed_pick_rolls_the_well_back(service):
    """A pick that definitely did not happen must not consume the tip."""

    service.setup_protocol(RECIPE)
    service.tips.set_status("4", "A1", "sample_X")
    service.control.pick_up_tip.side_effect = RuntimeError("no tip detected")

    with pytest.raises(RuntimeError):
        _pick(service, well="A1", force=True)

    # Restored to what it was, not flattened to "new".
    assert service.tips.status("4", "A1") == "sample_X"
    assert "p300" not in service._mounted_tips


def test_pick_with_an_unknown_outcome_keeps_the_tip_reserved(service):
    """The unsafe direction is assuming the tip is still in the rack: the next
    auto-pick would send the head back onto that well."""

    service.setup_protocol(RECIPE)
    service.control.pick_up_tip.side_effect = OSError("connection reset")

    with pytest.raises(UnknownOutcomeError):
        _pick(service, well="A1")

    assert service.tips.status("4", "A1") == "on_pipette"
    assert service._mounted_tips["p300"]["uncertain"] is True
    # ...so the next pick goes elsewhere.
    service.control.pick_up_tip.side_effect = None
    service.state = OT2ServiceState.READY
    _pick(service)
    assert service._mounted_tips["p300"]["well"] == "B1"


def test_tips_mark_releases_a_stale_mount(service):
    """The recovery path when the gateway's belief and the bench disagree."""

    service.setup_protocol(RECIPE)
    _pick(service)
    assert "p300" in service._mounted_tips

    # The operator looks at the rack and says: A1 has a tip in it.
    service.mark_tips("4", status="new", wells=["A1"])

    assert service.tips.status("4", "A1") == "new"
    assert "p300" not in service._mounted_tips  # no claim on a well that is full


def test_rack_reset_releases_mounts_from_that_rack(service):
    service.setup_protocol(RECIPE)
    _pick(service)

    service.reset_tip_rack("4")  # a fresh rack was physically swapped in

    assert service.tips.summary()["4"]["available"] == 96
    # The old tip may still be on the head, but it has no origin to return to.
    assert "p300" not in service._mounted_tips
