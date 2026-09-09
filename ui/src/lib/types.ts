/**
 * Wire types for the gateway's STATUS_SPEC v1.1 surface, plus the
 * snapshot wrapper the ported dashboard components expect.
 *
 * `GatewaySnapshot` mirrors the shape of the ac-organic-lab dashboard's
 * `EquipmentSnapshot` closely enough that components ported from there
 * (DeckPanel, ControlPanel, …) run unchanged: the gateway's `/status`
 * envelope is the `status` field, and the fetch bookkeeping
 * (`fetch_error`, `latency_ms`, `fetched_at`) is synthesized client-side
 * by `useGatewayStatus` instead of by the dashboard aggregator.
 */

export type EquipmentState =
  | "ready"
  | "busy"
  | "requires_init"
  | "degraded"
  | "dry_run"
  | "error"
  | "e_stop"
  | "unknown";

export interface ComponentStatus {
  connected: boolean;
  state: string;
  message?: string | null;
  last_event_at?: string | null;
}

export interface MetricValue {
  value: number | string | boolean;
  unit?: string | null;
  timestamp?: string | null;
}

export interface ErrorInfo {
  code?: string | null;
  message: string;
  severity: "info" | "warning" | "error" | "critical";
  timestamp: string;
}

export interface EquipmentStatus {
  protocol_version: string;
  equipment_id: string;
  equipment_name: string;
  equipment_kind: string;
  equipment_version?: string | null;
  host?: string | null;
  equipment_status: EquipmentState;
  message?: string | null;
  required_actions?: string[];
  device_time?: string;
  uptime_seconds?: number | null;
  components?: Record<string, ComponentStatus>;
  metrics?: Record<string, MetricValue>;
  last_error?: ErrorInfo | null;
  allowed_actions?: string[];
  details?: Record<string, unknown>;
}

/** Client-side transport failure (the browser could not reach the gateway).
 *  Shape-compatible with the dashboard's aggregator `fetch_error`. */
export interface FetchError {
  kind: string;
  http_status?: number | null;
}

export interface GatewaySnapshot {
  id: string;
  name: string;
  kind: string;
  status: EquipmentStatus;
  fetch_error: FetchError | null;
  latency_ms: number | null;
  fetched_at: string;
}

// ---------------------------------------------------------------------------
// Deck / modules (details.snapshot.deck, details.robot.modules)
// ---------------------------------------------------------------------------

/** One well of the orchestrator-tracked plate (deck slot `labware.wells`). */
export interface WellSample {
  well: string;
  sample_id?: string | null;
  volume_ul?: number | null;
  notes?: string | null;
}

/** One slot of the gateway's normalized deck (details.snapshot.deck.slots). */
export interface DeviceDeckSlot {
  labware: {
    kind: string;
    load_name: string;
    display_name?: string | null;
    is_tiprack?: boolean;
    rows?: number | null;
    columns?: number | null;
    plate_id?: string | null;
    /** Tracked plate samples the deck folds onto this slot. */
    wells?: WellSample[] | null;
    /** Exact schema-2 definition supplied with an operator-declared custom
     * labware item. It is authoritative for its own preview geometry. */
    definition?: unknown | null;
    /** The setup recipe's name for this labware — the key `details.tip_racks`
     *  and `/control/*` use. Stamped per slot so it survives run/REPL
     *  precedence, unlike `display_name`. */
    nickname?: string | null;
  } | null;
  module: {
    module_name: string;
    status?: string | null;
    serial_number?: string | null;
  } | null;
  slot_state: "empty" | "declared" | "occupied" | "in_use" | "mismatch";
  source: "run" | "repl" | "declared" | "empty";
  /** The operator/recipe declaration on this slot, set whatever won the merge —
   *  including once a run/REPL source occupies it, when `slot_state` no longer
   *  says "declared". The declared-layout round-trip depends on this. */
  declared?: { kind: string; load_name: string } | null;
  /** As `declared`, when the declaration is a sticky module rather than labware. */
  declared_module?: { module_name: string } | null;
}

export interface DeviceDeck {
  source: "run" | "repl" | "declared" | "empty";
  slots: Record<string, DeviceDeckSlot>;
  timestamp?: string;
}

/** One attached hardware module from `details.robot.modules` — live telemetry
 *  straight off the robot. Distinct from the deck's declared module, which is
 *  operator intent; the panel pairs the two by serial or module family. */
export interface RobotModule {
  model: string;
  type: string;
  serial?: string | null;
  id?: string | null;
  status?: string | null;
  current_temperature?: number | null;
  target_temperature?: number | null;
}

// ---------------------------------------------------------------------------
// Claims (STATUS_SPEC v1.1)
// ---------------------------------------------------------------------------

export interface ClaimedBy {
  session_id: string;
  owner: string;
  expires_at: string;
}

export interface ClaimResponse {
  claim_token: string;
  heartbeat_interval_s: number;
  expires_at: string;
}

// ---------------------------------------------------------------------------
// Labware catalog (GET /labware)
// ---------------------------------------------------------------------------

export interface LabwareSummary {
  load_name: string;
  display_name: string;
  display_category?: string;
  is_tiprack?: boolean;
  rows?: number;
  columns?: number;
  well_count?: number;
  well_volume_ul?: number | null;
  version?: number;
  namespace?: string;
  /** Manufacturer metadata from the definition's schema-2 `brand` object.
   *  Optional: an older gateway deploy may not serve these yet. */
  vendor?: string | null;
  product_numbers?: string[];
  product_links?: string[];
  source?: string;
}

// ---------------------------------------------------------------------------
// Agent-proposed plans (see gateway/plans.py)
//
// An agent may create and revise a plan. Approving and running it are
// claim-gated, so they happen here, in the operator's browser, and nowhere
// else.
// ---------------------------------------------------------------------------

export type PlanStatus =
  | "draft"
  | "approved"
  | "executing"
  | "executed"
  | "failed"
  | "aborted";

export type StepOutcome = "pending" | "ok" | "failed" | "skipped";

export interface PlanStep {
  action: string;
  args: Record<string, unknown>;
}

export interface StepResult {
  action: string;
  outcome: StepOutcome;
  message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface StepApproval {
  owner: string;
  session_id: string;
  step_hash: string;
  approved_at: string;
  expires_at: string;
}

export interface Plan {
  plan_id: string;
  steps: PlanStep[];
  /** Digest of the exact step list. Approving sends this back, so an edit
   *  between render and click is caught instead of silently approved. */
  step_hash: string;
  status: PlanStatus;
  created_at: string;
  created_by: string;
  results: StepResult[];
  /** Not a 'run authorization' (AGENTIC_ELN_DESIGN.md §12) — a device-local
   *  operator approval of one ad-hoc step list. */
  approval: StepApproval | null;
  halt_reason: string | null;
  /** Steps that cannot be safely repeated after a transport loss. */
  non_idempotent_actions: string[];
  /** Whether the gateway would run this right now... */
  executable: boolean;
  /** ...and if not, why — same string the agent sees. */
  blocked_reason: string | null;
}

// --- Optional chat assistant (see gateway/assistant.py) ---------------------

export interface AssistantMessage {
  role: "user" | "assistant";
  content: string;
  /** Tool calls made while producing this reply, retained with the transcript. */
  tools?: AssistantToolProgress[];
  /** Set when this turn produced a draft plan, so the bubble can point at it. */
  planId?: string;
  /** The drafted steps, shown read-only inline. The panel remains authoritative;
   *  this is a preview so the operator sees what was proposed in context. */
  steps?: PlanStep[];
}

export interface AssistantHealth {
  configured: boolean;
  reason: string | null;
  model: string | null;
}

export interface AssistantReply {
  reply: string;
  tools_used: string[];
  plan_id: string | null;
  model: string;
}

export interface AssistantToolProgress {
  id: string;
  name: string;
  status: "running" | "succeeded" | "failed";
  error?: string;
}

export type AssistantProgressEvent =
  | { type: "thinking"; round: number }
  | { type: "tool_started"; id: string; name: string }
  | {
      type: "tool_finished";
      id: string;
      name: string;
      success: boolean;
      error: string | null;
    }
  | { type: "complete"; result: AssistantReply }
  | { type: "error"; message: string };
