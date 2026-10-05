"""Human-approved step lists for agent-proposed OT-2 operation.

An agent (Hermes, `lab-skills`, anything else) may *propose* an ordered list of
control actions. That is the whole of its reach. A human then reviews the
proposal in the operator UI, approves it, and runs it — both of those last two
steps require the device claim, which an agent never holds. This module is that
gate.

**Not a "run authorization".** That term is taken, and the collision is worth
avoiding deliberately: ``ac-organic-lab/docs/AGENTIC_ELN_DESIGN.md`` §12 defines
a *Run Authorization* as a campaign-level gate that pins a merged commit SHA, a
protocol schema version and a compiled-package digest, revalidates inventory and
device readiness, and lands an immutable ``authorization_id`` in AnaliticaDB.

What lives here is a smaller, device-local thing — a **step approval**: one
operator, holding the claim, agreeing to one ad-hoc list of actions on one
robot, for the next few minutes. It governs the work a run authorization does
*not* cover: bring-up, homing, a manual tip pickup, turning the lights off.

The two compose rather than compete — layer 4 decides which validated protocols
may run at all; this decides whether the person at the bench meant to press the
button. Calling both "authorization" would conflate a durable scientific record
with an ephemeral operator gesture in exactly the conversation where the
difference matters.

Why the gate has to live here, in the gateway
---------------------------------------------
The agent harness is a general autonomous system — it has a shell, cron, and
subagents, and it can be driven from a chat app by someone who is nowhere near
the robot. It is therefore not trustworthy as a safety layer, and nothing in
this module assumes it behaves. Every rule below is enforced device-side, on
the only machine that can actually see the deck.

The three rules that make an approval mean something:

1. **The step list is hashed, and the human approves the hash.** Editing any
   step after approval changes the hash and silently voids the approval
   (:meth:`PlanStore.replace_steps`). An agent cannot get approval for a
   harmless plan and then swap the steps.
2. **Approval requires a live claim, and starting execution requires the
   *same* claim session.** The claim is held by the operator's browser, and
   its TTL only refreshes while that page is heartbeating: an approval whose
   operator walked away before pressing Run dies with their claim. Once the
   run starts, the claim passes to the plan itself (an ``automation`` owner,
   :meth:`ClaimManager.hand_to_plan`) and the plan runs to completion, error
   or stop whether or not the page stays open. Anyone signed in may stop,
   pause or abort it meanwhile; nobody else can take control.
3. **Nothing here can approve or start itself.** :meth:`PlanStore.approve`
   takes a ``ClaimedBy`` — an identity the caller cannot mint — and the API
   layer only ever passes one obtained from a real claim. Both ``approve``
   and ``execute`` are claim-gated in ``api.py``, so every path that moves the
   robot begins with a human clicking in the UI. An agent can draft work at
   3am; it cannot start it.

Plans are held in memory on purpose. A claim does not survive a gateway
restart (``claims.ClaimManager``), so an *approval* must not either —
otherwise a restart would leave an approved plan executable with no human
attached to it. Losing a draft on restart is a mild annoyance; keeping a stale
approval is a hazard.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from functools import wraps
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence

from pydantic import BaseModel, Field, ValidationError

from .advanced import ADVANCED_ACTIONS
from .platebalance import PlateBalanceReferenceRequest, PlateBalanceRequest
from .claims import plan_session_id
from .models import (
    ClaimedBy,
    DeckDeclareRequest,
    LightsRequest,
    LiquidMoveRequest,
    DispenseRequest,
    DropTipRequest,
    MoveLabwareRequest,
    MoveToRequest,
    PlateLoadRequest,
    ProtocolSetupRequest,
    TempmodDeactivateRequest,
    TempmodSetRequest,
    TipRequest,
    TipsMarkRequest,
    TipsResetRequest,
    WellUpdateRequest,
)

logger = logging.getLogger(__name__)

# How long an approval stays good *to start* the plan. Short on purpose: it is
# a standing permission to move a robot, and the operator who granted it is
# expected to be watching. Once execution has started, the approval covers the
# whole run — a 225-step balance plan halted mid-run at exactly this many
# seconds on 2026-10-02 when the window was also checked before every step.
# During a run the plan holds the claim itself (rule 2 above); it halts on a
# cleared claim, a revoked approval, a failed or refused step, or a stop.
APPROVAL_TTL_S = 600.0


PlanStatus = Literal[
    "draft",       # proposed, not approved — the only state an agent can create
    "approved",  # a human approved this exact step hash
    "executing",
    "executed",
    "failed",
    "aborted",
]

StepOutcome = Literal["pending", "ok", "failed", "skipped"]


# ---------------------------------------------------------------------------
# The plannable action catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """One action a plan step may name.

    ``idempotent`` mirrors ``OT2Service._run_action``: a transport loss during
    a non-idempotent action leaves the robot in ``unknown_outcome``, which no
    plan may drive through automatically.
    """

    model: Optional[type[BaseModel]]
    idempotent: bool
    invoke: Callable[[Any, Optional[BaseModel]], Any]


# Deliberately excluded, and why — these are not oversights:
#   startup   — carries an SSH credential and is a bring-up step, not lab work
#   shutdown  — lifecycle; ending a session is an operator decision
#   pause /
#   resume    — control flow over a running plan; must stay immediate, never
#               queued behind other steps
#   reconcile — the recovery action for `unknown_outcome`. Letting a plan clear
#               its own "did that actually happen?" state would erase exactly
#               the signal a human is supposed to adjudicate.
PLAN_ACTIONS: Dict[str, ActionSpec] = {
    "home": ActionSpec(None, True, lambda svc, _a: svc.home()),
    "setup": ActionSpec(
        ProtocolSetupRequest, True, lambda svc, a: svc.setup_protocol(a.model_dump())
    ),
    "move_to": ActionSpec(MoveToRequest, True, lambda svc, a: svc.move_to(a)),
    "pick_up_tip": ActionSpec(TipRequest, False, lambda svc, a: svc.pick_up_tip(a)),
    "aspirate": ActionSpec(LiquidMoveRequest, False, lambda svc, a: svc.aspirate(a)),
    "dispense": ActionSpec(DispenseRequest, False, lambda svc, a: svc.dispense(a)),
    "drop_tip": ActionSpec(DropTipRequest, False, lambda svc, a: svc.drop_tip(a)),
    "move_labware": ActionSpec(
        MoveLabwareRequest, False, lambda svc, a: svc.move_labware(a)
    ),
    # Bookkeeping — no robot motion, but still gated: they rewrite the records
    # a later step's tip/well guard reads.
    "plate.load": ActionSpec(
        PlateLoadRequest,
        True,
        lambda svc, a: svc.load_plate(plate_id=a.plate_id, model=a.model, wells=a.wells),
    ),
    "plate.unload": ActionSpec(None, True, lambda svc, _a: svc.unload_plate()),
    "well.update": ActionSpec(
        WellUpdateRequest,
        True,
        lambda svc, a: svc.update_well(
            a.well,
            sample_id=a.sample_id,
            volume_ul=a.volume_ul,
            notes=a.notes,
            clear_sample_id=a.clear_sample_id,
            clear_notes=a.clear_notes,
        ),
    ),
    "tips.reset": ActionSpec(
        TipsResetRequest, True, lambda svc, a: svc.reset_tip_rack(a.target, wells=a.wells)
    ),
    "tips.mark": ActionSpec(
        TipsMarkRequest,
        True,
        lambda svc, a: svc.mark_tips(
            a.target, status=a.status, wells=a.wells, columns=a.columns
        ),
    ),
    "lights.set": ActionSpec(LightsRequest, True, lambda svc, a: svc.set_lights(a.on)),
    "deck.declare": ActionSpec(
        DeckDeclareRequest, True, lambda svc, a: svc.declare_deck(a.slots)
    ),
    # Set-target only (no wait): the ramp is hardware-side and /status already
    # shows current vs target. Waiting would pin BUSY for minutes.
    "tempmod.set": ActionSpec(
        TempmodSetRequest, True, lambda svc, a: svc.set_tempmod_temperature(a)
    ),
    "tempmod.deactivate": ActionSpec(
        TempmodDeactivateRequest, True, lambda svc, a: svc.deactivate_tempmod(a)
    ),
    # Reference writes are non-idempotent. Tare waits for an observed stable
    # baseline; zero still reports only that its serial command was sent.
    "platebalance.read": ActionSpec(
        PlateBalanceRequest, True, lambda svc, a: svc.platebalance_action("read", a)
    ),
    "platebalance.tare": ActionSpec(
        PlateBalanceReferenceRequest, False, lambda svc, _a: svc.platebalance_action("tare")
    ),
    "platebalance.zero": ActionSpec(
        PlateBalanceReferenceRequest, False, lambda svc, _a: svc.platebalance_action("zero")
    ),
}


for _name, _operation in ADVANCED_ACTIONS.items():
    PLAN_ACTIONS[_name] = ActionSpec(
        _operation.model, _operation.idempotent,
        lambda svc, args, name=_name: svc.advanced_action(name, args),
    )


# ---------------------------------------------------------------------------
# Errors — each maps to one HTTP status in api.py
# ---------------------------------------------------------------------------


class PlanError(Exception):
    """Base for every refusal this module issues."""


class PlanNotFound(PlanError):
    pass


class StepValidationError(PlanError):
    """A step named an unknown action, or its args failed the action's model."""


class PlanStateError(PlanError):
    """The plan is not in a state where this operation makes sense."""


class PlanHashMismatch(PlanError):
    """The step list changed since the human looked at it.

    The whole point of the gate: whatever was approved is not what is being
    run, so the approval does not transfer.
    """


class ApprovalRequiresClaim(PlanError):
    """Approving (or executing) needs a live claim held by a human."""


class StepNotAllowed(PlanError):
    """The device would refuse this step right now.

    Raised from the live ``allowed_actions`` re-check immediately before a step
    runs, so a plan approved when the deck was ready cannot fire into a robot
    that has since faulted.
    """


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PlanStep(BaseModel):
    action: str
    args: Dict[str, Any] = {}

    def spec(self) -> ActionSpec:
        try:
            return PLAN_ACTIONS[self.action]
        except KeyError:
            raise StepValidationError(
                f"unknown action {self.action!r}; allowed: {sorted(PLAN_ACTIONS)}"
            ) from None

    def validated_args(self) -> Optional[BaseModel]:
        """Parse ``args`` with the action's own request model.

        This is interlock layer 1 applied at *proposal* time: a bad volume or a
        malformed well is refused while it is still a sentence in a chat
        window, not when a pipette is already moving.
        """
        spec = self.spec()
        if spec.model is None:
            return None
        try:
            return spec.model(**self.args)
        except ValidationError as exc:
            raise StepValidationError(f"{self.action}: {exc}") from exc


class StepResult(BaseModel):
    action: str
    outcome: StepOutcome = "pending"
    message: Optional[str] = None
    reading: Optional[Dict[str, Any]] = None
    balance_operation: Optional[Dict[str, Any]] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class StepApproval(BaseModel):
    """Proof a human approved one exact step list."""

    owner: str
    session_id: str
    step_hash: str
    approved_at: datetime
    expires_at: datetime

    def expired(self, *, now: Optional[datetime] = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at


class Plan(BaseModel):
    plan_id: str
    steps: List[PlanStep]
    step_hash: str
    status: PlanStatus = "draft"
    created_at: datetime
    created_by: str
    results: List[StepResult] = []
    approval: Optional[StepApproval] = None
    # Set when a plan stops early, so the operator sees why without digging
    # through per-step results.
    halt_reason: Optional[str] = None
    # The ELN project the approver chose for this plan's results; None keeps
    # them on the gateway only (see plan_results.py).
    eln_project: Optional[str] = None

    @property
    def non_idempotent_actions(self) -> List[str]:
        """Steps that cannot be safely repeated after a transport loss.

        Surfaced to the review UI so the operator approving a plan can see
        which steps carry an unrecoverable-ambiguity risk.
        """
        return [s.action for s in self.steps if not PLAN_ACTIONS[s.action].idempotent]


class PlanCreateRequest(BaseModel):
    """``POST /plans`` — an agent proposing work. Creates a draft, nothing more."""

    steps: List[PlanStep]
    # Who proposed this, for the review UI and the audit row. Free-form because
    # the proposer is not authenticated at this layer; it is a label, never a
    # permission. Approval identity comes from the claim, not from here.
    created_by: str = "agent"
    notes: Optional[str] = None


class PlanReviseRequest(BaseModel):
    """``PUT /plans/{id}/steps`` — an edit, which voids any approval."""

    steps: List[PlanStep]


class PlanApproveRequest(BaseModel):
    """``POST /plans/{id}/approve`` — the human gate.

    ``step_hash`` must be the hash the operator was shown. See
    :meth:`PlanStore.approve`.
    """

    step_hash: str
    # ELN project (title) to file the results under once the plan ends.
    eln_project: Optional[str] = Field(default=None, min_length=1, max_length=200)


def compute_step_hash(steps: Sequence[PlanStep]) -> str:
    """Stable digest of the step list.

    Canonical JSON (sorted keys, no incidental whitespace) so that re-ordering
    a dict or reformatting cannot change the hash, while any change to an
    action or an argument value does.
    """
    payload = json.dumps(
        [{"action": s.action, "args": s.args} for s in steps],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


@dataclass
class PlanStore:
    """In-memory plan registry. See the module docstring on why not persisted."""

    _plans: Dict[str, Plan] = field(default_factory=dict)
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    # -- lifecycle ---------------------------------------------------------

    @_synchronized
    def create(self, steps: Sequence[PlanStep], *, created_by: str) -> Plan:
        if not steps:
            raise StepValidationError("a plan needs at least one step")
        for step in steps:
            step.validated_args()  # layer 1, at proposal time
        plan = Plan(
            plan_id=secrets.token_urlsafe(12),
            steps=[step.model_copy(deep=True) for step in steps],
            step_hash=compute_step_hash(steps),
            created_at=datetime.now(timezone.utc),
            created_by=created_by,
            results=[StepResult(action=s.action) for s in steps],
        )
        self._plans[plan.plan_id] = plan
        return plan

    @_synchronized
    def get(self, plan_id: str) -> Plan:
        try:
            return self._plans[plan_id]
        except KeyError:
            raise PlanNotFound(f"no plan {plan_id!r}") from None

    @_synchronized
    def list(self) -> List[Plan]:
        return sorted(self._plans.values(), key=lambda p: p.created_at, reverse=True)

    @_synchronized
    def replace_steps(self, plan_id: str, steps: Sequence[PlanStep]) -> Plan:
        """Revise a plan — which always drops it back to ``draft``.

        This is the teeth behind rule 1. An edit does not "update an approved
        plan"; it produces a different plan that has never been approved.
        """
        plan = self.get(plan_id)
        if plan.status in {"executing", "executed", "failed", "aborted"}:
            raise PlanStateError(f"plan {plan_id} is {plan.status} and cannot be edited")
        if not steps:
            raise StepValidationError("a plan needs at least one step")
        for step in steps:
            step.validated_args()
        plan.steps = [step.model_copy(deep=True) for step in steps]
        plan.step_hash = compute_step_hash(steps)
        plan.results = [StepResult(action=s.action) for s in steps]
        plan.status = "draft"
        plan.approval = None
        plan.eln_project = None
        return plan

    # -- the gate ----------------------------------------------------------

    @_synchronized
    def approve(
        self,
        plan_id: str,
        *,
        step_hash: str,
        claimed_by: Optional[ClaimedBy],
        eln_project: Optional[str] = None,
    ) -> Plan:
        """Record a human's approval of one exact step list.

        ``step_hash`` is what the operator was *shown*. Requiring them to send
        it back is what makes this a review rather than a rubber stamp: if the
        plan changed between render and click, the hashes differ and the
        approval is refused instead of silently applying to new steps.
        """
        plan = self.get(plan_id)
        if plan.status != "draft":
            raise PlanStateError(
                f"plan {plan_id} is {plan.status}; only a draft can be approved"
            )
        if claimed_by is None:
            raise ApprovalRequiresClaim(
                "approving a plan requires holding the device claim"
            )
        if step_hash != plan.step_hash:
            raise PlanHashMismatch(
                "the plan changed since it was displayed — re-read it and approve again"
            )
        now = datetime.now(timezone.utc)
        plan.approval = StepApproval(
            owner=claimed_by.owner,
            session_id=claimed_by.session_id,
            step_hash=plan.step_hash,
            approved_at=now,
            expires_at=now + timedelta(seconds=APPROVAL_TTL_S),
        )
        plan.status = "approved"
        plan.eln_project = eln_project.strip() if eln_project and eln_project.strip() else None
        return plan

    @_synchronized
    def check_executable(self, plan_id: str, *, claimed_by: Optional[ClaimedBy]) -> Plan:
        """Every reason a plan may not run, checked in one place.

        Called by the executor, and also by the API's read path so the review
        UI can grey out "Execute" with the real reason rather than guessing.
        """
        plan = self.get(plan_id)
        if plan.status != "approved":
            raise PlanStateError(f"plan {plan_id} is {plan.status}, not approved")
        auth = plan.approval
        if auth is None:  # pragma: no cover — status invariant
            raise PlanStateError("approved plan has no approval record")
        if auth.expired():
            plan.status = "draft"
            plan.approval = None
            raise PlanStateError("approval expired; have an operator review it again")
        if auth.step_hash != plan.step_hash:  # pragma: no cover — replace_steps resets
            raise PlanHashMismatch("plan changed after approval")
        if claimed_by is None:
            raise ApprovalRequiresClaim("no live claim; the approving operator is gone")
        if claimed_by.session_id != auth.session_id or claimed_by.owner != auth.owner:
            raise ApprovalRequiresClaim(
                "the claim is held by a different session than the one that approved "
                "this plan; have the current holder review it"
            )
        return plan

    @_synchronized
    def abort(self, plan_id: str, *, reason: str) -> Plan:
        plan = self.get(plan_id)
        if plan.status in {"executed", "failed", "aborted"}:
            return plan
        for result in plan.results:
            if result.outcome == "pending" and result.started_at is None:
                result.outcome = "skipped"
        plan.status = "aborted"
        plan.halt_reason = reason
        plan.approval = None
        return plan

    @_synchronized
    def delete(self, plan_id: str) -> None:
        """Dismiss a settled plan — remove it from the registry entirely.

        Only terminal plans (executed / failed / aborted) may be deleted: a
        draft or approved plan is *aborted* instead, so the proposing agent
        can observe the rejection, and an executing plan is not removable at
        all. This is how an operator clears a failed plan's banner; what ran
        is already durable in the events exporter's audit rows, and the
        registry itself is in-memory (dies with the process) by design.
        """
        plan = self.get(plan_id)
        if plan.status not in {"executed", "failed", "aborted"}:
            raise PlanStateError(
                f"plan {plan_id} is {plan.status}; abort it instead of deleting"
            )
        if any(r.started_at is not None and r.finished_at is None for r in plan.results):
            raise PlanStateError("a plan with a command still in flight cannot be deleted")
        del self._plans[plan_id]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class PlanExecutor:
    """Runs an approved plan, one step at a time, against the live device."""

    def __init__(self, service: Any, store: PlanStore) -> None:
        self._service = service
        self._store = store
        # Called with a snapshot of the running plan when it starts and as each
        # step starts and ends (plan_results.save_progress): the run record on
        # disk follows the run, so it can be watched live and survives a
        # restart. If a save fails the plan stops before its next step —
        # never mid-step — so no step ever runs unrecorded, and a recovered
        # record's "not started" is true.
        self.on_progress: Optional[Callable[[Plan], None]] = None
        self.progress_error: Optional[str] = None

    def _report_progress(self, plan: Plan) -> bool:
        if self.on_progress is None:
            return True
        with self._store._lock:
            snapshot = plan.model_copy(deep=True)
        try:
            self.on_progress(snapshot)
        except Exception as exc:  # surfaced in /status and as the halt reason
            self.progress_error = f"run record not saved: {exc}"
            logger.exception("plan %s: run record not saved", plan.plan_id)
            return False
        self.progress_error = None
        return True

    def _halt_unrecorded(self, plan: Plan, from_index: int) -> None:
        with self._store._lock:
            if plan.status == "executing":
                self._halt(plan, from_index,
                           f"{self.progress_error}; stopped before the next step so "
                           "nothing runs unrecorded")

    def execute(self, plan_id: str, *, claimed_by: Optional[ClaimedBy]) -> Plan:
        # Validation and reservation must be one transaction: concurrent
        # execute/revise requests cannot spend or replace the same approval.
        with self._store._lock:
            plan = self._store.check_executable(plan_id, claimed_by=claimed_by)
            plan.status = "executing"
            plan.halt_reason = None
            # From here the plan, not the approving browser, holds the device:
            # closing the page no longer halts the run. Released however the
            # run ends.
            self._service.claims.hand_to_plan(
                plan.plan_id, approved_by=plan.approval.owner, total_steps=len(plan.steps)
            )
        try:
            if not self._report_progress(plan):
                self._halt_unrecorded(plan, 0)
                return plan
            return self._run(plan)
        finally:
            self._service.claims.release_plan(plan.plan_id)

    def _run(self, plan: Plan) -> Plan:
        for index, step in enumerate(plan.steps):
            if not self._wait_while_paused(plan):
                return plan
            with self._store._lock:
                if plan.status != "executing":
                    return plan
                result = plan.results[index]
                try:
                    self._assert_live_approval(plan)
                    self._assert_allowed(step)
                except (ApprovalRequiresClaim, PlanStateError, StepNotAllowed) as exc:
                    self._halt(plan, index, str(exc))
                    return plan
                result.started_at = datetime.now(timezone.utc)
                self._service.claims.plan_progress(plan.plan_id, step=index + 1, action=step.action)
            if not self._report_progress(plan):
                # The start could not be recorded: do not send this step.
                with self._store._lock:
                    result.started_at = None
                self._halt_unrecorded(plan, index)
                return plan
            # Do not hold the registry lock during I/O: abort must remain
            # available. A command already started keeps its actual outcome.
            try:
                spec = step.spec()
                output = spec.invoke(self._service, step.validated_args())
                reading = None
                balance_operation = None
                step_message = None
                if step.action == "platebalance.read":
                    if not isinstance(output, dict):
                        raise ValueError("Balance read returned no result")
                    reading = output.get("reading")
                    if reading is None:
                        if not output.get("simulation"):
                            raise ValueError("Balance read returned no measurement")
                        step_message = "Simulation: no weight was measured"
                    elif not isinstance(reading, dict):
                        raise ValueError("Balance read returned an invalid measurement")
                elif step.action in {"platebalance.tare", "platebalance.zero"}:
                    if not isinstance(output, dict):
                        raise ValueError("Balance reference command returned no result")
                    balance_operation = output.get("last_operation")
                    expected = ("not_executed" if output.get("simulation") else
                                "baseline_observed" if step.action == "platebalance.tare" else "sent_unconfirmed")
                    if (not isinstance(balance_operation, dict)
                            or balance_operation.get("action") != step.action.removeprefix("platebalance.")
                            or balance_operation.get("outcome") != expected):
                        raise ValueError("Balance reference command returned an invalid outcome")
                    if step.action == "platebalance.tare" and not output.get("simulation"):
                        reading = output.get("reading")
                        if (not isinstance(reading, dict) or reading.get("stable") is not True
                                or not isinstance(reading.get("value"), (int, float))):
                            raise ValueError("Balance tare returned no stable baseline measurement")
                    step_message = ("Simulation: reference command was not sent" if output.get("simulation")
                                    else "Stable zero baseline observed after tare" if step.action == "platebalance.tare"
                                    else "Reference command sent; change unconfirmed")
            except Exception as exc:
                with self._store._lock:
                    result.outcome = "failed"
                    result.message = str(exc)
                    result.finished_at = datetime.now(timezone.utc)
                    if plan.status == "executing":
                        self._halt(plan, index + 1, f"{step.action} failed: {exc}")
                    return plan
            with self._store._lock:
                result.outcome = "ok"
                result.message = step_message
                result.reading = reading.copy() if reading is not None else None
                result.balance_operation = balance_operation.copy() if balance_operation is not None else None
                result.finished_at = datetime.now(timezone.utc)
                if plan.status != "executing":
                    return plan
            if not self._report_progress(plan):
                self._halt_unrecorded(plan, index + 1)
                return plan

        with self._store._lock:
            if plan.status != "executing":
                return plan
            if getattr(self._service, "_stop_latched", False) is True:
                self._halt(plan, len(plan.results), "Operator stopped the robot run")
                return plan
            plan.status = "executed"
            plan.approval = None
            return plan

    def _wait_while_paused(self, plan: Plan) -> bool:
        """Hold at a step boundary while the operator has the gateway paused.

        The operator's pause lands between commands (the running one always
        completes), so a paused plan is simply one that has not started its
        next step. Polled without the registry lock, so abort stays available
        and ends the wait. Returns False when the plan is no longer executing.
        """
        while True:
            state = getattr(getattr(self._service, "state", None), "value", None)
            if state != "paused":
                return True
            with self._store._lock:
                if plan.status != "executing":
                    return False
            time.sleep(0.2)

    def _assert_live_approval(self, plan: Plan) -> None:
        """Before each step: the approval still exists and the plan still
        holds the claim it was handed at start.

        The approving operator's own claim is not re-checked: it was enforced by
        ``check_executable`` when execution began, and the plan has held the
        device since, so a closed page or a sleeping laptop does not halt it.
        The approval's start window is not re-checked either; applying it per
        step killed any plan that ran longer than the window. A revoked
        approval (cleared by abort, revise, or stop) still halts at the next
        step, as does a plan claim that was cleared.
        """
        auth = plan.approval
        if auth is None:
            raise PlanStateError("approval revoked; review remaining work before continuing")
        live = self._service.claims.current()
        if live is None or live.session_id != plan_session_id(plan.plan_id):
            raise ApprovalRequiresClaim("the plan no longer holds the device claim")

    def _assert_allowed(self, step: PlanStep) -> None:
        """Layer-3 re-check against the device's own live answer.

        `allowed_actions` is rebuilt from current state on every call, so this
        catches a robot that faulted, got paused, or was seized by an external
        run between approval and this step.
        """
        allowed = self._service.allowed_actions()
        if step.action not in allowed:
            raise StepNotAllowed(
                f"device will not accept {step.action!r} right now "
                f"(allowed: {sorted(allowed)})"
            )

    def _halt(self, plan: Plan, from_index: int, reason: str) -> None:
        """Stop the plan, marking everything not yet run as skipped.

        Fail-fast, never continue-past-error: the later steps of a pipetting
        sequence assume the earlier ones happened.
        """
        for result in plan.results[from_index:]:
            if result.outcome == "pending":
                result.outcome = "skipped"
        plan.status = "failed"
        plan.halt_reason = reason
        plan.approval = None
