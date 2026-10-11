"""platebalanceV1 transport through a weigh-every-plate service (STATUS_SPEC v1.2).

The weigher service on the same PC owns the balance's serial port and its lift;
this driver gives ``PlateBalanceV1`` the same two calls the serial driver does,
so every tare and stability rule in ``platebalance.py`` is unchanged:

- ``_weigh()`` -> ``(stable, grams)``; raises ``BalanceNoFrame`` when the balance
  answered nothing and ``ValueError`` for a frame that is not a weight;
- ``set_reference("tare" | "zero")`` -> one command, never repeated here.

The weigher's control routes are claim-gated. ``operation()`` claims for one
whole ``PlateBalanceV1.execute`` (a tare wait included, so nothing can move the
lift mid-tare) and releases afterwards. ``WeigherUnavailable`` means nothing was
sent (claim refused, a precondition such as a moving lift, or no connection);
anything that may have reached the balance is an ``OSError`` so the gateway's
unknown-outcome rules apply.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
import time
from typing import Any, Iterator
import uuid

import httpx

logger = logging.getLogger(__name__)

_CLAIM_TTL_S = 120.0       # the weigher's maximum; covers a three-attempt tare
_HEARTBEAT_AFTER_S = 30.0


class WeigherUnavailable(RuntimeError):
    """The weigher refused or could not be reached; no balance command was sent."""


class WeigherHttpDriver:
    claims_per_operation = True

    def __init__(self, base_url: str, *, owner: str, timeout_s: float = 15.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport)
        self._owner = owner
        self._session_id = f"{owner}-{uuid.uuid4()}"
        self._token: str | None = None
        self._refreshed = 0.0

    # ----- claim -------------------------------------------------------

    @contextmanager
    def operation(self) -> Iterator[None]:
        try:
            response = self._http.post("/control/claim", json={
                "owner": self._owner, "session_id": self._session_id, "ttl_s": _CLAIM_TTL_S})
        except httpx.TransportError as exc:
            raise WeigherUnavailable(f"Weigher unreachable: {exc}") from exc
        if response.status_code != 200:
            raise WeigherUnavailable(f"Weigher claim refused (HTTP {response.status_code}): {_detail(response)}")
        self._token = response.json()["claim_token"]
        self._refreshed = time.monotonic()
        try:
            yield
        finally:
            token, self._token = self._token, None
            try:
                self._http.post("/control/release", headers={"X-Claim-Token": token})
            except httpx.TransportError as exc:
                # The claim then expires on its TTL; the operation's own outcome stands.
                logger.warning("weigher claim release failed: %s", exc)

    def _post(self, path: str, body: dict[str, Any], *, sent_if_failed: bool) -> dict[str, Any]:
        if self._token is None:
            raise WeigherUnavailable("Weigher command outside a claimed operation")
        headers = {"X-Claim-Token": self._token}
        try:
            if time.monotonic() - self._refreshed >= _HEARTBEAT_AFTER_S:
                self._http.post("/control/heartbeat", headers=headers).raise_for_status()
                self._refreshed = time.monotonic()
            response = self._http.post(path, json=body, headers=headers)
        except httpx.ConnectError as exc:
            raise WeigherUnavailable(f"Weigher unreachable: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise WeigherUnavailable(f"Weigher heartbeat refused: {_detail(exc.response)}") from exc
        except httpx.TransportError as exc:
            if sent_if_failed:
                raise OSError(f"Weigher {path} transport failed: {exc}") from exc
            raise WeigherUnavailable(f"Weigher {path} transport failed: {exc}") from exc
        if response.status_code in (412, 423):
            raise WeigherUnavailable(f"Weigher refused {path} (HTTP {response.status_code}): {_detail(response)}")
        if response.status_code != 200:
            raise OSError(f"Weigher {path} failed (HTTP {response.status_code}): {_detail(response)}")
        return response.json()

    # ----- the PlateBalanceV1 driver interface -------------------------

    def _weigh(self, *, timeout_s: float | None = None) -> tuple[bool, float]:
        # timeout_s: the weigher reads one whole frame per request with its own
        # serial timeout; the caller's wait loop still stops at its deadline.
        body = self._post("/control/read", {}, sent_if_failed=False)
        condition = body.get("condition")
        if condition == "no_frame":
            from .platebalance import BalanceNoFrame

            raise BalanceNoFrame("Balance returned no weight frame")
        if body.get("mass_g") is None:
            raise ValueError(f"Balance reported {condition or 'no weight'} instead of a weight")
        stable = body.get("stable")
        if type(stable) is not bool:
            raise ValueError("Weigher returned a weight without a stability flag")
        return stable, float(body["mass_g"])

    def set_reference(self, action: str) -> None:
        if action not in ("tare", "zero"):
            raise ValueError(f"unknown reference action {action!r}")
        self._post(f"/control/{action}", {}, sent_if_failed=True)


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        detail = detail.get("detail", detail)
    return str(detail)[:200]
