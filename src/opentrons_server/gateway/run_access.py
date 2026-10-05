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


def redact_plan_view(view: Dict[str, Any]) -> Dict[str, Any]:
    """A plan view without its measured data, for a reader who may see that
    the run happened but not what it measured."""
    out = dict(view)
    out["results"] = [
        {k: v for k, v in result.items() if k not in {"reading", "balance_operation", "message"}}
        for result in view.get("results") or []
    ]
    out["redacted"] = True
    return out
