import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  abortPlan,
  approvePlan,
  assistantChatStream,
  cancelAssistantChat,
  deletePlan,
  executePlan,
  getAssistantHealth,
  getPlan,
  listPlans,
} from "../lib/api";
import { moveWindow, resizeWindow } from "../lib/floating-window";
import { AssistantMarkdown } from "../lib/assistant-markdown";
import type { Corner, WindowRect } from "../lib/floating-window";
import type { ClaimState } from "../lib/use-claim";
import type {
  AssistantMessage,
  AssistantProgressEvent,
  AssistantToolProgress,
  GatewaySnapshot,
  Plan,
  PlanStep,
} from "../lib/types";

/**
 * Optional chat popup for simple operations on THIS OT-2.
 *
 * When chat is unconfigured, the popup still appears for externally proposed
 * plans so the operator retains one place to review and run them.
 *
 * The assistant can only propose. What it drafts renders here as a plan card
 * the operator can approve and run **in the chat** — the same claim-gated
 * calls the gateway's human-approval surface, with the same two review
 * properties preserved:
 *
 *  - **Approve sends the hash of the steps this card is showing.** The card
 *    renders from the live plan (re-fetched, not the proposal-time preview),
 *    so what is approved is what is on screen; a plan revised elsewhere gets
 *    a 409 and a re-read.
 *  - **Approve and Run stay two clicks.**
 */

const STORAGE_KEY = "ot2-assistant-thread";
// Session-scoped like the thread. Both gateways share the edge's origin, so a
// saved pick is only honoured if this gateway still offers it.
const MODEL_STORAGE_KEY = "ot2-assistant-model";
const MAX_KEPT = 20;
const RESIZE_CORNERS = [
  { corner: "nw", position: "left-0 top-0", cursor: "cursor-nwse-resize", glyph: "↖", name: "top left" },
  { corner: "ne", position: "right-0 top-0", cursor: "cursor-nesw-resize", glyph: "↗", name: "top right" },
  { corner: "sw", position: "bottom-0 left-0", cursor: "cursor-nesw-resize", glyph: "↙", name: "bottom left" },
  { corner: "se", position: "bottom-0 right-0", cursor: "cursor-nwse-resize", glyph: "↘", name: "bottom right" },
] as const;

function loadThread(): AssistantMessage[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as AssistantMessage[]) : [];
  } catch {
    return [];
  }
}

export function AssistantBubble({
  claim,
  snapshot,
}: {
  claim: ClaimState;
  snapshot: GatewaySnapshot | null;
}) {
  const [available, setAvailable] = useState(false);
  // Which model answers the chat (from /assistant/health), shown under the
  // input so operators know what they are talking to — dashboard parity.
  const [model, setModel] = useState<string | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [panelSize, setPanelSize] = useState({ width: 460, height: 520 });
  // Drag offset from the bottom-right anchor, in px.
  // Component state only: a reload snaps back to the corner, which beats
  // restoring a position that an old window size may have made unreachable.
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 });
  const dragRef = useRef<{ startX: number; startY: number; rect: WindowRect } | null>(
    null,
  );
  const panelRef = useRef<HTMLElement | null>(null);
  const resizeRef = useRef<{
    startX: number;
    startY: number;
    rect: WindowRect;
    corner: Corner;
  } | null>(null);
  const [thread, setThread] = useState<AssistantMessage[]>(loadThread);
  const [failedMessageIndex, setFailedMessageIndex] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const activeRequest = useRef<{ controller: AbortController; id: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toolProgress, setToolProgress] = useState<AssistantToolProgress[]>([]);
  const toolProgressRef = useRef<AssistantToolProgress[]>([]);
  const [progressLabel, setProgressLabel] = useState<string | null>(null);
  // Live plan state per plan id — what the cards render and approve from.
  // "gone" = deleted/dismissed (or died with a gateway restart). Not
  // persisted: statuses are re-fetched when the bubble opens, so a stale
  // sessionStorage copy can never be what gets approved.
  const [planStates, setPlanStates] = useState<Record<string, Plan | "gone">>({});
  const [allPlans, setAllPlans] = useState<Plan[]>([]);
  const [planBusy, setPlanBusy] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  const refreshPlan = useCallback(async (planId: string) => {
    try {
      const plan = await getPlan(planId);
      setPlanStates((s) => ({ ...s, [planId]: plan }));
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setPlanStates((s) => ({ ...s, [planId]: "gone" }));
      }
      /* other failures: keep whatever we had; the card degrades read-only */
    }
  }, []);

  useEffect(() => {
    // Keep externally proposed plans available in this popup too. Without
    // this, removing the page panel would strand plans from MCP proposers.
    let active = true;
    const refresh = async () => {
      try {
        const plans = await listPlans();
        if (active) setAllPlans(plans);
      } catch {
        // The status panel reports connectivity; preserve the last review.
      }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 3000);
    return () => { active = false; clearInterval(timer); };
  }, []);

  useEffect(() => {
    if (!open) return;
    // Re-sync every card when the bubble opens: approvals expire, plans get
    // revised or dismissed elsewhere, and the gateway may have restarted.
    const ids = new Set(thread.map((m) => m.planId).filter(Boolean) as string[]);
    ids.forEach((id) => void refreshPlan(id));
    // Deliberately not keyed on `thread`: send() stores the fresh plan itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, refreshPlan]);

  const runPlanAction = useCallback(
    async (planId: string, fn: () => Promise<Plan>) => {
      setPlanBusy(planId);
      setError(null);
      try {
        const updated = await fn();
        setPlanStates((s) => ({ ...s, [planId]: updated }));
        setAllPlans((plans) => plans.map((p) => p.plan_id === planId ? updated : p));
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          setError(`${err.message} — the plan changed; re-read it before approving.`);
        } else {
          setError(err instanceof Error ? err.message : String(err));
        }
        void refreshPlan(planId);
      } finally {
        setPlanBusy(null);
      }
    },
    [refreshPlan],
  );

  const dismissPlan = useCallback(
    async (planId: string) => {
      setPlanBusy(planId);
      setError(null);
      try {
        await deletePlan(planId, claim.token);
        setPlanStates((s) => ({ ...s, [planId]: "gone" }));
        setAllPlans((plans) => plans.filter((p) => p.plan_id !== planId));
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        void refreshPlan(planId);
      } finally {
        setPlanBusy(null);
      }
    },
    [claim.token, refreshPlan],
  );

  const chooseModel = useCallback((next: string) => {
    setModel(next);
    try {
      sessionStorage.setItem(MODEL_STORAGE_KEY, next);
    } catch {
      /* private mode — the pick just won't survive a reload */
    }
  }, []);

  const clearThread = useCallback(() => {
    // Forgets the conversation only. Plans the assistant drafted live on the
    // gateway — clearing a chat must never silently discard something awaiting
    // review. It remains in the popup's external-plan list.
    setThread([]);
    setFailedMessageIndex(null);
    setPlanStates({});
    setError(null);
    toolProgressRef.current = [];
    setToolProgress([]);
    setProgressLabel(null);
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* private mode / quota — nothing to remove */
    }
  }, []);

  const stopReply = useCallback(async () => {
    const active = activeRequest.current;
    if (!active || active.controller.signal.aborted) return;
    setStopping(true);
    setProgressLabel("Stopping…");
    const cancelController = new AbortController();
    const cancelTimeout = window.setTimeout(() => cancelController.abort(), 5000);
    try {
      await cancelAssistantChat(active.id, claim.token, cancelController.signal);
    } catch {
      setError("Gateway cancellation could not be confirmed.");
    } finally {
      window.clearTimeout(cancelTimeout);
      active.controller.abort();
    }
  }, [claim.token]);

  useEffect(() => {
    // One probe on mount. If the gateway has no assistant this component then
    // costs nothing for the rest of the session.
    void getAssistantHealth()
      .then((h) => {
        setAvailable(h.configured);
        const offered = h.models ?? [];
        setModels(offered);
        let saved: string | null = null;
        try {
          saved = sessionStorage.getItem(MODEL_STORAGE_KEY);
        } catch {
          /* private mode — fall back to the gateway default */
        }
        setModel(saved && offered.includes(saved) ? saved : (h.model ?? null));
      })
      .catch(() => setAvailable(false));
  }, []);

  useEffect(() => {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(thread.slice(-MAX_KEPT)));
    } catch {
      /* private mode / quota — the thread just won't survive a reload */
    }
    endRef.current?.scrollIntoView({ block: "end" });
  }, [thread]);

  const send = useCallback(async (resendText?: string, resendIndex?: number) => {
    const text = (resendText ?? draft).trim();
    if (!text || pending) return;
    const controller = new AbortController();
    const requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)),
      (byte) => byte.toString(16).padStart(2, "0")).join("");
    activeRequest.current = { controller, id: requestId };
    const retryFailed = resendIndex === failedMessageIndex && resendIndex === thread.length - 1;
    const next = [...(retryFailed ? thread.slice(0, -1) : thread),
      { role: "user" as const, content: text }];
    setThread(next);
    setFailedMessageIndex(null);
    setDraft("");
    setPending(true);
    setStopping(false);
    setError(null);
    toolProgressRef.current = [];
    setToolProgress([]);
    setProgressLabel("Thinking…");

    const updateProgress = (
      transform: (current: AssistantToolProgress[]) => AssistantToolProgress[],
    ) => {
      const updated = transform(toolProgressRef.current);
      toolProgressRef.current = updated;
      setToolProgress(updated);
    };

    const onProgress = (event: AssistantProgressEvent) => {
      if (controller.signal.aborted) return;
      if (event.type === "thinking") {
        setProgressLabel(
          toolProgressRef.current.length > 0 ? "Reviewing tool results…" : "Thinking…",
        );
      } else if (event.type === "tool_started") {
        updateProgress((current) => [
          ...current,
          { id: event.id, name: event.name, status: "running" },
        ]);
        setProgressLabel(`Using ${toolLabel(event.name)}…`);
      } else if (event.type === "tool_finished") {
        updateProgress((current) =>
          current.map((tool) =>
            tool.id === event.id
              ? {
                  ...tool,
                  status: event.success ? "succeeded" : "failed",
                  error: event.error ?? undefined,
                }
              : tool,
          ),
        );
        setProgressLabel(event.success ? "Thinking…" : "Tool failed; trying to recover…");
      } else if (event.type === "complete") {
        setProgressLabel("Finishing…");
      }
    };

    try {
      const res = await assistantChatStream(
        next.slice(-MAX_KEPT),
        claim.token,
        onProgress,
        model,
        controller.signal,
        requestId,
      );
      if (controller.signal.aborted) throw new DOMException("Stopped", "AbortError");
      // Fetch the steps of any plan it drafted so the operator sees what was
      // proposed *in the chat*. Best-effort: a failed fetch just omits the
      // inline preview rather than failing the turn.
      let steps: PlanStep[] | undefined;
      if (res.plan_id) {
        try {
          const plan = await getPlan(res.plan_id, controller.signal);
          steps = plan.steps;
          // Seed the live card immediately — approvable without a reopen.
          setPlanStates((s) => ({ ...s, [plan.plan_id]: plan }));
        } catch {
          /* preview is a nicety; the card degrades read-only without it */
        }
      }
      if (controller.signal.aborted) throw new DOMException("Stopped", "AbortError");
      const tools = toolProgressRef.current;
      setThread((t) => [
        ...t,
        {
          role: "assistant" as const,
          content:
            res.reply.trim() ||
            (res.plan_id
              ? "Proposed a plan for your review."
              : "The model returned no reply after using tools."),
          tools,
          planId: res.plan_id ?? undefined,
          steps,
        },
      ]);
      toolProgressRef.current = [];
      setToolProgress([]);
      setProgressLabel(null);
    } catch (err) {
      if (controller.signal.aborted) {
        const tools = toolProgressRef.current.map((tool) =>
          tool.status === "running" ? { ...tool, status: "canceled" as const } : tool,
        );
        setThread((t) => [...t, { role: "assistant", content: "Reply stopped.", tools }]);
        // A draft may have been created just before cancellation. The regular
        // poll will catch it too, but refresh now so it can be reviewed.
        void listPlans().then(setAllPlans).catch(() => {});
        toolProgressRef.current = [];
        setToolProgress([]);
        setProgressLabel(null);
        return;
      }
      // The turn failed, so no assistant message is appended — showing an
      // empty bubble would read as the assistant having said nothing rather
      // than as the request never landing.
      const message =
        err instanceof ApiError && err.status === 423
          ? "Take control of the device to use the assistant."
          : err instanceof Error
            ? err.message
            : String(err);
      setError(message);
      setFailedMessageIndex(next.length - 1);
      updateProgress((current) => [
        ...current.map((tool) =>
          tool.status === "running"
            ? { ...tool, status: "failed" as const, error: message }
            : tool,
        ),
        {
          id: `request:${Date.now()}`,
          name: "assistant_request",
          status: "failed",
          error: message,
        },
      ]);
      setProgressLabel(null);
    } finally {
      if (activeRequest.current?.controller === controller) activeRequest.current = null;
      setPending(false);
      setStopping(false);
    }
  }, [draft, pending, thread, failedMessageIndex, claim.token, model]);

  // The header and bottom grip move the panel. Pointer capture keeps movement
  // continuous when the pointer leaves either grip; controls stay clickable.
  function onDragStart(e: React.PointerEvent<HTMLElement>) {
    if ((e.target as HTMLElement).closest("button, input, textarea, select, a")) return;
    const rect = panelRef.current?.getBoundingClientRect();
    if (!rect) return;
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom },
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function onDragMove(e: React.PointerEvent<HTMLElement>) {
    const d = dragRef.current;
    if (!d) return;
    const next = moveWindow(d.rect, e.clientX - d.startX, e.clientY - d.startY,
      window.innerWidth, window.innerHeight);
    setDragOffset({
      x: next.right - (window.innerWidth - 20),
      y: next.bottom - (window.innerHeight - 80),
    });
  }

  function onDragEnd() {
    dragRef.current = null;
  }

  function onResizeStart(e: React.PointerEvent<HTMLElement>, corner: Corner) {
    e.preventDefault();
    e.stopPropagation();
    const rect = panelRef.current?.getBoundingClientRect();
    if (!rect) return;
    resizeRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom },
      corner,
    };
    // Resizing an expanded panel begins from its visible dimensions.
    setPanelSize({ width: rect.width, height: rect.height });
    setExpanded(false);
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function onResizeMove(e: React.PointerEvent<HTMLElement>) {
    const r = resizeRef.current;
    if (!r) return;
    const next = resizeWindow(r.rect, r.corner, e.clientX - r.startX,
      e.clientY - r.startY, window.innerWidth, window.innerHeight);
    setPanelSize({
      width: next.right - next.left,
      height: next.bottom - next.top,
    });
    setDragOffset({
      x: next.right - (window.innerWidth - 20),
      y: next.bottom - (window.innerHeight - 80),
    });
  }

  function onResizeEnd() {
    resizeRef.current = null;
  }

  const threadPlanIds = new Set(thread.map((m) => m.planId).filter(Boolean));
  const visiblePlans = allPlans.filter((p) => p.status !== "aborted" &&
    (p.status !== "executed" || p.results.some((r) => r.reading)));
  const externalPlans = visiblePlans.filter((p) => !threadPlanIds.has(p.plan_id));
  if (!available && visiblePlans.length === 0) return null;

  return (
    <>
      {/* Floating launcher — dashboard AssistantBubble parity: stays visible
          while the panel is open and swaps the chat glyph for an X. Purple is
          the lab-wide "proposes actions you authorize" accent (UI_DESIGN §5),
          shared with the dashboard's Control mode and the xArm panel. */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={open ? "Close the assistant" : "Open the assistant"}
        title={open ? "Minimize — the conversation is kept" : "Ask about this OT-2"}
        className="fixed bottom-5 right-5 z-40 flex h-12 w-12 items-center justify-center rounded-full bg-purple-600 text-white shadow-lg transition hover:bg-purple-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-purple-400 focus-visible:ring-offset-2"
      >
        {open ? (
          <svg
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-5 w-5"
          >
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        ) : (
          <svg
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-5 w-5"
          >
            <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
          </svg>
        )}
      </button>

      {open && (
    <section
      ref={panelRef}
      role="dialog"
      aria-label="OT-2 assistant"
      style={{
        transform: `translate(${dragOffset.x}px, ${dragOffset.y}px)`,
        width: expanded ? 704 : panelSize.width,
        height: expanded ? 736 : panelSize.height,
      }}
      className="fixed bottom-20 right-5 z-40 flex max-h-[calc(100vh-6rem)] max-w-[calc(100vw-2.25rem)] flex-col overflow-hidden rounded-xl border border-purple-300 bg-surface-raised shadow-2xl dark:border-purple-800 dark:bg-slate-900"
    >
      {RESIZE_CORNERS.map(({ corner, position, cursor, glyph, name }) => (
        <div key={corner}
          className={`absolute z-10 flex h-5 w-5 items-center justify-center touch-none select-none text-[11px] text-purple-400 ${position} ${cursor}`}
          onPointerDown={(event) => onResizeStart(event, corner)}
          onPointerMove={onResizeMove}
          onPointerUp={onResizeEnd}
          onPointerCancel={onResizeEnd}
          role="separator"
          aria-label={`Resize the assistant window from ${name}`}
          title={`Drag ${name} corner to resize`}
        >{glyph}</div>
      ))}
      <header
        onPointerDown={onDragStart}
        onPointerMove={onDragMove}
        onPointerUp={onDragEnd}
        onPointerCancel={onDragEnd}
        className="flex cursor-move touch-none select-none items-center justify-between gap-2 border-b border-purple-200 bg-purple-50/60 px-3 py-2 dark:border-purple-800 dark:bg-purple-950/30"
        title="Drag to move"
      >
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-semibold text-ink dark:text-slate-100">
            {available ? "Assistant" : "Plans"}{snapshot?.name ? ` — ${snapshot.name}` : ""}
          </span>
          {/* Which robot this chat drives, and through which machine/address —
              two panels open side by side must be tellable apart before a
              plan gets approved on the wrong one. `status.host` is the
              gateway PC's hostname; the address is the one THIS browser is
              actually connected through (edge or direct port). */}
          <span className="truncate font-mono text-[10px] text-ink-subtle dark:text-slate-500">
            {[snapshot?.status?.host, window.location.host]
              .filter(Boolean)
              .join(" · ")}
          </span>
          <span className="truncate text-[10px] text-ink-subtle dark:text-slate-500">
            Review plans here, then approve and run
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={clearThread}
            disabled={thread.length === 0 || pending}
            className="rounded border border-slate-300 px-2 py-1 text-[11px] font-medium text-ink-subtle transition hover:bg-slate-100 hover:text-ink disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-600 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200"
            aria-label="Clear the conversation"
            title="Clear the conversation"
          >
            Clear
          </button>
          <button
            type="button"
            onClick={() => setExpanded((e) => !e)}
            className="rounded px-2 py-1 text-[14px] leading-none text-ink-subtle hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
            aria-label={expanded ? "Shrink the assistant window" : "Enlarge the assistant window"}
            title={expanded ? "Shrink" : "Enlarge"}
          >
            {expanded ? "⤡" : "⤢"}
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="rounded px-2 py-1 text-[14px] leading-none text-ink-subtle hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
            aria-label="Minimize the assistant"
            title="Minimize — the conversation is kept"
          >
            &minus;
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-3 py-3 text-sm">
        {externalPlans.length > 0 && (
          <div className="mb-3 space-y-2">
            <p className="text-xs font-semibold text-ink dark:text-slate-100">Other plans</p>
            {externalPlans.map((plan) => (
              <div key={plan.plan_id} className="rounded-lg bg-slate-100 p-2 dark:bg-slate-800">
                <p className="text-[11px] text-ink-subtle dark:text-slate-400">From {plan.created_by}</p>
                <ChatPlanCard planId={plan.plan_id} live={plan} busy={planBusy === plan.plan_id}
                  claimHeld={claim.held}
                  onApprove={(hash) => void runPlanAction(plan.plan_id, () => approvePlan(plan.plan_id, hash, claim.token))}
                  onRun={() => void runPlanAction(plan.plan_id, () => executePlan(plan.plan_id, claim.token))}
                  onDiscard={() => void runPlanAction(plan.plan_id, () => abortPlan(plan.plan_id, claim.token))}
                  onDismiss={() => void dismissPlan(plan.plan_id)} />
              </div>
            ))}
          </div>
        )}
        {available && thread.length === 0 && (
          <div className="text-xs text-ink-subtle dark:text-slate-500">
            Ask about this robot&apos;s state, or describe a simple operation and
            I&apos;ll propose it for your approval. For example:
            <ul className="mt-2 list-disc space-y-1 pl-4">
              <li>What is on the deck right now?</li>
              <li>Pick up a tip from A1 and aspirate 50 µL from well B2.</li>
            </ul>
          </div>
        )}
        <ul className="flex flex-col gap-3">
          {thread.map((m, i) => (
            <li
              key={i}
              className={
                m.role === "user"
                  ? "max-w-[85%] self-end rounded-lg bg-purple-600 px-3 py-2 text-[13px] leading-relaxed text-white"
                  : "max-w-[85%] self-start rounded-lg bg-slate-100 px-3 py-2 text-[13px] leading-relaxed text-ink dark:bg-slate-800 dark:text-slate-100"
              }
            >
              {m.role === "assistant" && m.tools && m.tools.length > 0 && (
                <ToolPills tools={m.tools} className="mb-1.5" />
              )}
              {m.role === "assistant"
                ? <AssistantMarkdown text={m.content} />
                : <span className="whitespace-pre-wrap break-words">{m.content}</span>}
              {m.role === "user" && (
                <button type="button" disabled={pending || !claim.held}
                  onClick={() => void send(m.content, i)}
                  className="mt-1 block text-[10px] underline underline-offset-2 disabled:opacity-50"
                  aria-label={`Resend message ${i + 1}`}>
                  {failedMessageIndex === i ? "Retry" : "Resend"}
                </button>
              )}
              {m.planId && (
                <ChatPlanCard
                  planId={m.planId}
                  live={planStates[m.planId]}
                  previewSteps={m.steps}
                  busy={planBusy === m.planId}
                  claimHeld={claim.held}
                  onApprove={(hash) =>
                    void runPlanAction(m.planId!, () =>
                      approvePlan(m.planId!, hash, claim.token),
                    )
                  }
                  onRun={() =>
                    void runPlanAction(m.planId!, () => executePlan(m.planId!, claim.token))
                  }
                  onDiscard={() =>
                    void runPlanAction(m.planId!, () => abortPlan(m.planId!, claim.token))
                  }
                  onDismiss={() => void dismissPlan(m.planId!)}
                />
              )}
            </li>
          ))}
        </ul>
        {toolProgress.length > 0 && (
          <div className="mt-2">
            <ToolPills tools={toolProgress} />
          </div>
        )}
        {pending && progressLabel && (
          <p className="mt-1 text-[11px] text-ink-subtle dark:text-slate-500">
            {progressLabel}
          </p>
        )}
        {error && (
          <div
            role="alert"
            className="mt-2 rounded border border-rose-300 bg-rose-50 px-2 py-1 text-xs text-rose-800 dark:border-rose-700 dark:bg-rose-950/40 dark:text-rose-200"
          >
            {error}
          </div>
        )}
        <div ref={endRef} />
      </div>

      {available && <form
        className="border-t border-purple-200 px-3 py-2 dark:border-purple-800"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <div className="flex items-end gap-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            rows={2}
            disabled={pending}
            placeholder={
              pending
                ? "Working…"
                : claim.held
                  ? "Ask or describe a step…"
                  : "Take control first…"
            }
            className="flex-1 resize-none rounded border border-slate-300 bg-white px-2 py-1 text-sm text-ink shadow-inner focus:border-purple-500 focus:outline-none disabled:opacity-60 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
          />
          {pending ? (
            <button
              type="button"
              onClick={() => void stopReply()}
              disabled={stopping}
              aria-label="Stop assistant reply"
              className="self-stretch rounded bg-slate-700 px-3 text-sm font-medium text-white shadow-sm transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {stopping ? "Stopping…" : "Stop reply"}
            </button>
          ) : (
            <button
              type="submit"
              disabled={!draft.trim()}
              className="self-stretch rounded bg-purple-600 px-3 text-sm font-medium text-white shadow-sm transition hover:bg-purple-700 disabled:cursor-not-allowed disabled:bg-slate-300 dark:disabled:bg-slate-700"
            >
              Send
            </button>
          )}
        </div>
        {model && models.length > 1 ? (
          <label className="mt-1 flex items-center justify-center gap-1 text-[10px] text-ink-subtle dark:text-slate-500">
            model:
            <select
              value={model}
              disabled={pending}
              onChange={(e) => chooseModel(e.target.value)}
              className="rounded border border-slate-300 bg-white px-1 py-0 text-[10px] text-ink focus:border-purple-500 focus:outline-none disabled:opacity-60 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
            >
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
        ) : (
          model && (
            <p className="mt-1 text-center text-[10px] text-ink-subtle dark:text-slate-500">
              model: {model}
            </p>
          )
        )}
      </form>}
      <div
        onPointerDown={onDragStart}
        onPointerMove={onDragMove}
        onPointerUp={onDragEnd}
        onPointerCancel={onDragEnd}
        className="flex h-4 shrink-0 cursor-move touch-none select-none items-center justify-center border-t border-purple-200 bg-purple-50/60 dark:border-purple-800 dark:bg-purple-950/30"
        title="Drag the bottom edge to move the window"
      >
        <span className="pointer-events-none h-1 w-10 rounded-full bg-purple-300 dark:bg-purple-700" aria-hidden />
      </div>
    </section>
      )}
    </>
  );
}

const TOOL_TONE: Record<AssistantToolProgress["status"], string> = {
  running:
    "border-purple-300 bg-purple-50 text-purple-800 dark:border-purple-700 dark:bg-purple-950/50 dark:text-purple-300",
  succeeded:
    "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300",
  failed:
    "border-rose-400 bg-rose-100 text-rose-900 dark:border-rose-700 dark:bg-rose-950/60 dark:text-rose-200",
  canceled:
    "border-slate-300 bg-slate-100 text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

function toolLabel(name: string): string {
  return name.replaceAll("_", " ");
}

function ToolPills({
  tools,
  className = "",
}: {
  tools: AssistantToolProgress[];
  className?: string;
}) {
  return (
    <div className={`flex flex-wrap gap-1 ${className}`}>
      {tools.map((tool) => (
        <span
          key={tool.id}
          title={tool.error}
          className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${
            TOOL_TONE[tool.status]
          } ${tool.status === "running" ? "animate-pulse" : ""}`}
        >
          {tool.status === "running" ? "↻" : tool.status === "succeeded" ? "✓" : tool.status === "canceled" ? "■" : "×"}{" "}
          {toolLabel(tool.name)}
        </span>
      ))}
    </div>
  );
}

const CARD_STATUS_TONE: Record<string, string> = {
  draft: "bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200",
  approved: "bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-300",
  executing: "bg-purple-100 text-purple-800 dark:bg-purple-900/40 dark:text-purple-300",
  executed: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300",
  failed: "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300",
  aborted: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-300",
};

const CARD_STEP_TONE: Record<string, string> = {
  pending: "text-ink dark:text-slate-200",
  ok: "text-emerald-700 dark:text-emerald-400",
  failed: "text-rose-700 dark:text-rose-400",
  skipped: "text-amber-700 dark:text-amber-500",
};

function stepLine(s: PlanStep): string {
  const args = Object.entries(s.args)
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" ");
  return args ? `${s.action}  ${args}` : s.action;
}

/**
 * The in-chat plan card: review, approve, and run without leaving the chat.
 *
 * Renders from the LIVE plan (`live`), never from the proposal-time preview,
 * so the hash sent by Approve is the hash of exactly what is displayed — the
 * same review property the gateway enforces. Without live state (fetch failed,
 * gateway restarted) the card degrades to the read-only preview.
 */
function ChatPlanCard({
  live,
  previewSteps,
  busy,
  claimHeld,
  onApprove,
  onRun,
  onDiscard,
  onDismiss,
}: {
  planId: string;
  live: Plan | "gone" | undefined;
  previewSteps?: PlanStep[];
  busy: boolean;
  claimHeld: boolean;
  onApprove: (stepHash: string) => void;
  onRun: () => void;
  onDiscard: () => void;
  onDismiss: () => void;
}) {
  if (live === "gone") {
    return (
      <p className="mt-1.5 text-[10px] italic text-ink-subtle dark:text-slate-500">
        Plan dismissed.
      </p>
    );
  }

  if (live === undefined) {
    // No live state: show what was proposed, but never an Approve button —
    // approving requires the hash of a plan we can actually see fresh.
    return (
      <div className="mt-1.5 rounded-lg border border-purple-300 bg-purple-50 p-2 text-[12px] dark:border-purple-700 dark:bg-purple-950/40">
        {previewSteps && previewSteps.length > 0 && (
          <ol className="mb-1 flex flex-col gap-0.5">
            {previewSteps.map((s, si) => (
              <li key={si} className="font-mono text-[11px] text-ink dark:text-slate-200">
                {si + 1}. {stepLine(s)}
              </li>
            ))}
          </ol>
        )}
      </div>
    );
  }

  // Dashboard ProposalCard button family: a solid accent action and a quiet
  // text dismiss with a purple-tinted hover.
  const actionButton =
    "rounded px-3 py-1 text-[11px] font-medium text-white transition disabled:cursor-not-allowed disabled:bg-slate-300 dark:disabled:bg-slate-700";
  const quietButton =
    "rounded px-2 py-1 text-[11px] text-ink-subtle hover:bg-purple-100 disabled:opacity-60 dark:text-slate-400 dark:hover:bg-purple-900/40";

  return (
    <div className="mt-1.5 rounded-lg border border-purple-300 bg-purple-50 p-2 text-[12px] dark:border-purple-700 dark:bg-purple-950/40">
      <div className="mb-1 flex items-center gap-1.5">
        <span
          className={`rounded px-1 py-px text-[10px] font-medium ${
            CARD_STATUS_TONE[live.status] ?? CARD_STATUS_TONE.draft
          }`}
        >
          {live.status}
        </span>
      </div>
      <ol className="mb-1 flex flex-col gap-0.5">
        {live.steps.map((s, si) => {
          const outcome = live.results[si]?.outcome ?? "pending";
          return (
            <li
              key={si}
              className={`font-mono text-[11px] ${CARD_STEP_TONE[outcome] ?? CARD_STEP_TONE.pending}`}
            >
              {si + 1}. {stepLine(s)}
              {live.results[si]?.message && (
                <span className={`ml-1 font-sans ${outcome === "failed" ? "text-rose-700 dark:text-rose-400" : "text-ink-subtle dark:text-slate-400"}`}>
                  {live.results[si].message}
                </span>
              )}
              {live.results[si]?.reading && (
                <span className="ml-1 font-sans text-emerald-700 dark:text-emerald-400">
                  {live.results[si].reading.value.toFixed(4)} {live.results[si].reading.unit} · {live.results[si].reading.stable ? "stable" : "unstable"} · {live.results[si].reading.observed_at}
                </span>
              )}
              {live.results[si]?.balance_operation && (
                <span className="ml-1 font-sans text-amber-700 dark:text-amber-400">
                  {live.results[si].balance_operation.outcome === "sent_unconfirmed"
                    ? "Command sent; balance reference unconfirmed"
                    : live.results[si].balance_operation.outcome === "baseline_observed"
                    ? "Stable zero baseline observed after tare"
                    : "Simulation: reference command not sent"}
                </span>
              )}
            </li>
          );
        })}
      </ol>
      {live.status === "draft" && live.non_idempotent_actions.length > 0 && (
        <p className="mb-1 text-[10px] text-amber-700 dark:text-amber-500">
          ⚠ {live.non_idempotent_actions.join(", ")} cannot be safely repeated if the
          link drops mid-step.
        </p>
      )}
      {live.halt_reason && (
        <p className="mb-1 text-[10px] text-rose-700 dark:text-rose-400">
          Halted: {live.halt_reason}
        </p>
      )}
      {live.status === "approved" && !live.executable && live.blocked_reason && (
        <p className="mb-1 text-[10px] text-ink-subtle dark:text-slate-500">
          {live.blocked_reason}
        </p>
      )}
      {!claimHeld && live.status === "draft" && (
        <p className="mb-1 text-[10px] text-amber-700 dark:text-amber-500">
          Take control of the device to approve.
        </p>
      )}
      <div className="flex flex-wrap gap-1.5">
        {live.status === "draft" && (
          <button
            type="button"
            disabled={!claimHeld || busy}
            // Approves the hash of the steps rendered above — never a
            // re-fetch-and-approve, so a plan that moved gets a 409.
            onClick={() => onApprove(live.step_hash)}
            className={`${actionButton} bg-purple-600 hover:bg-purple-700`}
          >
            Approve these {live.steps.length} steps
          </button>
        )}
        {live.status === "approved" && (
          <button
            type="button"
            disabled={!claimHeld || busy || !live.executable}
            onClick={onRun}
            className={`${actionButton} bg-emerald-600 hover:bg-emerald-700`}
          >
            {busy ? "Running…" : "Run"}
          </button>
        )}
        {(live.status === "draft" || live.status === "approved") && (
          <button type="button" disabled={!claimHeld || busy} onClick={onDiscard} className={quietButton}>
            Discard
          </button>
        )}
        {(live.status === "failed" || live.status === "executed" || live.status === "aborted") && (
          <button type="button" disabled={!claimHeld || busy} onClick={onDismiss} className={quietButton}>
            Dismiss
          </button>
        )}
      </div>
    </div>
  );
}
