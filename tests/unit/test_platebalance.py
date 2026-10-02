from unittest.mock import Mock, call

import pytest
from fastapi.testclient import TestClient

from opentrons_server.gateway.platebalance import BalanceNoFrame, BalancePipettingGeometry, BalanceReferenceTimeout, BalanceStabilityTimeout, PlateBalanceConfig, PlateBalanceRequest, PlateBalanceV1, parse_weight
from opentrons_server.gateway.advanced import BlowOutRequest
from opentrons_server.gateway.limits import OutOfEnvelope
from opentrons_server.gateway.models import DispenseRequest, MoveToRequest, WellLocation
from opentrons_server.gateway.service import OT2Service, OT2ServiceState, UnknownOutcomeError
from opentrons_server.gateway.api import create_app
from opentrons_server.gateway.deck import DeckDeclarationStore
from opentrons_server.gateway.tip_state import TipStateStore
from opentrons_server.gateway.plate_state import PlateStateStore


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def make_service(tmp_path, **kwargs):
    driver = Mock()
    driver._weigh.return_value = (True, -1.2345)
    factory = Mock(return_value=driver)
    balance = PlateBalanceV1(PlateBalanceConfig(com_port="COM3"), driver_factory=factory)
    service = OT2Service(platebalance=balance,
        decks=DeckDeclarationStore(state_path=tmp_path / "deck.json"),
        tips=TipStateStore(state_path=tmp_path / "tips.json"),
        plates=PlateStateStore(state_path=tmp_path / "plate.json"), **kwargs)
    service.state = OT2ServiceState.DRY_RUN if service.dry_run else OT2ServiceState.READY
    service.refresh_snapshot = Mock()
    return service, driver, factory


def test_status_never_opens_port_and_module_is_local(tmp_path):
    service, driver, factory = make_service(tmp_path)
    status = service.get_status()
    assert status.details["platebalance"]["reading"] is None
    assert service._build_deck_state().slots["9"].module.local_peripheral
    factory.assert_not_called()
    driver.assert_not_called()


def test_read_and_distinct_reference_operations(tmp_path):
    service, driver, _ = make_service(tmp_path)
    result = service.platebalance_action("read")
    assert result["reading"]["value"] == -1.2345
    assert result["reading"]["stable"] is True
    driver._weigh.side_effect = [(False, 0.01), (True, 0.0001), (True, 0.0)]
    result = service.platebalance_action("tare")
    assert result["reading"]["stable"] is True
    assert result["reading"]["value"] == 0.0
    assert result["last_operation"]["outcome"] == "baseline_observed"
    assert driver._weigh.call_count == 4
    result = service.platebalance_action("zero")
    assert result["reading"] is None
    assert result["last_operation"]["outcome"] == "sent_unconfirmed"
    assert driver.set_reference.call_count == 2


@pytest.mark.parametrize("action", ["tare", "zero"])
def test_unknown_outcome_blocks_repeat(action, tmp_path):
    service, driver, _ = make_service(tmp_path)
    driver.set_reference.side_effect = RuntimeError("lost acknowledgment")
    with pytest.raises(UnknownOutcomeError):
        service.platebalance_action(action)
    assert service.state == OT2ServiceState.UNKNOWN_OUTCOME
    with pytest.raises(RuntimeError):
        service.platebalance_action(action)
    driver.set_reference.assert_called_once_with(action)


@pytest.mark.parametrize("state", [OT2ServiceState.BUSY, OT2ServiceState.ERROR,
    OT2ServiceState.UNKNOWN_OUTCOME, OT2ServiceState.EXTERNAL_CONTROL, OT2ServiceState.PAUSED])
def test_state_gate(state, tmp_path):
    service, _, factory = make_service(tmp_path)
    service.state = state
    with pytest.raises(RuntimeError):
        service.platebalance_action("read")
    factory.assert_not_called()


@pytest.mark.parametrize("mode", ["dry_run", "simulation"])
def test_simulation_never_opens_port_and_stop_still_blocks(mode, tmp_path):
    service, _, factory = make_service(tmp_path, **{mode: True})
    assert service.platebalance_action("zero")["simulation"]
    service._stop_latched = True
    with pytest.raises(RuntimeError):
        service.platebalance_action("read")
    factory.assert_not_called()


def test_claim_gate(tmp_path):
    service, _, factory = make_service(tmp_path)
    with TestClient(create_app(dry_run=True, auto_reconnect=False, enforce_claims=True)) as client:
        assert client.post("/control/platebalance/read").status_code == 423
    factory.assert_not_called()


def test_reserved_slot_and_native_module_rejected(tmp_path):
    service, _, _ = make_service(tmp_path)
    with pytest.raises(ValueError, match="reserved"):
        service.declare_deck({"9": "plate_96"})
    with pytest.raises(ValueError, match="local peripheral"):
        service.setup_protocol({"modules": [{"name": "platebalanceV1", "location": "9"}]})
    with pytest.raises(ValueError, match="reserved"):
        service.setup_protocol({"labware": [{"location": "9"}]})


def test_existing_occupant_is_preserved_and_actions_withheld(tmp_path):
    service, _, factory = make_service(tmp_path)
    service.decks.declare({"9": "plate_96"})
    assert service._build_deck_state().slots["9"].labware is not None
    assert "platebalance.read" not in service.allowed_actions()
    with pytest.raises(ValueError, match="conflicts"):
        service.platebalance_action("read")
    factory.assert_not_called()


@pytest.mark.parametrize("frame,value,stable", [
    ("+     12.3456 g  \r\n", 12.3456, True),
    ("-      0.1234    \r\n", -0.1234, False),
    ("N     +     12.3456 g  \r\n", 12.3456, True),
])
def test_parse_weight(frame, value, stable):
    assert parse_weight(frame) == (stable, value)


@pytest.mark.parametrize("frame", ["", "Err 08\r\n", "     H\r\n", "+ 1.000 mg\r\n", "+ 1.000 g", "garbage 1.000 g\r\n"])
def test_invalid_reading_is_not_measurement(frame):
    with pytest.raises(ValueError):
        parse_weight(frame)


def test_driver_wire_commands_use_distinct_tare_and_zero_without_open_on_init(monkeypatch):
    pytest.importorskip("matterlab_balances")
    from opentrons_server.gateway.platebalance import matterlab_driver
    monkeypatch.setattr("opentrons_server.gateway.platebalance.time.sleep", lambda _: None)
    monkeypatch.setattr("matterlab_serial_device.serial_device.time.sleep", lambda _: None)
    driver = matterlab_driver(PlateBalanceConfig(com_port="COM3", balance_blow_out_enabled=True))
    assert driver.device is None
    transport = Mock()
    transport.read_until.return_value = b"+     12.3456 g  \r\n"
    driver.device = transport
    assert driver._weigh() == (True, 12.3456)
    transport.read_until.return_value = b""
    with pytest.raises(BalanceNoFrame, match="no weight frame"):
        driver._weigh()
    driver.set_reference("tare")
    driver.set_reference("zero")
    assert [call.args[0] for call in transport.write.call_args_list] == [
        b"\x1bP\r\n", b"\x1bP\r\n", b"\x1bU\r\n", b"\x1bV\r\n"]
    assert transport.close.call_count >= 3


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def balance_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("opentrons_server.gateway.platebalance.time", clock)
    return clock


def test_wait_returns_only_stable_sample_and_no_reference_commands(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    driver._weigh.side_effect = [(False, 1.0), (False, 1.1), (True, 1.2)]
    result = service.platebalance_action("read", PlateBalanceRequest(wait_until_stable=True))
    assert result["reading"]["value"] == 1.2
    assert result["reading"]["stable"] is True
    assert result["last_operation"]["outcome"] == "observed"
    assert driver._weigh.call_count == 3
    driver.set_reference.assert_not_called()
    assert service.state == OT2ServiceState.READY


def test_wait_holds_command_lock_and_status_does_not_read_serial(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    def sample(**kwargs):
        assert service._command_lock.locked()
        assert service.get_status().activity == "running"
        with pytest.raises(RuntimeError, match="in flight"):
            service._run_action("home", lambda: pytest.fail("must not execute"), idempotent=False)
        return True, 1.0
    driver._weigh.side_effect = sample
    service.platebalance_action("read", PlateBalanceRequest(wait_until_stable=True))
    assert driver._weigh.call_count == 1


def test_stability_timeout_preserves_unstable_sample_and_releases_lock(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    driver._weigh.return_value = (False, 1.2)
    with pytest.raises(BalanceStabilityTimeout, match="within 1 s"):
        service.platebalance_action("read", PlateBalanceRequest(wait_until_stable=True, timeout_s=1))
    snapshot = service.platebalance.snapshot()
    assert snapshot["last_operation"]["outcome"] == "stability_timeout"
    assert snapshot["reading"]["stable"] is False
    assert service.state == OT2ServiceState.READY
    assert not service._command_lock.locked()
    assert driver._weigh.call_count == 4
    driver._weigh.return_value = (True, 1.3)
    assert service.platebalance_action("read")["last_error"] is None


def test_no_stable_result_accepted_after_deadline(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    def late_sample(**kwargs):
        balance_clock.sleep(2)
        return True, 1.0
    driver._weigh.side_effect = late_sample
    with pytest.raises(BalanceStabilityTimeout):
        service.platebalance_action("read", PlateBalanceRequest(wait_until_stable=True, timeout_s=1))
    assert service.platebalance.snapshot()["last_operation"]["outcome"] == "stability_timeout"


def test_read_failure_is_not_retried_while_waiting(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    driver._weigh.side_effect = OSError("serial disconnected")
    with pytest.raises(OSError, match="disconnected"):
        service.platebalance_action("read", PlateBalanceRequest(wait_until_stable=True))
    assert driver._weigh.call_count == 1
    assert service.state == OT2ServiceState.ERROR


def test_stop_cancels_wait_without_more_reads(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    def interrupted_sample(**kwargs):
        service._stop_latched = True
        service.state = OT2ServiceState.UNKNOWN_OUTCOME
        return False, 1.0
    driver._weigh.side_effect = interrupted_sample
    with pytest.raises(UnknownOutcomeError, match="interrupted"):
        service.platebalance_action("read", PlateBalanceRequest(wait_until_stable=True))
    assert driver._weigh.call_count == 1
    assert service._stop_latched
    assert service.state == OT2ServiceState.UNKNOWN_OUTCOME
    assert not service._command_lock.locked()


def test_slot_can_be_configured_without_passing_geometry_to_serial_driver(tmp_path):
    service, _, factory = make_service(tmp_path)
    service.platebalance = PlateBalanceV1(PlateBalanceConfig(com_port="COM3", slot="8",
        adapter_height_mm=20, max_labware_height_mm=20), driver_factory=factory)
    deck = service._build_deck_state()
    assert deck.slots["8"].module.local_peripheral
    assert deck.slots["9"].module is None
    assert deck.slots["6"].module is None
    assert service.platebalance.snapshot()["geometry"]["pipetting_enabled"] is False
    with pytest.raises(ValueError, match="Slot 8"):
        service.setup_protocol({"labware": [{"location": "8"}]})
    factory.assert_not_called()


@pytest.mark.parametrize("body", [{"timeout_s": 0}, {"timeout_s": 31}, {"timeout_s": float("nan")}, {"stable": True}])
def test_bad_wait_options_rejected(body):
    with pytest.raises(ValueError):
        PlateBalanceRequest(**body)


def test_zero_does_not_accept_stability_wait(tmp_path):
    service, _, factory = make_service(tmp_path)
    with pytest.raises(ValueError, match="only to read or tare"):
        service.platebalance_action("zero", PlateBalanceRequest(wait_until_stable=True))
    factory.assert_not_called()


def test_tare_timeout_halts_before_dosing_and_preserves_last_reading(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    driver._weigh.return_value = (True, 1.0)
    with pytest.raises(BalanceReferenceTimeout, match="stable zero was not observed.*after 3 tare attempts"):
        service.platebalance_action("tare", PlateBalanceRequest(timeout_s=1))
    assert driver.set_reference.call_count == 3  # default attempts, each with its own wait
    assert driver._weigh.call_count > 3
    assert service.platebalance.snapshot()["last_operation"]["outcome"] == "baseline_unconfirmed"
    assert service.platebalance.snapshot()["last_operation"]["attempts"] == 3
    assert service.platebalance.snapshot()["reading"]["value"] == 1.0
    assert service.state == OT2ServiceState.READY


def test_tare_waits_through_empty_weight_frames_without_resending(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    driver._weigh.side_effect = [BalanceNoFrame("Balance returned no weight frame"),
                                 (False, 0.01), BalanceNoFrame("Balance returned no weight frame"),
                                 (True, 0.0), (True, 0.0001)]
    result = service.platebalance_action("tare", PlateBalanceRequest(timeout_s=3))
    assert result["last_operation"]["outcome"] == "baseline_observed"
    assert result["reading"]["value"] == 0.0001
    assert driver._weigh.call_count == 5
    driver.set_reference.assert_called_once_with("tare")
    assert service.state == OT2ServiceState.READY


def test_tare_waits_through_an_unreadable_frame_without_resending(tmp_path, balance_clock):
    """Live halt on ot2_complexation 2026-10-02: a frame cut off at the read
    timeout ('-   0.0005 g  ', no CR/LF) during the baseline wait escalated to
    unknown_outcome and required manual reconciliation. The scale answered, so
    the tare was delivered; an unreadable frame is retried like silence."""
    service, driver, _ = make_service(tmp_path)
    driver._weigh.side_effect = [(True, -0.0005),
                                 ValueError("Invalid balance weight frame: '-   0.0005 g  '"),
                                 (True, 0.0), (True, -0.0001)]
    result = service.platebalance_action("tare", PlateBalanceRequest(timeout_s=3))
    assert result["last_operation"]["outcome"] == "baseline_observed"
    assert driver._weigh.call_count == 4
    driver.set_reference.assert_called_once_with("tare")
    assert service.state == OT2ServiceState.READY


def test_tare_with_only_unreadable_frames_is_unconfirmed_not_unknown(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    driver._weigh.side_effect = ValueError("Invalid balance weight frame: 'Err 08\\r\\n'")
    with pytest.raises(BalanceReferenceTimeout, match="unreadable: Invalid balance weight frame"):
        service.platebalance_action("tare", PlateBalanceRequest(timeout_s=1, attempts=1))
    driver.set_reference.assert_called_once_with("tare")
    assert driver._weigh.call_count > 1
    assert service.platebalance.snapshot()["last_operation"]["outcome"] == "baseline_unconfirmed"
    assert service.state == OT2ServiceState.READY  # a plan halts; the gateway is not wrecked
    assert service.last_error is None


def test_stable_read_waits_through_an_unreadable_frame_but_a_single_read_does_not(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    driver._weigh.side_effect = [ValueError("Invalid balance weight frame: '+ 1.0'"), (True, 1.0)]
    result = service.platebalance_action("read", PlateBalanceRequest(wait_until_stable=True, timeout_s=2))
    assert result["reading"]["value"] == 1.0
    driver._weigh.side_effect = ValueError("Invalid balance weight frame: 'H'")
    with pytest.raises(ValueError, match="Invalid balance weight frame"):
        service.platebalance_action("read", PlateBalanceRequest(wait_until_stable=False))


def test_driver_never_shortens_the_serial_timeout_below_a_whole_frame(monkeypatch):
    pytest.importorskip("matterlab_balances")
    from opentrons_server.gateway.platebalance import matterlab_driver
    monkeypatch.setattr("matterlab_serial_device.serial_device.time.sleep", lambda _: None)
    driver = matterlab_driver(PlateBalanceConfig(com_port="COM3", timeout=1.0))
    transport = Mock()
    transport.timeout = 1.0
    transport.write_timeout = 1.0
    seen = []
    def read_until(**_kwargs):
        seen.append(transport.timeout)
        return b"+     0.0001 g  \r\n"
    transport.read_until.side_effect = read_until
    driver.device = transport
    driver._weigh(timeout_s=0.02)   # 20 ms left on the deadline: still a whole-frame read
    driver._weigh(timeout_s=0.8)
    driver._weigh(timeout_s=5.0)    # never above the configured timeout
    assert seen == [0.5, 0.8, 1.0]
    assert transport.timeout == 1.0  # restored


def test_tare_is_resent_when_the_baseline_settles_off_zero(tmp_path, balance_clock):
    """The live case: the balance tared before the pan was still and then read a
    stable -0.0005 g for the whole wait. A second tare on the settled pan
    re-zeroes it. Attempts are counted in the operation record."""
    service, driver, _ = make_service(tmp_path)
    readings = iter([(True, -0.0005)] * 12 + [(True, 0.0), (True, 0.0001)])
    driver._weigh.side_effect = lambda **_k: next(readings)
    result = service.platebalance_action("tare", PlateBalanceRequest(timeout_s=2))
    assert result["last_operation"]["outcome"] == "baseline_observed"
    assert result["last_operation"]["attempts"] == 2
    assert driver.set_reference.call_args_list == [call("tare"), call("tare")]
    assert service.state == OT2ServiceState.READY


def test_tare_attempts_apply_only_to_tare_and_are_capped(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    with pytest.raises(ValueError, match="attempts applies only to tare"):
        service.platebalance_action("read", PlateBalanceRequest(attempts=2))
    with pytest.raises(ValueError):
        PlateBalanceRequest(attempts=4)
    driver._weigh.return_value = (True, 0.0)
    result = service.platebalance_action("zero")
    assert result["last_operation"] == {**result["last_operation"], "outcome": "sent_unconfirmed", "attempts": 1}


def test_tare_with_only_empty_frames_latches_unknown_outcome(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    driver._weigh.side_effect = BalanceNoFrame("Balance returned no weight frame")
    with pytest.raises(UnknownOutcomeError, match="no frame"):
        service.platebalance_action("tare", PlateBalanceRequest(timeout_s=1))
    driver.set_reference.assert_called_once_with("tare")
    assert driver._weigh.call_count > 1
    assert service.platebalance.snapshot()["last_operation"]["outcome"] == "unknown_outcome"
    assert service.state == OT2ServiceState.UNKNOWN_OUTCOME


def test_tare_holds_command_lock_until_stable_baseline(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    def assert_stationary(**kwargs):
        assert service._command_lock.locked()
        assert service.get_status().activity == "running"
        with pytest.raises(RuntimeError, match="in flight"):
            service._run_action("move_to", lambda: pytest.fail("robot must not move"), idempotent=True)
        return True, 0.0
    driver._weigh.side_effect = assert_stationary
    result = service.platebalance_action("tare")
    assert result["last_operation"]["outcome"] == "baseline_observed"
    assert driver._weigh.call_count == 2
    assert service.state == OT2ServiceState.READY


def test_tare_does_not_accept_zero_after_deadline(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    def late_zero(**kwargs):
        balance_clock.sleep(2)
        return True, 0.0
    driver._weigh.side_effect = late_zero
    with pytest.raises(BalanceReferenceTimeout):
        service.platebalance_action("tare", PlateBalanceRequest(timeout_s=1))
    assert driver.set_reference.call_count == 3  # every attempt refused the late zero
    assert service.platebalance.snapshot()["last_operation"]["outcome"] == "baseline_unconfirmed"


def test_stop_during_tare_verification_never_repeats_reference(tmp_path, balance_clock):
    service, driver, _ = make_service(tmp_path)
    def interrupted_zero(**kwargs):
        service._stop_latched = True
        service.state = OT2ServiceState.UNKNOWN_OUTCOME
        return True, 0.0
    driver._weigh.side_effect = interrupted_zero
    with pytest.raises(UnknownOutcomeError, match="interrupted"):
        service.platebalance_action("tare")
    driver.set_reference.assert_called_once_with("tare")
    assert driver._weigh.call_count == 1
    assert service.state == OT2ServiceState.UNKNOWN_OUTCOME


def test_api_read_options_and_validation(tmp_path):
    app = create_app(dry_run=True, auto_reconnect=False, enforce_claims=False)
    app.state.service.platebalance_action = Mock(return_value={"ok": True})
    with TestClient(app) as client:
        assert client.post("/control/platebalance/read", json={"wait_until_stable": True, "timeout_s": 8}).status_code == 200
        app.state.service.platebalance_action.assert_called_once_with("read", PlateBalanceRequest(wait_until_stable=True, timeout_s=8))
        assert client.post("/control/platebalance/read", json={"timeout_s": 31}).status_code == 422
        assert client.post("/control/platebalance/read").status_code == 200
        app.state.service.platebalance_action.side_effect = BalanceStabilityTimeout("Weight did not stabilize within 10 s")
        response = client.post("/control/platebalance/read", json={"wait_until_stable": True})
        assert response.status_code == 409
        assert response.json()["detail"] == "Weight did not stabilize within 10 s"


def balance_plate(height=14.2):
    return {"load_name": "custom_test_plate", "support_module": "platebalanceV1",
            "plate_id": "collection-1", "definition": {
                "schemaVersion": 2, "parameters": {"loadName": "custom_test_plate", "isTiprack": False},
                "metadata": {"displayCategory": "wellPlate", "displayName": "Test plate"},
                "dimensions": {"xDimension": 127.76, "yDimension": 85.48, "zDimension": height},
                "ordering": [["A1", "B1"]],
                "wells": {n: {"x": 10, "y": 10, "z": 1, "depth": 5} for n in ("A1", "B1")}}}


@pytest.mark.parametrize("height", [25, 25.001, 0, -1, float("nan"), float("inf"), None])
def test_balance_plate_rejects_invalid_height(tmp_path, height):
    service, _, factory = make_service(tmp_path)
    with pytest.raises(ValueError):
        service.declare_deck({"9": balance_plate(height)})
    assert not service.decks.get()
    factory.assert_not_called()


@pytest.mark.parametrize("category,tiprack", [("tipRack", True), ("reservoir", False), ("adapter", False)])
def test_balance_plate_rejects_nonplates(tmp_path, category, tiprack):
    service, _, _ = make_service(tmp_path)
    plate = balance_plate()
    plate["definition"]["metadata"]["displayCategory"] = category
    plate["definition"]["parameters"]["isTiprack"] = tiprack
    with pytest.raises(ValueError, match="well plates only"):
        service.declare_deck({"9": plate})


def test_operator_can_load_weigh_and_clear_plate_without_moving_balance(tmp_path):
    from opentrons_server.gateway.module_access import module_admin
    service, driver, factory = make_service(tmp_path)
    token = module_admin.set(False)
    try:
        service.declare_deck({"9": balance_plate(24.999)})
        slot = service._build_deck_state().slots["9"]
        assert slot.module.module_name == "platebalanceV1"
        assert slot.labware.plate_id == "collection-1"
        assert slot.labware.rows == 2
        assert slot.labware.support_module == "platebalanceV1"
        factory.assert_not_called()
        assert "platebalance.read" in service.allowed_actions()
        assert service.platebalance_action("read")["reading"]["stable"]
        driver._weigh.assert_called_once()
        saved = DeckDeclarationStore(state_path=tmp_path / "deck.json").get()["9"]
        assert saved == service.decks.get()["9"]
        service.declare_deck({"9": saved.model_dump(), "1": "plate_96"})
        assert service.decks.get()["9"].plate_id == "collection-1"
        service.declare_deck({})
        assert service._build_deck_state().slots["9"].module.local_peripheral
        assert service._build_deck_state().slots["9"].labware is None
    finally:
        module_admin.reset(token)


@pytest.mark.parametrize("ref", ["9", "slot_9", "collection-1", "cached_plate"])
def test_balance_plate_cannot_be_autoloaded_or_pipetted(tmp_path, ref):
    service, _, factory = make_service(tmp_path)
    service.declare_deck({"9": balance_plate()})
    service._session_labware["9"] = "cached_plate"
    with pytest.raises(ValueError, match="geometry is not calibrated"):
        service._resolve_session_labware(ref)
    factory.assert_not_called()


def test_balance_plate_must_use_configured_slot(tmp_path):
    service, _, _ = make_service(tmp_path)
    with pytest.raises(ValueError, match="configured balance slot"):
        service.declare_deck({"8": balance_plate()})
    service.platebalance = None
    with pytest.raises(ValueError, match="configured balance slot"):
        service.declare_deck({"9": balance_plate()})


def test_balance_plate_config_can_only_lower_height_limit(tmp_path):
    service, _, _ = make_service(tmp_path)
    service.platebalance.config.max_labware_height_mm = 15
    with pytest.raises(ValueError, match="below 15"):
        service.declare_deck({"9": balance_plate(15)})
    with pytest.raises(ValueError):
        PlateBalanceConfig(com_port="COM3", max_labware_height_mm=26)


def test_standard_balance_plate_definition_resolved(tmp_path):
    service, _, _ = make_service(tmp_path)
    service.declare_deck({"9": {"load_name": "corning_96_wellplate_360ul_flat", "support_module": "platebalanceV1"}})
    assert service.decks.get()["9"].definition["dimensions"]["zDimension"] < 25


def qualified_balance(tmp_path):
    service, _, _ = make_service(tmp_path)
    plate = balance_plate(19)
    definition = plate["definition"]
    definition.update({
        "namespace": "custom", "version": 1,
        "cornerOffsetFromSlot": {"x": 0, "y": 0, "z": 0},
        "brand": {"brand": "Test"}, "groups": [],
    })
    definition["wells"]["A1"].update(x=13.88, y=74.26, z=6.4, depth=12.6)
    definition["wells"]["B1"].update(x=13.88, y=65.26, z=6.4, depth=12.6)
    for well in definition["wells"].values():
        well.update(shape="rectangular", xDimension=7.4, yDimension=7.4, totalLiquidVolume=700)
    geometry = BalancePipettingGeometry(
        load_name="custom_test_plate", definition_sha256=BalancePipettingGeometry.digest(definition),
        xy_alignment="slot", seating_height_mm=102, rim_height_mm=121,
    )
    service.platebalance.config.pipetting_geometry = geometry
    service._last_probe = {"instruments": [{"mount": "right", "name": "p300_single_gen2"}]}
    service.declare_deck({"9": plate})
    return service, geometry, definition


def test_qualified_geometry_compiles_raised_plate_without_changing_source(tmp_path):
    service, geometry, definition = qualified_balance(tmp_path)
    compiled = geometry.compile_definition(definition)
    assert compiled["wells"]["A1"]["z"] == 108.4
    assert compiled["wells"]["A1"]["z"] + compiled["wells"]["A1"]["depth"] == 121
    assert compiled["dimensions"]["zDimension"] == 121
    assert definition["wells"]["A1"]["z"] == 6.4
    assert service.platebalance.snapshot()["geometry"]["pipetting_enabled"] is True
    assert service.platebalance.snapshot()["geometry"]["qualified_plate"]["rim_height_mm"] == 121
    assert geometry.model_copy(update={"seating_height_mm": 103}).compiled_load_name() != compiled["parameters"]["loadName"]
    definition["wells"]["A1"]["x"] += 1
    with pytest.raises(ValueError, match="definition differs"):
        geometry.compile_definition(definition)


def test_balance_move_requires_explicit_clearance_and_arc(tmp_path):
    service, _, _ = qualified_balance(tmp_path)
    service.control = Mock()
    service._ensure_session_pipette = Mock(return_value="right")
    service._run_action = Mock(side_effect=lambda _name, execute, **_kw: execute())
    location = WellLocation(labware_nickname="9", position="A1", top=20)
    service.move_to(MoveToRequest(pipette="right", location=location))
    assert service.control.load_labware.call_args.args[0]["config"]["dimensions"]["zDimension"] == 121
    assert service.control.move_to_pip.call_args.kwargs["minimum_z_height"] == 123
    assert service.control.move_to_pip.call_args.kwargs["force_direct"] is None
    for unsafe in (WellLocation(labware_nickname="9", position="A1"),
                   WellLocation(labware_nickname="9", position="A1", top=1),
                   WellLocation(labware_nickname="9", position="A1", bottom=15)):
        with pytest.raises(ValueError):
            service.move_to(MoveToRequest(pipette="right", location=unsafe))
    with pytest.raises(ValueError, match="Direct moves"):
        service.move_to(MoveToRequest(pipette="right", location=location, force_direct=True))
    service._last_probe["instruments"][0]["name"] = "p300_multi_gen2"
    with pytest.raises(ValueError, match="single-channel"):
        service.move_to(MoveToRequest(pipette="right", location=location))


def test_compiled_balance_run_is_reconciled_without_hiding_observed_name(tmp_path):
    service, geometry, _ = qualified_balance(tmp_path)
    compiled_name = geometry.compiled_load_name()
    service._session_labware["9"] = "slot_9"
    service._session_balance_load_name = compiled_name
    service._last_run_labware = {"labware": [{"loadName": compiled_name,
                                             "location": {"slotName": "9"}}]}
    observed = service._build_deck_state().slots["9"]
    assert observed.source == "run"
    assert observed.labware.load_name == compiled_name
    assert observed.slot_state == "occupied"
    assert observed.module.local_peripheral
    assert service._balance_placement_valid()
    service._session_balance_load_name = "another_definition"
    assert service._build_deck_state().slots["9"].slot_state == "mismatch"


@pytest.mark.parametrize("transport,expected_key,expected_value", [
    ("ssh", "rate", 0.5), ("http", "flow_rate", 46.43),
])
def test_balance_dispense_defaults_to_two_mm_and_half_rate(tmp_path, transport, expected_key, expected_value):
    service, _, _ = qualified_balance(tmp_path)
    service.transport = transport
    service.control = Mock()
    service.control.get_flow_rate.return_value = {"dispense": 92.86}
    service._last_probe = {"instruments": [{"mount": "right", "name": "p300_single_gen2"}]}
    service._ensure_session_pipette = Mock(return_value="right")
    service._run_action = Mock(side_effect=lambda _name, execute, **_kw: execute())
    service._mark_tip_used = Mock()
    request = DispenseRequest(pipette="right", volume_ul=10,
                              location=WellLocation(labware_nickname="collection-1", position="A1"))
    service.dispense(request)
    assert service.control.get_location_from_labware.call_args.kwargs["default_offset"] == 2
    assert service.control.move_to_pip.call_args.kwargs["minimum_z_height"] == 123
    assert service.control.dispense.call_args.kwargs[expected_key] == pytest.approx(expected_value)
    calls = [entry[0] for entry in service.control.mock_calls]
    assert calls.index("move_to_pip") < calls.index("dispense")
    with pytest.raises(ValueError, match="at most"):
        service.dispense(request.model_copy(update={"flow_rate": 47}))
    assert service.state == OT2ServiceState.READY
    assert service._run_action.call_count == 1
    with pytest.raises(ValueError, match="at least"):
        service.dispense(request.model_copy(update={"location": WellLocation(
            labware_nickname="9", position="A1", top=1)}))


def test_balance_blow_out_requires_opt_in_clearance_and_capped_flow(tmp_path):
    service, _, _ = qualified_balance(tmp_path)
    service.control = Mock()
    service.control.get_flow_rate.return_value = {"blow_out": 40.0}
    service._ensure_session_pipette = Mock(return_value="right")
    service._mark_tip_used = Mock()
    request = BlowOutRequest(pipette="right", location=WellLocation(
        labware_nickname="9", position="A1"))

    with pytest.raises(ValueError, match="operator-qualified"):
        service.advanced_action("blow_out", request)
    service.control.blow_out.assert_not_called()
    assert service.state == OT2ServiceState.READY

    service.platebalance.config.balance_blow_out_enabled = True
    service.advanced_action("blow_out", request)
    assert service.control.get_location_from_labware.call_args.kwargs["default_offset"] == 2
    assert service.control.move_to_pip.call_args.kwargs["minimum_z_height"] == 123
    calls = [entry[0] for entry in service.control.mock_calls]
    assert calls.index("get_location_from_labware") < calls.index("move_to_pip") < calls.index("blow_out")
    service.control.blow_out.assert_called_once_with("right")
    service._mark_tip_used.assert_called_once_with("right", "9", "A1")

    service.control.reset_mock()
    service.control.get_flow_rate.return_value = {"blow_out": 47.0}
    with pytest.raises(OutOfEnvelope, match="at most"):
        service.advanced_action("blow_out", request)
    assert service.state == OT2ServiceState.READY
    service.control.get_location_from_labware.assert_not_called()
    service.control.move_to_pip.assert_not_called()
    service.control.blow_out.assert_not_called()

    with pytest.raises(ValueError, match="at least"):
        service.advanced_action("blow_out", request.model_copy(update={"location": WellLocation(
            labware_nickname="9", position="A1", top=0)}))
    service.control.blow_out.assert_not_called()
