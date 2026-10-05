"""Optional in-page chat assistant for one OT-2.

The gateway is self-contained by design — it ships its own UI inside the wheel,
its own claim protocol, its own identity gate, and runs with no dashboard, no
central server and no agent harness. This module keeps that true for the chat
box: install the package alone and you get one, without installing anything
else.

**Optional, and off unless configured.** With no provider key or configured
Claude Code CLI the assistant reports itself disabled. The ``openai`` import
is deliberately soft so a venv without it still serves a healthy gateway.

**It cannot move the robot.** Its entire tool surface is reads plus
``propose_plan`` — the same door an agent harness comes through
(``tools/ot2_agent_mcp.py``). A proposal is a draft; approving and running it
are claim-gated clicks in the operator panel (``gateway/plans.py``). So adding
a chat box widens the safety surface by nothing at all: it is one more
proposer behind the same gate, not a new path to the hardware.

**Scoped to this robot by construction.** The tools close over *this* service
instance. There is no shell, no other device, no scheduler — the constraint is
a property of the code rather than of a prompt, which is why this is a small
purpose-built endpoint and not a general agent embedded in a device page.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from .plans import PlanCreateRequest, PlanStep, PlanStore, StepValidationError, compile_proposal
from .robot_profile import PROFILE, IS_FLEX
from .documentation import action_catalog, equipment_documentation
from .assistant_claude import claude_code_authenticated, run_claude_code
from .plate_report import build_plate_report, summarize_for_agent
from .plan_patterns import PatternError, summarize
from .run_access import RESTRICTED, RunReader, redact_plan_view

logger = logging.getLogger(__name__)

# OpenRouter's OpenAI-compatible endpoint. Overridable so the same code can
# point at OpenAI proper, a local vLLM, or any other compatible server —
# nothing here depends on the provider.
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# A tool-calling model by default. The assistant's whole job is choosing a verb
# from a fixed catalog and filling in a schema, so reasoning depth matters far
# less than reliable structured output. Note that Nous Hermes models on
# OpenRouter do NOT advertise tool support — pointing this at one silently
# degrades to text-only replies with no proposals. Was z-ai/glm-5.2 until
# 2026-09-21, when the OpenRouter workspace guardrail stopped allowing it (a
# blocked slug 404s every turn); both deployed gateways already ran a DeepSeek
# flash model, so this default now matches them. It is a reasoning model —
# the empty-reply nudge below is what keeps a spent token budget from
# surfacing as a silent "…".
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
CLAUDE_CODE_MODEL = "claude-sonnet-5-5"

# What the operator may pick in the chat panel when OT2_ASSISTANT_MODELS is
# unset. An allowlist, never free text: the chat endpoint is reachable by
# whoever holds the claim, and an arbitrary slug could be a model with no tool
# support (text-only replies, no proposals), one the OpenRouter guardrail
# blocks, or one that bills far more per turn. OpenRouter slugs only — a
# custom OT2_ASSISTANT_BASE_URL gets just its configured model unless
# OT2_ASSISTANT_MODELS names others for that provider.
DEFAULT_MODEL_CHOICES: Tuple[str, ...] = (
    "deepseek/deepseek-v4.1-flash",
    "z-ai/glm-5.3-flash",
)

DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_S = 60.0
# One empty model completion is common with reasoning models after a tool
# round (they spend the token budget internally and return no content). A
# bounded nudge asks them to actually answer instead of completing as "…".
_EMPTY_REPLY_NUDGES = 2
_EMPTY_REPLY_NUDGE = (
    "Your last response was empty. Propose a plan with propose_plan, or tell "
    "the operator in one short paragraph why you cannot."
)

_SYSTEM_PROMPT = """\
You are the operator assistant for a single Opentrons {robot_model} liquid handler, \
reached through its gateway. You help with simple, single-robot operations on \
THIS robot only.

What you can do:
- Read the robot's state: status, deck layout, tip racks, loaded plate.
- Propose an ordered plan of control actions for the operator to review.
- Propose bookkeeping corrections when the recorded state and the physical \
deck disagree — `tips.mark` (these wells are full / empty), `tips.reset` (a \
fresh rack went in), `plate.load`, `well.update`, `deck.declare`. These are \
in the catalog like any other action. Propose the correction rather than \
describing it in prose and asking the operator to go and do it by hand.
- Module placement changes require an admin to approve and execute. Preserve \
all module placements when proposing ordinary plate or tip-record edits. The \
module tile has no placement editor; admins use the API or an approved chat plan.
- Use `platebalance.tare` to set a loaded plate's weight as the baseline; \
`platebalance.zero` is for the unloaded balance and has a limited zero range. \
`platebalance.tare` holds the command lock and waits up to 30 s for two fresh \
stable near-zero readings, with no gateway robot movement during that wait, \
resending tare and waiting again up to three times if the baseline settles \
off zero. It stops the plan if they are still not observed. Propose tare before aspirating; \
inspect its baseline result before proposing a separate dosing plan. Zero \
remains a non-idempotent serial write whose `sent_unconfirmed` result proves \
only that its command was sent. A read reports the weight actually observed. \
A cached status reading does not prove a plan measured weight. Balance well `blow_out` requires \
`details.platebalance.geometry.blow_out_enabled: true`, an explicit well \
location at least 2 mm above the measured rim, and a pre-set blow-out flow \
rate no faster than half the pipette's documented dispense default. Never \
use `in_place=true` as a balance workaround. Balance `touch_tip` is unavailable.
- Read current plan records to answer whether a proposal was approved, run, \
failed, or aborted. Use `list_plans` to find the plan and `get_plan` for its \
step results. Report only the status and results actually recorded; records \
are in memory and disappear on restart.
- Summarize balance results with `get_plate_report` when asked how a run went \
or for a plate summary or heatmap. Report the stats and name outlying and \
unweighed wells; tell the operator the interactive heatmap opens from the \
plan card's **Plate report** button. Pass a density only if the operator gave \
one; never assume water.

What you cannot do, and must never imply otherwise:
- For work repeated over wells (dispense to each well, weigh each well), \
propose with `for_each_well` — a step template plus the wells — and optional \
`prelude`/`epilogue` steps (tip pickup, tip drop). Never write a plate out as \
hundreds of steps: the gateway expands the pattern and the operator reviews \
the expansion. Put `{well}` where the well name goes; single-channel only.
- You cannot run anything. `propose_plan` creates a DRAFT. A human reviews, \
approves, and runs it in chat. Say a newly proposed draft awaits approval. \
For an existing plan, use its current gateway record. `executed` means all \
steps finished; `failed` or `aborted` may include skipped steps, so inspect \
each result before saying what ran. Never infer execution from earlier \
conversation or a cached device status.
- You cannot connect or disconnect the robot, pause or resume a run, or \
reconcile an unknown outcome. Operator-only actions include `startup`, \
`shutdown`, `pause`, `resume`, `stop`, `reconcile`. Everything `list_actions` returns \
is yours to propose — an action being an assertion about the physical world \
does not make it operator-only, because approving your draft is how the \
operator makes that assertion. Never tell the operator to perform an action \
by hand when you could propose it.
- You do not decide chemistry. Volumes, reagents, well maps and protocol \
design come from the operator or their project's protocol. If asked to choose \
one, decline and ask what they want.

How to work:
Use `get_equipment_docs` to understand API coverage, naming conventions and \
limitations (including Python methods that have no gateway endpoint).
1. Read the state first. A plan built without looking at the deck is a guess.
2. Check consumables before proposing pipetting — a rack with no fresh tips or \
an unloaded plate will fail at the first step.
Compare `details.mounted_tips` (the gateway's ledger) with \
`details.snapshot.pipettes.<mount>.has_tip` (the robot's own belief) when \
present. If the robot reports a tip the ledger does not, propose `drop_tip` \
with only the pipette before any `pick_up_tip`; it clears the robot's record \
even when the head is bare. If the ledger shows a tip the robot does not (a \
run after a stop starts with none) and the operator says a tip is on the head, \
propose `drop_tip` with `force_drop: true` and only the pipette; never assume it.
3. Use the exact argument names from `list_actions`; unknown keys are \
rejected. Address labware by `labware_nickname`: the observed run nickname or ID \
from status, else the declared deck slot. The setup recipe is not authoritative. Address pipettes by the \
recipe nickname, else the mount ("left" / "right"). `pick_up_tip` may omit \
the rack and position entirely — the gateway picks the next available tip \
from a tracked, size-compatible rack. To discard a tip into the waste, \
{trash_guidance} Address a temperature module by `module` \
(recipe nickname or deck slot). Omit `module` when exactly one temperature \
module is on the deck. `tempmod.set` starts the ramp and returns immediately \
— watch current vs target on the deck; it does not wait. \
`tempmod.deactivate` turns the module off.
For a declared balance plate, address wells by its declared slot (for example \
`"9"`). The compiled run labware may appear as an adapter without wells in \
the deck snapshot; the gateway resolves the wells from the exact declared \
plate definition. Balance dispense clearance is above the highest rim.
4. Propose the smallest plan that does what was asked. Explain each step in one \
short line.
5. If a request is ambiguous, out of scope, or unsafe, say so plainly instead \
of proposing something approximate. Never propose a reset or metadata correction \
just to bypass an interlock. Physical-state corrections require the operator's \
explicit observation; never infer that a tip or rack is fresh.
6. Preserve motion intent: force_direct=true omits the Z retract. Constant-height \
XY motion requires the destination Z to equal the current Z. Never silently \
replace a requested direct path with an arc or invent a clear path.
7. Write replies in light Markdown: **bold** for key values and well addresses, \
`backticks` for action and argument names, and a short bullet list for \
enumerations. No headings or tables — the chat window is narrow.
"""


class AssistantDisabled(Exception):
    """The assistant cannot run. ``reason`` is safe to show an operator."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AssistantCancelled(Exception):
    """The operator stopped this assistant turn."""


# Settings a `.env` file may supply. An allowlist, not a general loader, and
# the reason is specific to this deployment: the file lives at the repo root,
# which BOTH gateway instances share. A general loader would let it provide an
# instance-specific value — a host alias, one of the three state paths — to two
# robots at once, and shared state files corrupt each other. Everything that
# distinguishes one robot from the other stays in its NSSM service env, which
# a file can never reach.
ENV_FILE_KEYS = frozenset(
    {
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "OT2_ASSISTANT_ENABLED",
        "OT2_ASSISTANT_MODEL",
        "OT2_ASSISTANT_MODELS",
        "OT2_ASSISTANT_BASE_URL",
        "OT2_ASSISTANT_MAX_TOKENS",
        "OT2_ASSISTANT_TIMEOUT_S",
    }
)


def env_file_candidates() -> List[Path]:
    """Where a ``.env`` is looked for, in order.

    ``OT2_ENV_FILE`` wins when set. Otherwise the working directory, which is
    the repo root under NSSM (``AppDirectory``) and when running from a
    checkout — then a package-relative guess so an editable install started
    from elsewhere still finds it.
    """
    explicit = os.environ.get("OT2_ENV_FILE")
    if explicit:
        return [Path(explicit)]
    # Deduped: run from a checkout — which is how the NSSM services run, with
    # AppDirectory set to the repo — and both candidates are the same file.
    # /assistant/health publishes this list, and showing one path twice reads
    # like a bug to whoever is trying to work out where to put their key.
    seen: Dict[Path, None] = {}
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env"):
        seen.setdefault(candidate.resolve() if candidate.parent.exists() else candidate, None)
    return list(seen)


def load_env_file(path: Optional[Path] = None) -> Dict[str, str]:
    """Allowlisted settings from a ``.env``, or ``{}`` if there is none.

    Deliberately does **not** mutate ``os.environ``: the caller uses this as a
    *fallback*, so a real environment variable always wins and the precedence
    is visible at the point it matters rather than depending on import order.

    Not cached, on purpose — it is read per request, so dropping a key into the
    file takes effect on the next poll with no restart. That is the whole point
    of having it: `nssm set AppEnvironmentExtra` replaces the entire variable
    block, and getting that wrong wipes the state paths that keep two robots
    from overwriting each other's plate and tip records.

    Malformed lines are skipped rather than raising. A typo in an optional
    config file must not take the gateway down.
    """
    paths = [path] if path is not None else env_file_candidates()
    for candidate in paths:
        try:
            if not candidate.is_file():
                continue
            found: Dict[str, str] = {}
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].lstrip()
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                key = key.strip()
                if key not in ENV_FILE_KEYS:
                    continue
                value = value.strip().strip('"').strip("'")
                if value:
                    found[key] = value
            return found
        except OSError as exc:  # unreadable file is not a reason to fail
            logger.warning("could not read %s: %s", candidate, exc)
    return {}


@dataclass(frozen=True)
class AssistantConfig:
    enabled: bool
    api_key: Optional[str]
    model: str
    base_url: str
    max_tokens: int
    timeout_s: float

    # Where the key came from, for /assistant/health. Answers the only question
    # an operator asks when the bubble stays hidden: "did it see my key?"
    key_source: Optional[str] = None
    configuration_error: Optional[str] = None
    # Other models an operator may switch to per turn. ``model`` is always
    # allowed as well; see ``choices``.
    alternatives: Tuple[str, ...] = ()
    claude_code_path: Optional[str] = None
    claude_code_ready: bool = False

    @property
    def choices(self) -> Tuple[str, ...]:
        """Every model a request may name: the configured one first."""
        return tuple(dict.fromkeys((self.model, *self.alternatives)))

    def with_model(self, requested: Optional[str]) -> "AssistantConfig":
        """This config answering with ``requested``, or unchanged for None.

        Raises ``ValueError`` for a model outside ``choices`` — the caller
        refuses the request rather than quietly using the default, so the
        operator never believes one model answered when another did.
        """
        if requested is None or requested == self.model:
            return self
        if requested not in self.choices:
            raise ValueError(f"model {requested!r} is not offered by this gateway")
        return replace(self, model=requested)

    @classmethod
    def from_env(cls) -> "AssistantConfig":
        from_file = load_env_file()

        def setting(key: str, default: Optional[str] = None) -> Optional[str]:
            """Environment first, then the file, then the default."""
            value = os.environ.get(key)
            if value:
                return value
            return from_file.get(key, default)

        key = None
        source = None
        key_name = None
        for origin, values in (("environment", os.environ), ("file", from_file)):
            for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
                if values.get(name):
                    key, source, key_name = values[name], origin, name
                    break
            if key:
                break
        configuration_error = None
        base_url = setting("OT2_ASSISTANT_BASE_URL")
        if key_name == "OPENAI_API_KEY" and not base_url:
            configuration_error = "OPENAI_API_KEY requires explicit OT2_ASSISTANT_BASE_URL and a model supported by that provider"
        max_tokens, timeout_s = DEFAULT_MAX_TOKENS, DEFAULT_TIMEOUT_S
        try:
            max_tokens = int(setting("OT2_ASSISTANT_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
            if max_tokens <= 0:
                raise ValueError
        except (TypeError, ValueError):
            configuration_error = "OT2_ASSISTANT_MAX_TOKENS must be a positive integer"
        try:
            timeout_s = float(setting("OT2_ASSISTANT_TIMEOUT_S", str(DEFAULT_TIMEOUT_S)))
            if not math.isfinite(timeout_s) or timeout_s <= 0:
                raise ValueError
        except (TypeError, ValueError):
            configuration_error = "OT2_ASSISTANT_TIMEOUT_S must be a positive finite number"
        listed = setting("OT2_ASSISTANT_MODELS")
        configured_claude = os.environ.get("OT2_ASSISTANT_CLAUDE_PATH")
        claude_path = shutil.which(configured_claude) if configured_claude else None
        claude_ready = bool(claude_path and claude_code_authenticated(claude_path))
        provider_ready = bool(key and (key_name != "OPENAI_API_KEY" or base_url))
        if listed:
            alternatives = tuple(
                m for item in listed.split(",") if (m := item.strip())
                and ((m == CLAUDE_CODE_MODEL and claude_ready)
                     or (m != CLAUDE_CODE_MODEL and provider_ready))
            )
        elif not base_url and provider_ready:
            alternatives = (*DEFAULT_MODEL_CHOICES, *((CLAUDE_CODE_MODEL,) if claude_ready else ()))
        else:
            alternatives = (CLAUDE_CODE_MODEL,) if claude_ready else ()
        model_default = DEFAULT_MODEL if provider_ready else CLAUDE_CODE_MODEL if configured_claude else DEFAULT_MODEL
        return cls(
            enabled=(setting("OT2_ASSISTANT_ENABLED", "true") or "true").lower()
            not in {"0", "false", "no", "off"},
            api_key=key,
            model=setting("OT2_ASSISTANT_MODEL", model_default) or model_default,
            base_url=base_url or DEFAULT_BASE_URL,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            key_source=source,
            configuration_error=configuration_error,
            alternatives=alternatives,
            claude_code_path=claude_path,
            claude_code_ready=claude_ready,
        )

    def unavailable_reason(self) -> Optional[str]:
        """Why the assistant cannot run, or None when it can."""
        if not self.enabled:
            return "assistant disabled (OT2_ASSISTANT_ENABLED=0)"
        if self.configuration_error and (
            self.model != CLAUDE_CODE_MODEL
            or not self.configuration_error.startswith("OPENAI_API_KEY requires")
        ):
            return self.configuration_error
        if self.model == CLAUDE_CODE_MODEL:
            if not self.claude_code_path:
                return "Claude Code CLI is not configured (set OT2_ASSISTANT_CLAUDE_PATH)"
            if not self.claude_code_ready:
                return "Claude Code is not authenticated for the gateway service account"
            return None
        if not self.api_key:
            return "no API key configured (set OPENROUTER_API_KEY)"
        try:
            import openai  # noqa: F401
        except ImportError:
            return "the 'openai' package is not installed in this environment"
        return None


def _tool_schemas() -> List[Dict[str, Any]]:
    """The assistant's entire capability, as OpenAI-style tool definitions.

    Reads plus ``propose_plan``. There is deliberately no approve, execute or
    abort tool — those are claim-gated and the assistant holds no claim, so
    offering them would only produce refusals and tempt the model to report
    work it cannot start.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "get_equipment_docs",
                "description": (
                    "Read the equipment API guide: capabilities, schemas, units, "
                    "agent boundaries and remaining Python API gaps. "
                    "Does not contact the robot."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_status",
                "description": (
                    "Health, activity, components, and which actions the robot "
                    "will currently accept. Start here."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_deck",
                "description": "The configured robot deck: labware per slot and its provenance.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_consumables",
                "description": (
                    "Tip racks with per-tip state, the loaded plate with per-well "
                    "samples, mounted tips, and pipette channel counts."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_actions",
                "description": (
                    "The catalog a plan step may use, with each action's argument "
                    "schema. Read before proposing."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_plan",
                "description": (
                    "Propose an ordered plan for the operator to review. Creates a "
                    "DRAFT — it does not run. The operator approves and runs it "
                    "in the panel. For work repeated over wells, use for_each_well "
                    "(with optional prelude/epilogue) instead of writing every step: "
                    "the gateway expands it and the operator reviews the expansion."
                ),
                "parameters": PROPOSAL_PARAMETERS,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_plan",
                "description": "Read one plan and its step results, including any balance weight. No robot I/O.",
                "parameters": {
                    "type": "object",
                    "properties": {"plan_id": {"type": "string"}},
                    "required": ["plan_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_plans",
                "description": "Read plans and their step results, including balance weights. No robot I/O.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_plate_report",
                "description": (
                    "Summarize balance results of one or more executed plans on a 96-well grid: "
                    "per-well mass, deviation from the mean, mean/SD/CV, and wells not weighed. "
                    "Combine a run split across plans by listing them in run order. The operator "
                    "opens the interactive heatmap from the plan card. No robot I/O."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "labware": {"type": "string", "description": "Labware nickname or slot; default is the first one dispensed into."},
                        "density_g_per_ml": {"type": "number", "description": "Only if the operator gave one; enables implied volume."},
                    },
                    "required": ["plan_ids"],
                },
            },
        },
    ]


class Assistant:
    """One conversation turn's worth of tool-calling over a single OT-2."""

    # Bounded so a confused model cannot spin against the robot's read path or
    # burn tokens indefinitely — but generous, because the budget counts model
    # *completions*, not tool calls, and the deployed model tends to make one
    # call per completion. The system prompt itself demands status +
    # consumables + list_actions reads before a propose, the client re-sends
    # history as plain text (previous tool results are never replayed), and
    # the final no-tool-call reply costs a round of its own — so a single
    # honest turn routinely needs 5-6 rounds, plus repair attempts after
    # validation errors.
    MAX_TOOL_ROUNDS = 16

    def __init__(self, service: Any, plans: PlanStore, config: AssistantConfig,
                 ensure_authorized: Optional[Callable[[], None]] = None,
                 reader: Optional[RunReader] = None) -> None:
        self._service = service
        self._plans = plans
        # Whose eyes the assistant reads run data with: the same rule as the
        # API routes, so asking the assistant is never a way around them.
        self._reader = reader or RunReader(unrestricted=True)
        self._config = config
        self._ensure_authorized = ensure_authorized
        self._cancel_event = threading.Event()
        self._client: Any = None

    def cancel(self) -> None:
        self._cancel_event.set()
        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                logger.warning("could not close canceled assistant client (%s)", type(exc).__name__)

    def _ensure_active(self) -> None:
        if self._cancel_event.is_set():
            raise AssistantCancelled()
        if self._ensure_authorized:
            self._ensure_authorized()

    # -- tools -------------------------------------------------------------

    def _tools(self) -> Dict[str, Callable[[Dict[str, Any]], Any]]:
        return {
            "get_equipment_docs": lambda _a: equipment_documentation(),
            "get_status": lambda _a: self._service.get_status().model_dump(mode="json"),
            "get_deck": lambda _a: self._service.get_status()
            .details.get("snapshot", {})
            .get("deck", {}),
            "get_consumables": lambda _a: self._consumables(),
            "list_actions": lambda _a: self._actions(),
            "propose_plan": self._propose,
            "get_plate_report": lambda a: summarize_for_agent(build_plate_report(
                [self._readable_plan(pid) for pid in a["plan_ids"]],
                labware=a.get("labware"), density_g_per_ml=a.get("density_g_per_ml"),
            )),
            "get_plan": lambda a: self._plan_for_reader(self._plans.get(a["plan_id"])),
            "list_plans": lambda _a: [
                {
                    "plan_id": plan.plan_id,
                    "status": plan.status,
                    "created_at": plan.created_at.isoformat(),
                    "created_by": plan.created_by,
                    "actions": [step.action for step in plan.steps],
                    "halt_reason": (plan.halt_reason if self._reader.can_read_plan(plan)
                                    else (RESTRICTED if plan.halt_reason else None)),
                    "results": [
                        {"step": index + 1, "action": result.action,
                         "outcome": result.outcome,
                         **({"message": result.message, "reading": result.reading,
                             "balance_operation": result.balance_operation}
                            if self._reader.can_read_plan(plan) else {})}
                        for index, result in enumerate(plan.results)
                    ],
                    "readings": [
                        {"step": index + 1, **result.reading}
                        for index, result in enumerate(plan.results)
                        if result.reading is not None and self._reader.can_read_plan(plan)
                    ],
                    **({} if self._reader.can_read_plan(plan) else {"redacted": True}),
                }
                for plan in self._plans.list()[:20]
            ],
        }

    def _plate_reports_for_context(self) -> List[Dict[str, Any]]:
        """Per-plan plate summaries for recent plans that recorded a weighing."""
        out: List[Dict[str, Any]] = []
        for plan in self._plans.list()[:10]:
            if not any(r.reading for r in plan.results) or not self._reader.can_read_plan(plan):
                continue
            out.append({"plan_id": plan.plan_id, **summarize_for_agent(build_plate_report([plan]))})
        return out

    def _plan_for_reader(self, plan: Any) -> Dict[str, Any]:
        view = plan.model_dump(mode="json")
        return view if self._reader.can_read_plan(plan) else redact_plan_view(view)

    def _readable_plan(self, plan_id: str) -> Any:
        plan = self._plans.get(plan_id)
        if not self._reader.can_read_plan(plan):
            raise PermissionError(
                f"plan {plan_id}'s data is only open to its approver and members of its ELN "
                "project; tell the user to ask them or an admin")
        return plan

    def _consumables(self) -> Dict[str, Any]:
        details = self._service.get_status().details
        robot = details.get("robot") or {}
        return {
            "tip_racks": details.get("tip_racks"),
            "loaded_plate": details.get("loaded_plate"),
            "mounted_tips": details.get("mounted_tips"),
            "pipette_channels": details.get("pipette_channels"),
            "modules": robot.get("modules"),
        }

    @staticmethod
    def _actions() -> Dict[str, Any]:
        return action_catalog()

    def _propose(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Create a draft. Validation errors are returned to the model, not
        raised — its next turn can repair the step instead of the operator
        seeing a stack trace."""
        try:
            request = PlanCreateRequest(
                steps=args.get("steps") or [],
                prelude=args.get("prelude") or [],
                for_each_well=args.get("for_each_well"),
                epilogue=args.get("epilogue") or [],
                created_by="assistant",
            )
            steps, pattern = compile_proposal(
                request, grid_for=self._service.labware_grid,
                channels_for=self._service._channels_for)
            plan = self._plans.create(steps, created_by="assistant", pattern=pattern)
        except (StepValidationError, PatternError, TypeError, ValueError) as exc:
            # Logged as well as returned to the model: otherwise the reason a
            # draft was refused exists only in one browser tab.
            logger.warning("propose_plan refused (%d steps): %s", len(args.get("steps") or []), exc)
            return {"error": str(exc), "hint": "fix the step and call propose_plan again"}
        return {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "step_count": len(plan.steps),
            # The full expansion is on the card; the model gets the summary
            # (a 500-step list would only crowd its context).
            "steps": ([{"action": s.action, "args": s.args} for s in plan.steps]
                      if pattern is None else None),
            "pattern_summary": summarize(pattern, plan.steps) if pattern else None,
            "note": "Draft created. The operator must approve and run it in chat.",
        }

    @staticmethod
    def _message_text(message: Any) -> str:
        """Visible assistant text only — never reasoning / chain-of-thought."""
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for part in content:
                if isinstance(part, str) and part.strip():
                    parts.append(part)
                    continue
                text = (
                    part.get("text")
                    if isinstance(part, dict)
                    else getattr(part, "text", None)
                )
                if isinstance(text, str) and text.strip():
                    parts.append(text)
            joined = "".join(parts).strip()
            if joined:
                return joined
        refusal = getattr(message, "refusal", None)
        if isinstance(refusal, str) and refusal.strip():
            return refusal
        return ""

    def _chat_claude_events(self, messages: List[Dict[str, str]]) -> Iterator[Dict[str, Any]]:
        """Supply read-only context to Claude Code and validate its draft here."""
        tools = self._tools()
        used: List[str] = []
        reads: Dict[str, Any] = {}
        yield {"type": "thinking", "round": 1}
        for index, name in enumerate((
            "get_status", "get_deck", "get_consumables", "list_actions", "get_equipment_docs",
        ), start=1):
            self._ensure_active()
            event_id = f"1:claude-read-{index}"
            yield {"type": "tool_started", "id": event_id, "name": name}
            reads[name] = tools[name]({})
            used.append(name)
            yield {"type": "tool_finished", "id": event_id, "name": name,
                   "success": True, "error": None}

        self._ensure_active()
        yield {"type": "thinking", "round": 2}
        system_prompt = _SYSTEM_PROMPT.format(
            robot_model=PROFILE.model,
            trash_guidance=(
                "Flex has no assumed fixed trash: register a physically present bin or name an explicit drop well."
                if IS_FLEX else "propose drop_tip with only the pipette when its fixed trash is registered."
            ),
        ) + (
            "\nFor this turn, the gateway has supplied the read results below. "
            "You have no tools. Return a JSON reply and an ordered steps array. "
            "Use an empty steps array if you cannot safely propose a plan. "
            "A nonempty steps array creates only a draft, subject to gateway validation "
            "and operator approval. Say clearly when a draft awaits approval."
        )
        assert self._config.claude_code_path is not None
        try:
            answer = run_claude_code(
                self._config.claude_code_path, system_prompt,
                {"messages": messages, "reads": reads,
                 "current_plans": tools["list_plans"]({})[:10],
                 # No tool calls on this path, so the plate summary the tool
                 # path gets from get_plate_report is precomputed per plan.
                 "plate_reports": self._plate_reports_for_context()},
                self._config.timeout_s, self._cancel_event,
            )
        except InterruptedError as exc:
            raise AssistantCancelled() from exc
        self._ensure_active()
        plan_id: Optional[str] = None
        reply = answer["reply"].strip()
        if answer["steps"] or answer.get("for_each_well"):
            self._ensure_active()
            used.append("propose_plan")
            event_id = "2:claude-proposal"
            yield {"type": "tool_started", "id": event_id, "name": "propose_plan"}
            self._ensure_active()
            proposal = self._propose({k: answer.get(k) for k in
                                      ("steps", "prelude", "for_each_well", "epilogue")})
            error = proposal.get("error")
            yield {"type": "tool_finished", "id": event_id, "name": "propose_plan",
                   "success": error is None, "error": error}
            if error:
                reply = f"I could not create that draft: {error}"
            else:
                plan_id = proposal["plan_id"]
                if not reply:
                    reply = "I proposed a draft for your review and approval."
        yield {"type": "complete", "result": {
            "reply": reply or "Claude Code returned no reply or draft.",
            "tools_used": used,
            "plan_id": plan_id,
            "model": CLAUDE_CODE_MODEL,
        }}

    # -- the turn ----------------------------------------------------------

    def chat_events(self, messages: List[Dict[str, str]]) -> Iterator[Dict[str, Any]]:
        """Run one turn and yield operator-safe progress events.

        The conversation may be any length; ``fit_history`` sends the model
        the most recent part that fits its context, newest message always.

        Model requests can take tens of seconds and a useful turn commonly
        needs several of them. Exposing the tool boundary lets the UI show
        truthful progress without streaming the model's private reasoning.
        The final event always has type ``complete`` unless an exception
        escapes to the HTTP layer.
        """
        reason = self._config.unavailable_reason()
        if reason:
            raise AssistantDisabled(reason)
        messages = fit_history(messages)

        if self._config.model == CLAUDE_CODE_MODEL:
            yield from self._chat_claude_events(messages)
            return

        from openai import OpenAI

        client = OpenAI(
            base_url=self._config.base_url,
            api_key=self._config.api_key,
            timeout=self._config.timeout_s,
        )
        self._client = client
        try:
            tools = self._tools()
            convo: List[Dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT.format(
                robot_model=PROFILE.model,
                trash_guidance=("Flex has no assumed fixed trash: register a physically present bin or name an explicit drop well."
                                if IS_FLEX else "propose drop_tip with only the pipette when its fixed trash is registered."),
            )}]
            # History replays plain chat text, not earlier tool results. Give
            # each turn the current plan states so an old draft claim cannot
            # override an execution that happened between chat messages.
            recent = self._tools()["list_plans"]({})[:10]
            convo.append({"role": "system", "content": (
                "Current gateway plan records (ephemeral; use get_plan for full details): "
                + json.dumps(recent, default=str)
                + (". No plans are currently held; do not infer their execution history."
                   if not recent else "")
            )})
            convo += [{"role": m["role"], "content": m["content"]} for m in messages]

            used: List[str] = []
            plan_id: Optional[str] = None
            empty_nudges = 0

            for round_index in range(self.MAX_TOOL_ROUNDS):
                self._ensure_active()
                yield {"type": "thinking", "round": round_index + 1}
                response = client.chat.completions.create(
                    model=self._config.model,
                    messages=convo,
                    tools=_tool_schemas(),
                    max_tokens=self._config.max_tokens,
                )
                choice = response.choices[0].message
                self._ensure_active()
                calls = getattr(choice, "tool_calls", None) or []
                if not calls:
                    text = self._message_text(choice)
                    if not text and empty_nudges < _EMPTY_REPLY_NUDGES:
                        empty_nudges += 1
                        convo.append({"role": "user", "content": _EMPTY_REPLY_NUDGE})
                        continue
                    yield {
                        "type": "complete",
                        "result": {
                            "reply": text
                            or (
                                "The model finished after reading the robot but "
                                "returned no reply. Try the request once more, or "
                                "ask for a smaller step."
                            ),
                            "tools_used": used,
                            "plan_id": plan_id,
                            "model": response.model,
                        },
                    }
                    return

                convo.append(
                    {
                        "role": "assistant",
                        "content": choice.content or "",
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {
                                    "name": c.function.name,
                                    "arguments": c.function.arguments,
                                },
                            }
                            for c in calls
                        ],
                    }
                )
                for call in calls:
                    self._ensure_active()
                    name = call.function.name
                    used.append(name)
                    event_id = f"{round_index + 1}:{call.id}"
                    yield {
                        "type": "tool_started",
                        "id": event_id,
                        "name": name,
                    }
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    fn = tools.get(name)
                    if fn is None:
                        result: Any = {"error": f"unknown tool {name!r}"}
                    else:
                        try:
                            self._ensure_active()
                            result = fn(args)
                        except AssistantCancelled:
                            raise
                        except Exception as exc:  # surfaced to the model, not the operator
                            logger.warning("assistant tool %s failed: %s", name, exc)
                            result = {"error": str(exc)}
                    tool_error = (
                        str(result["error"])
                        if isinstance(result, dict) and result.get("error")
                        else None
                    )
                    yield {
                        "type": "tool_finished",
                        "id": event_id,
                        "name": name,
                        "success": tool_error is None,
                        "error": tool_error,
                    }
                    if name == "propose_plan" and isinstance(result, dict):
                        plan_id = result.get("plan_id") or plan_id
                    convo.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result, default=str),
                        }
                    )

            # Out of rounds with the model still calling tools. Say so rather than
            # inventing a summary of work whose outcome we did not see.
            yield {
                "type": "complete",
                "result": {
                    "reply": (
                        "I wasn't able to finish that within my tool-call budget. "
                        "Try asking for one smaller step."
                    ),
                    "tools_used": used,
                    "plan_id": plan_id,
                    "model": self._config.model,
                },
            }
        finally:
            self._client = None
            client.close()

    def chat(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """Compatibility wrapper for non-streaming callers."""
        events = self.chat_events(messages)
        try:
            for event in events:
                if event["type"] == "complete":
                    return event["result"]
        finally:
            events.close()
        raise RuntimeError("assistant turn ended without a completion")


#: How much conversation text goes to the model per turn (characters; ~4 per
#: token, so ~50k tokens), leaving room for the system prompt, live context
#: and tool results.
HISTORY_CHAR_BUDGET = int(os.environ.get("OT2_ASSISTANT_HISTORY_CHARS", "200000"))


def fit_history(messages: List[Dict[str, str]], budget: Optional[int] = None) -> List[Dict[str, str]]:
    """The most recent messages whose text fits ``budget``, oldest first.

    The newest message is always kept whole, however long. When older ones
    are left out, a note says so, so the model does not answer as if it had
    seen the whole conversation.
    """
    budget = HISTORY_CHAR_BUDGET if budget is None else budget
    kept: List[Dict[str, str]] = []
    used = 0
    for message in reversed(messages):
        size = len(message.get("content") or "")
        if kept and used + size > budget:
            break
        kept.append(message)
        used += size
    kept.reverse()
    dropped = len(messages) - len(kept)
    if dropped:
        kept.insert(0, {"role": "user", "content": (
            f"[{dropped} earlier message{'s' if dropped != 1 else ''} of this conversation "
            "were left out to fit the model's context. Ask the operator if you need them.]")})
    return kept


_STEP_SCHEMA = {
    "type": "object",
    "properties": {"action": {"type": "string"}, "args": {"type": "object"}},
    "required": ["action"],
}

#: One proposal schema for the tool-calling path and the Claude reply schema.
PROPOSAL_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "description": "Ordered steps drawn from list_actions. Leave empty when using for_each_well.",
            "items": _STEP_SCHEMA,
        },
        "prelude": {
            "type": "array",
            "description": "Steps run once before the per-well loop (e.g. pick_up_tip).",
            "items": _STEP_SCHEMA,
        },
        "for_each_well": {
            "type": "object",
            "description": (
                "A step template applied to each well of one labware. In args, {well} is the "
                "well name, {row} its row letter, {column}/{index} integers. Single-channel "
                "pipettes only. wells: a range like 'A1:H12' (walked in `order`) or a list. "
                "Give a step an `id` to target it in overrides[well][id] = partial args."
            ),
            "properties": {
                "labware_nickname": {"type": "string"},
                "wells": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "order": {"type": "string", "enum": ["column", "row"]},
                "steps": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"action": {"type": "string"}, "args": {"type": "object"},
                                   "id": {"type": "string"}},
                    "required": ["action"],
                }},
                "overrides": {"type": "object"},
            },
            "required": ["labware_nickname", "wells", "steps"],
        },
        "epilogue": {
            "type": "array",
            "description": "Steps run once after the loop (e.g. drop_tip).",
            "items": _STEP_SCHEMA,
        },
    },
}


class AssistantMessage(BaseModel):
    role: Literal["user", "assistant"]
    # No length cap: a long reply (a big plan) or a pasted protocol must never
    # make the next turn fail. What reaches the model is fitted by
    # fit_history() instead.
    content: str = Field(..., min_length=1)


class AssistantChatRequest(BaseModel):
    """One turn plus the conversation so far.

    History is re-sent by the client rather than held server-side: the gateway
    already keeps per-process state it must reason about (claims, plans, tip
    tracking), and a chat transcript is the one thing here that nothing else
    depends on. Bounded so a long session cannot grow a request without limit.
    """

    messages: List[AssistantMessage] = Field(..., min_length=1)
    # One of /assistant/health's ``models``; omitted means the configured
    # default. Anything else is refused with 422, never substituted.
    model: Optional[str] = Field(None, min_length=1, max_length=200)
    request_id: Optional[str] = Field(None, pattern=r"^[a-f0-9]{32}$")


class AssistantCancelRequest(BaseModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
