"""Durable results of finished plans, and their delivery to the lab's ELN.

Plans live in memory and die with the process (see ``plans.py``), which is
right for the *permission* they carry but wrong for what they *measured*: until
this module, the balance weights of a 483-step run existed only in the
in-memory plan record, so a gateway restart or a dismissed card lost them.

When a plan ends, :meth:`PlanResultsStore.save` writes one JSON bundle per plan
to ``OT2_PLAN_RESULTS_DIR`` (default: ``ot2_plan_results/`` beside the tip-state
file, so each gateway keeps its own). The bundle is the record of what ran:
the full plan with every step's outcome and reading, who approved it, and the
per-well plate report when the plan weighed anything.

**Delivery.** A plan approved with an ELN project is also queued for the
central server, which files it in BitacoraDB as an unformatted Experiment for
the approver to curate. The gateway never writes the ELN itself: that needs
the ELN's edge secret and an asserted identity, which stay on the central host.
Delivery is a retrying outbox, not the best-effort events exporter, because
these are results rather than telemetry: a bundle stays ``pending`` on disk
until the central server acknowledges it, across restarts. Disabled unless
``OT2_RESULTS_URL`` is set; a dry-run gateway never delivers, so a simulation
cannot enter the lab's record.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import tempfile
import time
import threading
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .plate_report import build_plate_report

logger = logging.getLogger(__name__)

SCHEMA = "ot2.plan_results.v1"

# Delivery states, as stored in a bundle's ``delivery.state``.
LOCAL_ONLY = "local_only"  # no ELN project chosen, or delivery not configured
PENDING = "pending"
DELIVERED = "delivered"
RUNNING = "running"  # the plan is still executing; never delivered in this state

# What the central server did with a delivered bundle (``delivery.eln.state``).
# "delivered" only ever meant "journaled there"; these say whether the ELN
# actually has it.
ELN_FILING = "filing"  # accepted centrally, not filed yet (or not yet checked)
ELN_FILED = "filed"
ELN_HELD = "held"  # refused: not a member, project missing, rejected row
_ELN_TERMINAL = frozenset({ELN_FILED, ELN_HELD})
_FILING_POLL_S = 10.0
# A held bundle can still be filed later (someone creates the project, or a
# human refiles it centrally); it is re-checked at this slower pace.
_HELD_RECHECK_S = 300.0

#: The run status a bundle carries when its record was never completed.
INTERRUPTED = "interrupted"
_INTERRUPTED_REASON = (
    "This run's record was never completed: the gateway stopped (restart or crash) "
    "or could not write the final record. The step in progress at the last saved "
    "point has an unknown outcome; no later step was started (the executor stops "
    "before any step it cannot record). Inspect the robot before continuing."
)

#: Identifies this process as a record's writer, so recover() never closes out
#: a run that is still executing here (or in another live process on this host).
_WRITER = {"token": uuid.uuid4().hex, "pid": os.getpid(), "host": socket.gethostname()}


class InvalidPlanId(ValueError):
    pass


def _writer_alive(writer: Any) -> bool:
    if not isinstance(writer, dict):
        return False
    if writer.get("token") == _WRITER["token"]:
        return True
    if writer.get("host") != _WRITER["host"] or not isinstance(writer.get("pid"), int):
        return False
    return _pid_alive(writer["pid"])


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a running process — a pure query on every platform.

    Not ``os.kill(pid, 0)`` on Windows (where the gateways run): there a
    signal argument is not a probe — 0 is CTRL_C_EVENT and anything else
    calls TerminateProcess — so the "check" would interrupt or kill the other
    process. Windows opens the process for query only and reads its exit code.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX: signal 0 checks existence and sends nothing
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlanResultsStore:
    """One JSON file per finished plan, plus an optional delivery worker.

    ``transport`` is injectable for tests: a callable taking the payload dict
    and returning the central server's acknowledgement (a dict), raising on any
    failure. The default POSTs JSON with ``urllib``.
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        device_id: str,
        results_url: Optional[str] = None,
        results_token: Optional[str] = None,
        simulation: bool = False,
        retry_interval_s: float = 60.0,
        timeout_s: float = 15.0,
        transport: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        status_transport: Optional[Callable[[str], Dict[str, Any]]] = None,
        start_worker: bool = True,
    ) -> None:
        self.directory = Path(directory)
        self.device_id = device_id
        self.results_url = (results_url or "").strip() or None
        self._token = (results_token or "").strip() or None
        self.simulation = simulation
        self._retry_interval_s = retry_interval_s
        self._timeout_s = timeout_s
        self._transport = transport or self._default_transport
        self._status_transport = status_transport or self._default_status_transport
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Plan ids still waiting for the central server, seeded from disk so an
        # outbox survives restarts; /status reports its size.
        self._pending: set[str] = set()
        # Delivered bundles whose ELN outcome is not known yet (incl. ones
        # delivered before outcomes were tracked).
        self._awaiting: set[str] = set()
        self._held: set[str] = set()
        self._held_checked_at = time.monotonic()
        if self.directory.exists():
            for path in self.directory.glob("*.json"):
                bundle = json.loads(path.read_text(encoding="utf-8"))
                delivery = bundle.get("delivery") or {}
                if delivery.get("state") == PENDING:
                    self._pending.add(bundle["plan_id"])
                elif delivery.get("state") == DELIVERED:
                    eln_state = (delivery.get("eln") or {}).get("state")
                    if eln_state == ELN_HELD:
                        self._held.add(bundle["plan_id"])
                    elif eln_state != ELN_FILED:
                        self._awaiting.add(bundle["plan_id"])
        if self.delivery_enabled and start_worker:
            self._thread = threading.Thread(target=self._worker, name="ot2-plan-results", daemon=True)
            self._thread.start()

    @classmethod
    def from_env(cls, *, default_dir: Path, device_id: str, simulation: bool,
                 environ: Optional[dict] = None) -> "PlanResultsStore":
        env = os.environ if environ is None else environ
        return cls(
            env.get("OT2_PLAN_RESULTS_DIR") or default_dir,
            device_id=device_id,
            results_url=env.get("OT2_RESULTS_URL"),
            results_token=env.get("OT2_RESULTS_TOKEN"),
            simulation=simulation,
        )

    @property
    def delivery_enabled(self) -> bool:
        return self.results_url is not None and not self.simulation

    # -- writing ---------------------------------------------------------------

    def save(self, plan: Any, *, approved_by: Optional[str], equipment_id: str,
             gateway_version: str) -> Dict[str, Any]:
        """Write the final bundle for a plan that has ended; queue it for the ELN
        when it was approved with a project and delivery is configured."""
        record = plan.model_dump(mode="json")
        return self._finish(self._bundle(record, approved_by=approved_by,
                                         equipment_id=equipment_id,
                                         gateway_version=gateway_version))

    def save_progress(self, plan: Any, *, approved_by: Optional[str], equipment_id: str,
                      gateway_version: str) -> Dict[str, Any]:
        """Write the bundle of a plan that is still running, after each step
        starts and ends, so its steps and readings can be watched as they come
        in and survive a gateway restart. Never delivered while running."""
        record = plan.model_dump(mode="json")
        bundle = self._bundle(record, approved_by=approved_by, equipment_id=equipment_id,
                              gateway_version=gateway_version, final=False)
        self._write(bundle)
        return bundle

    def recover(self) -> List[str]:
        """Close out runs the last process left ``running``: the gateway died
        or restarted mid-run. The step that had started has an unknown
        outcome, later steps did not run; the run is marked ``interrupted``
        and, like any ended run, queued for the ELN when it has a project.
        Returns the plan ids recovered."""
        recovered: List[str] = []
        if not self.directory.exists():
            return recovered
        for path in sorted(self.directory.glob("*.json")):
            bundle = json.loads(path.read_text(encoding="utf-8"))
            if (bundle.get("delivery") or {}).get("state") != RUNNING:
                continue
            if _writer_alive(bundle.get("writer")):
                # Still being written by a live process: not ours to close out.
                if (bundle.get("writer") or {}).get("token") != _WRITER["token"]:
                    logger.warning("plan %s is running in another live process (pid %s); "
                                   "two gateways share %s", bundle["plan_id"],
                                   bundle["writer"].get("pid"), self.directory)
                continue
            record = bundle.get("plan") or {}
            for result in record.get("results") or []:
                if result.get("outcome") == "pending":
                    if result.get("started_at") and not result.get("finished_at"):
                        result["outcome"] = "unknown"
                        result["message"] = "In progress at the last saved point; outcome unknown"
                    else:
                        result["outcome"] = "skipped"
            record["status"] = INTERRUPTED
            record["halt_reason"] = _INTERRUPTED_REASON
            self._finish(self._bundle(record, approved_by=bundle.get("approved_by"),
                                      equipment_id=bundle.get("equipment_id") or "",
                                      gateway_version=bundle.get("gateway_version") or ""))
            recovered.append(bundle["plan_id"])
            logger.warning("plan %s was running when the gateway stopped; recorded as interrupted",
                           bundle["plan_id"])
        return recovered

    def _bundle(self, record: Dict[str, Any], *, approved_by: Optional[str], equipment_id: str,
                gateway_version: str, final: bool = True) -> Dict[str, Any]:
        results = record.get("results") or []
        started = [r["started_at"] for r in results if r.get("started_at")]
        finished = [r["finished_at"] for r in results if r.get("finished_at")]
        outcomes = [r.get("outcome") for r in results]
        report: Optional[Dict[str, Any]] = None
        report_error: Optional[str] = None
        # The plate report is derived data: built once, when the run ends.
        # While running, the report endpoints build it from the plan record.
        if final and any(r.get("reading") for r in results):
            try:
                report = build_plate_report([record])
            except ValueError as exc:
                # Recorded, not hidden: the readings are still in `plan`.
                report_error = str(exc)
        eln_project = record.get("eln_project")
        if not final:
            state = RUNNING
        else:
            state = PENDING if (eln_project and self.delivery_enabled) else LOCAL_ONLY
        return {
            "schema": SCHEMA,
            "plan_id": record["plan_id"],
            "device_id": self.device_id,
            "equipment_id": equipment_id,
            "gateway_version": gateway_version,
            "simulation": self.simulation,
            "approved_by": approved_by,
            "proposed_by": record.get("created_by"),
            "eln_project": eln_project,
            "status": record.get("status"),
            "halt_reason": record.get("halt_reason"),
            "started_at": min(started) if started else None,
            "finished_at": max(finished) if finished else None,
            "saved_at": _now(),
            "writer": dict(_WRITER),
            "steps_total": len(results),
            "steps_ok": outcomes.count("ok"),
            "steps_failed": outcomes.count("failed"),
            "steps_skipped": outcomes.count("skipped"),
            "steps_unknown": outcomes.count("unknown"),
            "steps_done": sum(1 for r in results if r.get("finished_at")),
            "readings": sum(1 for r in results if r.get("reading")),
            "plate_report": report,
            "plate_report_error": report_error,
            "plan": record,
            "delivery": {"state": state, "attempts": 0, "last_error": None,
                         "delivered_at": None, "receipt": None},
        }

    def _finish(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        self._write(bundle)
        if bundle["delivery"]["state"] == PENDING:
            with self._lock:
                self._pending.add(bundle["plan_id"])
            self._wake.set()
        return bundle

    def summary(self) -> Dict[str, Any]:
        """What /status shows: whether results reach the ELN, and how many are
        still waiting — an outbox that only grows is a broken link."""
        with self._lock:
            return {"delivery_enabled": self.delivery_enabled, "pending": len(self._pending),
                    "filing": len(self._awaiting)}

    def _path(self, plan_id: str) -> Path:
        if not plan_id or any(c in plan_id for c in "/\\.:"):
            raise InvalidPlanId(f"invalid plan id {plan_id!r}")
        return self.directory / f"{plan_id}.json"

    def _write(self, bundle: Dict[str, Any]) -> None:
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._path(bundle["plan_id"])
            # A unique temp file per write: two stores on one directory (two
            # apps in a test process) must not interleave on a shared name.
            fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(bundle, indent=1))
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            # The file time is the record's own save time, so a delivery retry
            # rewriting an old record does not make it look recent to list().
            # Ordering only: the record itself is already safely written, so a
            # failure here is logged, never reported as a lost record.
            try:
                stamp = datetime.fromisoformat(bundle["saved_at"]).timestamp()
                os.utime(path, (stamp, stamp))
            except (OSError, ValueError) as exc:
                logger.warning("plan %s: record saved, file time not set: %s",
                               bundle["plan_id"], exc)

    # -- reading ---------------------------------------------------------------

    def get(self, plan_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(plan_id)
        with self._lock:
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

    def list(self, limit: int = 50) -> List[Dict[str, Any]]:
        """The newest ``limit`` summaries (by file time), without the plan
        record and report — cheap enough for the panel to poll."""
        with self._lock:
            if not self.directory.exists():
                return []
            paths = sorted(self.directory.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:max(limit, 0)]
            bundles = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
        bundles.sort(key=lambda b: b.get("saved_at") or "", reverse=True)
        return [{k: v for k, v in b.items() if k not in {"plan", "plate_report"}}
                for b in bundles]

    # -- delivery --------------------------------------------------------------

    def deliver_pending(self) -> int:
        """One pass over the outbox. Returns how many bundles were delivered."""
        if not self.delivery_enabled:
            return 0
        delivered = 0
        with self._lock:
            pending = sorted(self._pending)
        for plan_id in pending:
            bundle = self.get(plan_id)
            if bundle is None or (bundle.get("delivery") or {}).get("state") != PENDING:
                with self._lock:
                    self._pending.discard(plan_id)
                continue
            payload = {k: v for k, v in bundle.items() if k != "delivery"}
            delivery = bundle["delivery"]
            delivery["attempts"] += 1
            try:
                receipt = self._transport(payload)
            except Exception as exc:  # recorded on the bundle and retried
                delivery["last_error"] = str(exc)
                if delivery["attempts"] == 1 or delivery["attempts"] % 10 == 0:
                    logger.warning("plan %s results not delivered (attempt %d): %s",
                                   bundle["plan_id"], delivery["attempts"], exc)
            else:
                delivery.update(state=DELIVERED, last_error=None, delivered_at=_now(),
                                receipt=receipt,
                                eln={"state": ELN_FILING, "last_error": None,
                                     "experiment_id": None, "checked_at": None})
                delivered += 1
            self._write(bundle)
            if delivery["state"] == DELIVERED:
                with self._lock:
                    self._pending.discard(plan_id)
                    self._awaiting.add(plan_id)
        return delivered

    def check_filing(self) -> int:
        """Ask the central server what became of delivered bundles; record
        ``filed`` or ``held`` (with its reason) on each. Returns how many
        reached an outcome."""
        if not self.delivery_enabled:
            return 0
        settled = 0
        now = time.monotonic()
        with self._lock:
            awaiting = sorted(self._awaiting)
            if self._held and now - self._held_checked_at >= _HELD_RECHECK_S:
                awaiting += sorted(self._held - self._awaiting)
                self._held_checked_at = now
        for plan_id in awaiting:
            bundle = self.get(plan_id)
            if bundle is None:
                with self._lock:
                    self._awaiting.discard(plan_id)
                continue
            delivery = bundle["delivery"]
            eln = dict(delivery.get("eln") or {"state": ELN_FILING, "last_error": None,
                                                "experiment_id": None})
            try:
                status = self._status_transport(plan_id)
            except Exception as exc:  # recorded and retried
                eln["checked_at"] = _now()
                eln["check_error"] = str(exc)[:300]
            else:
                central = status.get("state")
                eln["state"] = (ELN_FILED if central == "filed"
                                else ELN_HELD if central == "held" else ELN_FILING)
                eln["last_error"] = status.get("last_error")
                eln["experiment_id"] = status.get("experiment_id")
                eln["checked_at"] = _now()
                eln.pop("check_error", None)
            delivery["eln"] = eln
            self._write(bundle)
            with self._lock:
                if eln["state"] == ELN_FILED:
                    self._awaiting.discard(plan_id)
                    self._held.discard(plan_id)
                    settled += 1
                elif eln["state"] == ELN_HELD:
                    if plan_id in self._awaiting:
                        settled += 1
                    self._awaiting.discard(plan_id)
                    self._held.add(plan_id)
        return settled

    def _worker(self) -> None:
        while True:
            # Quick while something is being filed, so the panel shows the
            # outcome within seconds; the normal retry cadence otherwise.
            with self._lock:
                wait = _FILING_POLL_S if self._awaiting else self._retry_interval_s
            self._wake.wait(wait)
            self._wake.clear()
            try:
                self.deliver_pending()
                self.check_filing()
            except Exception:  # never let the outbox thread die silently
                logger.exception("plan results delivery pass failed")

    def _default_status_transport(self, plan_id: str) -> Dict[str, Any]:
        url = f"{self.results_url.rstrip('/')}/{self.device_id}/{plan_id}"
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {exc.code} from results status: {detail}") from exc

    def _default_transport(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            self.results_url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                body = response.read().decode("utf-8") or "{}"
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"HTTP {exc.code} from results endpoint: {detail}") from exc
        return json.loads(body)
