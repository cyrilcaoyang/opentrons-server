"""platebalanceV1 through a weigh-every-plate service: same rules, HTTP transport."""

from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from opentrons_server.gateway.platebalance import (BalanceNoFrame, BalanceReferenceTimeout, PlateBalanceConfig,
                                                   PlateBalanceRequest, PlateBalanceV1, matterlab_driver,
                                                   weigher_http_driver)
from opentrons_server.gateway.service import OT2Service, OT2ServiceState, UnknownOutcomeError
from opentrons_server.gateway.deck import DeckDeclarationStore
from opentrons_server.gateway.tip_state import TipStateStore
from opentrons_server.gateway.plate_state import PlateStateStore
from opentrons_server.gateway.weigher_http import WeigherHttpDriver, WeigherUnavailable

URL = "http://127.0.0.1:8078"


class FakeWeigher:
    """The weigh-every-plate routes this driver uses, with a hard claim."""

    def __init__(self, *reads):
        self.reads = list(reads)          # read bodies, or an int HTTP status
        self.calls: list[str] = []
        self.holder: str | None = None
        self.claim_status = 200
        self.reference_status = 200
        self.reference_error: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        token = request.headers.get("x-claim-token")
        if path == "/control/claim":
            if self.claim_status != 200:
                return httpx.Response(self.claim_status, json={"detail": {"detail": "claimed by operator"}})
            self.holder = "tok"
            return httpx.Response(200, json={"claim_token": "tok", "heartbeat_interval_s": 2.0,
                                             "expires_at": "2026-10-11T00:00:00Z"})
        if path == "/control/release":
            self.holder = None
            return httpx.Response(204)
        if token != self.holder or token is None:
            return httpx.Response(423, json={"detail": {"detail": "Valid X-Claim-Token required"}})
        if path == "/control/heartbeat":
            return httpx.Response(204)
        if path == "/control/read":
            body = self.reads.pop(0)
            if isinstance(body, int):
                return httpx.Response(body, json={"detail": "Lift is moving"})
            return httpx.Response(200, json={"tared_s_ago": None, "condition": None, **body})
        if path in ("/control/tare", "/control/zero"):
            if self.reference_error is not None:
                raise self.reference_error
            return httpx.Response(self.reference_status, json={"message": "ok", "detail": "refused"})
        return httpx.Response(404)


def weight(value, stable=True):
    return {"mass_g": value, "stable": stable}


def balance_with(fake: FakeWeigher) -> PlateBalanceV1:
    config = PlateBalanceConfig(transport="weigh_every_plate", weigher_url=URL)
    driver = WeigherHttpDriver(URL, owner="test-gateway", transport=httpx.MockTransport(fake))
    return PlateBalanceV1(config, driver_factory=lambda _: driver)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("opentrons_server.gateway.platebalance.time.sleep", lambda s: None)


# ----- config ------------------------------------------------------------

def test_serial_stays_the_default_and_weigher_fields_never_reach_the_serial_driver(monkeypatch):
    config = PlateBalanceConfig(com_port="COM3")
    assert config.transport == "serial" and config.configured
    assert PlateBalanceV1(config)._factory is matterlab_driver
    import sys, types
    captured = {}

    class SartoriusBalance:
        def __init__(self, **kwargs):
            captured.update(kwargs)
    monkeypatch.setitem(sys.modules, "matterlab_balances", types.SimpleNamespace(SartoriusBalance=SartoriusBalance))
    monkeypatch.setitem(sys.modules, "matterlab_serial_device", types.SimpleNamespace(open_close=lambda f: f))
    matterlab_driver(config)
    assert not {"transport", "weigher_url", "weigher_owner"} & set(captured)


def test_weigher_transport_selects_the_http_driver():
    config = PlateBalanceConfig(transport="weigh_every_plate", weigher_url=URL)
    balance = PlateBalanceV1(config)
    assert balance._factory is weigher_http_driver and config.configured
    snapshot = balance.snapshot()
    assert snapshot["configured"] is True and snapshot["transport"] == "weigh_every_plate"
    assert balance.supports("tare")


@pytest.mark.parametrize("fields", [
    {"transport": "weigh_every_plate"},
    {"transport": "weigh_every_plate", "weigher_url": URL, "com_port": "COM3"},
    {"weigher_url": URL, "com_port": "COM3"},
    {"transport": "weigh_every_plate", "weigher_url": "http://127.0.0.1:8078/control"},
    {"transport": "weigh_every_plate", "weigher_url": "file:///etc/passwd"},
])
def test_transport_fields_must_agree(fields):
    with pytest.raises(ValidationError):
        PlateBalanceConfig(**fields)


# ----- the same tare and read rules, over HTTP ---------------------------

def test_read_claims_reads_once_and_releases():
    fake = FakeWeigher(weight(-1.2345))
    snapshot = balance_with(fake).execute("read")
    assert snapshot["reading"]["value"] == -1.2345 and snapshot["reading"]["stable"] is True
    assert fake.calls == ["/control/claim", "/control/read", "/control/release"] and fake.holder is None


def test_tare_holds_one_claim_through_the_whole_baseline_wait():
    fake = FakeWeigher({"mass_g": None, "stable": None, "condition": "no_frame"},
                       {"mass_g": None, "stable": None, "condition": "unreadable"},
                       weight(0.01, False), weight(0.0001), weight(0.0))
    snapshot = balance_with(fake).execute("tare", request=PlateBalanceRequest(timeout_s=30))
    assert snapshot["last_operation"]["outcome"] == "baseline_observed"
    assert fake.calls.count("/control/tare") == 1 and fake.calls.count("/control/claim") == 1
    assert fake.calls[0] == "/control/claim" and fake.calls[-1] == "/control/release"


def test_no_frame_and_unreadable_map_to_the_serial_exceptions():
    fake = FakeWeigher({"mass_g": None, "stable": None, "condition": "no_frame"},
                       {"mass_g": None, "stable": None, "condition": "overload"})
    driver = WeigherHttpDriver(URL, owner="g", transport=httpx.MockTransport(fake))
    with driver.operation():
        with pytest.raises(BalanceNoFrame):
            driver._weigh()
        with pytest.raises(ValueError, match="overload"):
            driver._weigh()


def test_a_claim_held_by_someone_else_is_a_plain_failure_not_an_unknown_outcome():
    fake = FakeWeigher()
    fake.claim_status = 409
    balance = balance_with(fake)
    with pytest.raises(WeigherUnavailable):
        balance.execute("tare")
    assert balance.snapshot()["last_operation"]["outcome"] == "not_sent"
    assert "/control/tare" not in fake.calls


@pytest.mark.parametrize("status", [412, 423])
def test_a_refused_tare_was_not_sent(status):
    fake = FakeWeigher()
    fake.reference_status = status
    balance = balance_with(fake)
    with pytest.raises(WeigherUnavailable):
        balance.execute("tare")
    assert balance.snapshot()["last_operation"]["outcome"] == "not_sent" and fake.holder is None


def test_a_tare_that_may_have_reached_the_balance_is_an_unknown_outcome():
    fake = FakeWeigher()
    fake.reference_error = httpx.ReadTimeout("no answer")
    balance = balance_with(fake)
    with pytest.raises(OSError, match="outcome is unknown"):
        balance.execute("tare")
    assert balance.snapshot()["last_operation"]["outcome"] == "unknown_outcome" and fake.holder is None


def test_a_refused_read_after_the_tare_was_sent_is_still_unknown():
    fake = FakeWeigher(412)
    balance = balance_with(fake)
    with pytest.raises(OSError, match="outcome is unknown"):
        balance.execute("tare")
    assert balance.snapshot()["last_operation"]["outcome"] == "unknown_outcome"


def test_a_weigher_server_error_on_tare_is_unknown():
    fake = FakeWeigher()
    fake.reference_status = 503
    balance = balance_with(fake)
    with pytest.raises(OSError, match="outcome is unknown"):
        balance.execute("tare")


def test_tare_baseline_timeout_is_unconfirmed_as_on_serial(monkeypatch):
    clock = iter(x * 0.5 for x in range(1000))
    monkeypatch.setattr("opentrons_server.gateway.platebalance.time.monotonic", lambda: next(clock))
    fake = FakeWeigher(*[weight(0.5)] * 400)
    balance = balance_with(fake)
    with pytest.raises(BalanceReferenceTimeout):
        balance.execute("tare", request=PlateBalanceRequest(timeout_s=5, attempts=1))
    assert balance.snapshot()["last_operation"]["outcome"] == "baseline_unconfirmed"
    assert fake.holder is None


def test_long_operations_heartbeat_the_claim(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("opentrons_server.gateway.weigher_http.time.monotonic", lambda: now[0])
    fake = FakeWeigher(weight(1.0), weight(1.0))
    driver = WeigherHttpDriver(URL, owner="g", transport=httpx.MockTransport(fake))
    with driver.operation():
        driver._weigh()
        now[0] = 31.0
        driver._weigh()
    assert fake.calls == ["/control/claim", "/control/read", "/control/heartbeat", "/control/read",
                          "/control/release"]


@pytest.mark.parametrize("action", ["read", "tare", "zero"])
def test_an_unreachable_weigher_fails_without_latching_an_unknown_outcome(action, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def refuse(request):
        raise httpx.ConnectError("connection refused")
    config = PlateBalanceConfig(transport="weigh_every_plate", weigher_url=URL)
    driver = WeigherHttpDriver(URL, owner="g", transport=httpx.MockTransport(refuse))
    balance = PlateBalanceV1(config, driver_factory=lambda _: driver)
    service = OT2Service(platebalance=balance,
                         decks=DeckDeclarationStore(state_path=tmp_path / "deck.json"),
                         tips=TipStateStore(state_path=tmp_path / "tips.json"),
                         plates=PlateStateStore(state_path=tmp_path / "plate.json"))
    service.state = OT2ServiceState.DRY_RUN if service.dry_run else OT2ServiceState.READY
    service.refresh_snapshot = Mock()
    if service.simulation:
        pytest.skip("simulation never reaches the driver")
    with pytest.raises(WeigherUnavailable):
        service.platebalance_action(action)
    assert service.state != OT2ServiceState.UNKNOWN_OUTCOME
    assert balance.snapshot()["last_operation"]["outcome"] == "not_sent"
