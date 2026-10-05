"""Who may read a run's data: one rule for the API routes and the assistant.

Run records, plate reports and plan results carry scientific data (balance
readings per well). In the ELN that data is scoped by project, so on the
gateway it is too (decided 2026-10-05):

* reading anything about runs needs an identity — the auth edge's signed-in
  user, or a configured API key; unauthenticated reads are refused;
* every signed-in user sees the run list (who used the robot, when, status),
  but a run's readings, plate report and full record only open for the user
  who approved it, members and PIs of the ELN project it was filed under, and
  admins;
* API keys are configured lab services (the workflow SDK, the agent MCP) and
  read everything;
* a draft nobody has approved has no data yet and is visible to everyone
  signed in.

With ``OT2_REQUIRE_LOGIN`` off (a dev or open deployment) an anonymous caller
is an open reader, as every other route on such a gateway already is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RunReader:
    user: Optional[str] = None
    projects: frozenset[str] = field(default_factory=frozenset)
    admin: bool = False
    #: An API-key principal, or an open (login-off) deployment: full read.
    unrestricted: bool = False

    def can_read(self, approved_by: Optional[str], eln_project: Optional[str]) -> bool:
        if self.unrestricted or self.admin:
            return True
        if self.user and approved_by and self.user == approved_by:
            return True
        return bool(eln_project) and eln_project in self.projects

    def can_read_plan(self, plan: Any) -> bool:
        """A live plan (``plans.Plan``): drafts nobody approved, and that never
        ran a step, carry no data and are readable by any reader."""
        approved_by = getattr(plan, "approved_by", None)
        started = any(getattr(r, "started_at", None) for r in getattr(plan, "results", []) or [])
        if approved_by is None and not started:
            return True
        return self.can_read(approved_by, getattr(plan, "eln_project", None))

    def can_read_record(self, record: Dict[str, Any]) -> bool:
        """A saved run record (``plan_results`` bundle or summary)."""
        return self.can_read(record.get("approved_by"), record.get("eln_project"))


#: What a reader who may not open a run still sees: that it exists, who ran
#: it, and how far it got — an allowlist, so a field added later stays hidden
#: until someone decides it is safe. Step arguments (sample ids, volumes) and
#: messages (a balance error can carry raw frames) are never on it.
_PLAN_FIELDS = ("plan_id", "status", "created_at", "created_by", "approved_by", "eln_project",
                "executable", "blocked_reason", "non_idempotent_actions", "step_hash")
_RESULT_FIELDS = ("action", "outcome", "started_at", "finished_at")
RESTRICTED = "(restricted to the run's approver, project members and admins)"


def redact_plan_view(view: Dict[str, Any]) -> Dict[str, Any]:
    """A plan view for a reader who may see that the run happened but not
    what it used or measured."""
    out: Dict[str, Any] = {k: view[k] for k in _PLAN_FIELDS if k in view}
    out["steps"] = [{"action": step.get("action")} for step in view.get("steps") or []]
    out["results"] = [{k: r.get(k) for k in _RESULT_FIELDS} for r in view.get("results") or []]
    out["halt_reason"] = RESTRICTED if view.get("halt_reason") else None
    out["approval"] = None
    out["redacted"] = True
    return out


_SUMMARY_FIELDS = ("plan_id", "status", "approved_by", "proposed_by", "eln_project", "equipment_id",
                   "started_at", "finished_at", "saved_at", "steps_total", "steps_done", "steps_ok",
                   "steps_failed", "steps_skipped", "steps_unknown", "readings")


def redact_record_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    """A run-list row for the same reader — an allowlist too: counts, times
    and states, never the halt reason or error text (which can carry data)."""
    out: Dict[str, Any] = {k: row[k] for k in _SUMMARY_FIELDS if k in row}
    out["halt_reason"] = RESTRICTED if row.get("halt_reason") else None
    delivery = row.get("delivery") or {}
    eln = delivery.get("eln") or {}
    out["delivery"] = {
        "state": delivery.get("state"),
        "last_error": RESTRICTED if delivery.get("last_error") else None,
        "delivered_at": delivery.get("delivered_at"),
        **({"eln": {"state": eln.get("state"),
                    "last_error": RESTRICTED if eln.get("last_error") else None,
                    "experiment_id": None}} if eln else {}),
    }
    return out
