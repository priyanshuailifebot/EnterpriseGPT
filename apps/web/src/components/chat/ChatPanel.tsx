"use client";

/**
 * Production-grade test surface for any Tools-Agent workflow.
 *
 * Renders a chat conversation against a workflow that contains a chat
 * trigger. Streams the assistant's reply token by token, shows tool
 * calls as collapsible badges with their args + results + duration,
 * surfaces parser retries inline, and offers memory inspection + reset.
 *
 * Mounted with a single ``{ workspaceId, workflowId, triggerSlug }``
 * triple; the underlying ``useChatSession`` hook owns the session
 * lifecycle and SSE plumbing.
 */

import {
  AlertCircle,
  Brain,
  ChevronDown,
  ChevronRight,
  CircleDollarSign,
  History,
  Loader2,
  RefreshCcw,
  Send,
  Sparkles,
  Square,
  Wrench,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { resolveProviderForSlug } from "@/components/workflow/integration-icons";
import {
  type ChatMessage,
  type ToolInvocation,
  useChatSession,
} from "@/hooks/useChatSession";
import { useChatSessions } from "@/hooks/useChatSessions";
import { cn } from "@/lib/utils";

export interface ChatPanelProps {
  workspaceId: string;
  workflowId: string;
  triggerSlug: string;
  /** Show the operator controls (memory inspect, reset). Default: true. */
  showOperatorControls?: boolean;
  /** Called when the user closes the panel. */
  onClose?: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ChatPanel({
  workspaceId,
  workflowId,
  triggerSlug,
  showOperatorControls = true,
  onClose,
}: ChatPanelProps) {
  // ``resumeSessionId`` lets the user pick a prior session from the
  // History popover; when set the hook fetches durable history instead
  // of opening a fresh session.
  const [resumeSessionId, setResumeSessionId] = useState<string | null>(null);
  const opts = useMemo(
    () => ({ workspaceId, workflowId, triggerSlug, resumeSessionId }),
    [workspaceId, workflowId, triggerSlug, resumeSessionId],
  );
  const chat = useChatSession(opts);
  const [draft, setDraft] = useState("");
  const scrollerRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to the latest message whenever the list grows or the
  // in-flight assistant text mutates.
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [chat.messages, chat.status]);

  const onSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      const text = draft.trim();
      if (!text) return;
      if (chat.status !== "ready" && chat.status !== "error") return;
      chat.send(text);
      setDraft("");
    },
    [chat, draft],
  );

  const isBusy =
    chat.status === "streaming" ||
    chat.status === "tool_running" ||
    chat.status === "validating";

  return (
    <div className="flex h-[600px] w-full flex-col overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-xl dark:border-slate-800 dark:bg-slate-950">
      {/* ----------------------- header ----------------------- */}
      <header className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-4 py-3 dark:border-slate-800 dark:bg-slate-900">
        <div className="flex min-w-0 items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-brand-50 text-brand-700 dark:bg-brand-950 dark:text-brand-300">
            <Sparkles className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
              Test chat
            </p>
            <p className="truncate text-xs text-slate-500 dark:text-slate-400">
              <code className="rounded bg-slate-100 px-1 dark:bg-slate-800">
                /chat/{triggerSlug}
              </code>{" "}
              · {_statusLabel(chat.status)}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {showOperatorControls ? (
            <>
              <SessionPicker
                workspaceId={workspaceId}
                workflowId={workflowId}
                activeSessionId={chat.sessionId}
                onResume={(id) => setResumeSessionId(id)}
                onNew={() => setResumeSessionId(null)}
              />
              <UsageBadge usage={chat.usage} />
              <MemoryBadge
                onInspect={chat.refreshMemory}
                memory={chat.memory}
              />
              <button
                type="button"
                className="rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
                onClick={() => void chat.reset()}
                disabled={isBusy}
                title="Reset session and memory"
              >
                <RefreshCcw className="h-3.5 w-3.5" />
              </button>
            </>
          ) : null}
          {onClose ? (
            <button
              type="button"
              className="rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
              onClick={onClose}
              title="Close"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          ) : null}
        </div>
      </header>

      {/* ----------------------- conversation ----------------------- */}
      <div
        ref={scrollerRef}
        className="flex-1 space-y-3 overflow-y-auto bg-white px-4 py-4 dark:bg-slate-950"
      >
        {chat.status === "opening" ? (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <Loader2 className="h-4 w-4 animate-spin" />
            Opening session…
          </div>
        ) : null}

        {chat.messages.map((m) => (
          <MessageBubble key={m.clientId} message={m} />
        ))}

        {chat.error ? (
          <div className="flex items-start gap-2 rounded-xl border border-rose-300 bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:border-rose-800 dark:bg-rose-950/50 dark:text-rose-300">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{chat.error}</span>
          </div>
        ) : null}
      </div>

      {/* ----------------------- composer ----------------------- */}
      <form
        onSubmit={onSubmit}
        className="flex items-end gap-2 border-t border-slate-200 bg-slate-50 px-3 py-3 dark:border-slate-800 dark:bg-slate-900"
      >
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSubmit(e as unknown as React.FormEvent);
            }
          }}
          placeholder={
            chat.status === "opening" ? "Connecting…" : "Type a message…"
          }
          disabled={chat.status === "opening" || chat.status === "closed"}
          rows={1}
          className="min-h-[40px] flex-1 resize-none rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200 disabled:opacity-60 dark:border-slate-700 dark:bg-slate-950 dark:focus:ring-brand-800"
        />
        {isBusy ? (
          <button
            type="button"
            onClick={chat.cancel}
            className="inline-flex h-10 w-10 items-center justify-center rounded-xl bg-rose-600 text-white hover:bg-rose-700"
            title="Cancel"
          >
            <Square className="h-4 w-4" />
          </button>
        ) : (
          <button
            type="submit"
            disabled={
              !draft.trim() ||
              chat.status === "opening" ||
              chat.status === "closed"
            }
            className="inline-flex h-10 w-10 items-center justify-center rounded-xl bg-brand-600 text-white hover:bg-brand-700 disabled:opacity-50"
            title="Send"
          >
            <Send className="h-4 w-4" />
          </button>
        )}
      </form>

      {/* ----------------------- telemetry footer ----------------------- */}
      {chat.telemetry.promptTokens || chat.telemetry.completionTokens || chat.telemetry.toolCalls ? (
        <div className="border-t border-slate-200 bg-slate-50 px-3 py-1.5 text-[10px] text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
          {chat.telemetry.promptTokens} prompt · {chat.telemetry.completionTokens} completion · {chat.telemetry.toolCalls} tool calls
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function _statusLabel(status: string): string {
  switch (status) {
    case "idle":
      return "idle";
    case "opening":
      return "opening session…";
    case "ready":
      return "ready";
    case "streaming":
      return "assistant typing…";
    case "tool_running":
      return "calling tool…";
    case "validating":
      return "validating output…";
    case "closed":
      return "closed";
    case "error":
      return "error";
    default:
      return status;
  }
}

function MessageBubble({ message }: { message: ChatMessage }) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl rounded-br-md bg-brand-600 px-3 py-2 text-sm text-white shadow-sm">
          {message.content}
        </div>
      </div>
    );
  }
  if (message.role === "tool") {
    // Tool messages don't render in the conversation directly — they're
    // shown nested inside the assistant turn that triggered them. Keep
    // this branch for forward-compat with future history-replay flows.
    return null;
  }
  // Assistant
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] space-y-2">
        <div className="rounded-2xl rounded-bl-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-900 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:text-slate-100">
          {message.content || (
            <span className="inline-flex items-center gap-1 text-slate-400">
              <Loader2 className="h-3 w-3 animate-spin" />
              <span>thinking…</span>
            </span>
          )}
          {message.inFlight ? (
            <span className="ml-1 inline-block h-3 w-1 animate-pulse bg-slate-400" />
          ) : null}
        </div>
        {message.toolCalls && message.toolCalls.length > 0 ? (
          <div className="space-y-1.5">
            {message.toolCalls.map((tc) => (
              <ToolCallBadge key={tc.id} call={tc} />
            ))}
          </div>
        ) : null}
        {message.parserStatus === "failed" && message.parserError ? (
          <div className="rounded-xl border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] text-amber-700 dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-300">
            Output parser flagged: <code>{message.parserError}</code>
          </div>
        ) : null}
        {message.parserStatus === "ok" && message.structured ? (
          <details className="rounded-xl border border-emerald-300 bg-emerald-50 px-2 py-1 text-[11px] text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300">
            <summary className="cursor-pointer">structured output ✓</summary>
            <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[10px]">
              {JSON.stringify(message.structured, null, 2)}
            </pre>
          </details>
        ) : null}
      </div>
    </div>
  );
}

function ToolCallBadge({ call }: { call: ToolInvocation }) {
  const [open, setOpen] = useState(false);
  const tag = resolveProviderForSlug(call.name);
  const isPending = call.status === "pending";
  const isError = call.status === "error";
  return (
    <div
      className={cn(
        "overflow-hidden rounded-xl border text-xs",
        isError
          ? "border-rose-300 bg-rose-50 dark:border-rose-800 dark:bg-rose-950/40"
          : "border-slate-300 bg-white dark:border-slate-700 dark:bg-slate-900",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((s) => !s)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-slate-50 dark:hover:bg-slate-800"
      >
        {open ? (
          <ChevronDown className="h-3 w-3 shrink-0" />
        ) : (
          <ChevronRight className="h-3 w-3 shrink-0" />
        )}
        <div
          className={cn(
            "flex h-5 w-5 shrink-0 items-center justify-center rounded",
            tag ? tag.bg : "bg-slate-100 dark:bg-slate-800",
            tag ? tag.fg : "text-slate-500",
          )}
        >
          {tag ? tag.icon : <Wrench className="h-3 w-3" />}
        </div>
        <code className="truncate font-mono text-[11px] text-slate-800 dark:text-slate-200">
          {call.name}
        </code>
        {isPending ? (
          <Loader2 className="ml-auto h-3 w-3 shrink-0 animate-spin text-slate-400" />
        ) : (
          <span className="ml-auto shrink-0 text-[10px] text-slate-400">
            {call.durationMs != null ? `${call.durationMs} ms` : null}
          </span>
        )}
      </button>
      {open ? (
        <div className="space-y-1 border-t border-slate-200 bg-slate-50 px-2 py-1.5 dark:border-slate-800 dark:bg-slate-950">
          <div>
            <p className="text-[10px] uppercase tracking-wide text-slate-500">
              args
            </p>
            <pre className="max-h-32 overflow-auto whitespace-pre-wrap font-mono text-[10px] text-slate-700 dark:text-slate-300">
              {JSON.stringify(call.args, null, 2)}
            </pre>
          </div>
          {call.result !== undefined ? (
            <div>
              <p className="text-[10px] uppercase tracking-wide text-slate-500">
                result
              </p>
              <pre className="max-h-32 overflow-auto whitespace-pre-wrap font-mono text-[10px] text-slate-700 dark:text-slate-300">
                {JSON.stringify(call.result, null, 2)}
              </pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function UsageBadge({
  usage,
}: {
  usage: ReturnType<typeof useChatSession>["usage"];
}) {
  const [open, setOpen] = useState(false);
  if (!usage) {
    return null;
  }
  const cost = usage.total_cost_cents;
  const limits = usage.rate_limits ?? null;
  const tokenUsed =
    usage.total_prompt_tokens + usage.total_completion_tokens;
  const tokenLimit = limits?.max_total_tokens ?? null;
  const costLimit = limits?.max_total_cost_cents ?? null;
  const tokenPct = tokenLimit ? Math.min(100, Math.round((tokenUsed / tokenLimit) * 100)) : null;
  const costPct = costLimit ? Math.min(100, Math.round((cost / costLimit) * 100)) : null;
  const hot = (tokenPct != null && tokenPct >= 90) || (costPct != null && costPct >= 90);
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((s) => !s)}
        className={
          "inline-flex items-center gap-1 rounded-lg border px-2 py-1 text-xs " +
          (hot
            ? "border-rose-400 text-rose-700 hover:bg-rose-50 dark:border-rose-700 dark:text-rose-300"
            : "border-slate-300 text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800")
        }
        title="Inspect usage"
      >
        <CircleDollarSign className="h-3.5 w-3.5" />
        {cost > 0 ? `${(cost / 100).toFixed(2)}¢` : "0¢"}
      </button>
      {open ? (
        <div className="absolute right-0 top-full mt-2 w-64 rounded-xl border border-slate-200 bg-white p-3 text-xs shadow-xl dark:border-slate-700 dark:bg-slate-950">
          <p className="font-semibold text-slate-900 dark:text-slate-100">
            Usage
          </p>
          <dl className="mt-1 grid grid-cols-2 gap-1 text-slate-600 dark:text-slate-300">
            <dt>messages</dt>
            <dd className="text-right">{usage.total_messages}</dd>
            <dt>prompt tokens</dt>
            <dd className="text-right">{usage.total_prompt_tokens}</dd>
            <dt>completion tokens</dt>
            <dd className="text-right">{usage.total_completion_tokens}</dd>
            <dt>cost</dt>
            <dd className="text-right">{(cost / 100).toFixed(4)}¢ USD</dd>
          </dl>
          {tokenPct != null ? (
            <div className="mt-2">
              <div className="flex justify-between text-[10px] text-slate-500">
                <span>token budget</span>
                <span>
                  {tokenUsed} / {tokenLimit}
                </span>
              </div>
              <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
                <div
                  className={
                    "h-full " +
                    (tokenPct >= 90 ? "bg-rose-500" : "bg-brand-500")
                  }
                  style={{ width: `${tokenPct}%` }}
                />
              </div>
            </div>
          ) : null}
          {costPct != null ? (
            <div className="mt-2">
              <div className="flex justify-between text-[10px] text-slate-500">
                <span>cost budget</span>
                <span>
                  {(cost / 100).toFixed(2)}¢ / {(costLimit ?? 0) / 100}¢
                </span>
              </div>
              <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
                <div
                  className={
                    "h-full " +
                    (costPct >= 90 ? "bg-rose-500" : "bg-brand-500")
                  }
                  style={{ width: `${costPct}%` }}
                />
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function MemoryBadge({
  onInspect,
  memory,
}: {
  onInspect: () => Promise<void>;
  memory: ReturnType<typeof useChatSession>["memory"];
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => {
          setOpen((s) => !s);
          void onInspect();
        }}
        className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
        title="Inspect memory"
      >
        <Brain className="h-3.5 w-3.5" />
        {memory ? memory.count : "—"}
      </button>
      {open && memory ? (
        <div className="absolute right-0 top-full mt-2 w-60 rounded-xl border border-slate-200 bg-white p-3 text-xs shadow-xl dark:border-slate-700 dark:bg-slate-950">
          <p className="font-semibold text-slate-900 dark:text-slate-100">
            Memory
          </p>
          <dl className="mt-1 grid grid-cols-2 gap-1 text-slate-600 dark:text-slate-300">
            <dt>scope</dt>
            <dd className="text-right">{memory.scope}</dd>
            <dt>turns</dt>
            <dd className="text-right">
              {memory.count} / {memory.max_turns}
            </dd>
            <dt>ttl</dt>
            <dd className="text-right">
              {memory.ttl >= 0 ? `${memory.ttl}s` : "—"}
            </dd>
          </dl>
        </div>
      ) : null}
    </div>
  );
}

function SessionPicker({
  workspaceId,
  workflowId,
  activeSessionId,
  onResume,
  onNew,
}: {
  workspaceId: string;
  workflowId: string;
  activeSessionId: string | null;
  onResume: (id: string) => void;
  onNew: () => void;
}) {
  const [open, setOpen] = useState(false);
  const { items, loading, refresh } = useChatSessions({
    workspaceId,
    workflowId,
    enabled: open,
  });

  // Refresh when opening so the list isn't stale.
  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((s) => !s)}
        className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
        title="Prior sessions"
      >
        <History className="h-3.5 w-3.5" />
      </button>
      {open ? (
        <div className="absolute right-0 top-full mt-2 max-h-80 w-72 overflow-auto rounded-xl border border-slate-200 bg-white text-xs shadow-xl dark:border-slate-700 dark:bg-slate-950">
          <div className="sticky top-0 flex items-center justify-between border-b border-slate-100 bg-white px-3 py-2 font-semibold text-slate-900 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-100">
            <span>Prior sessions</span>
            <button
              type="button"
              onClick={() => {
                onNew();
                setOpen(false);
              }}
              className="rounded-md border border-brand-300 px-1.5 py-0.5 text-[10px] font-normal text-brand-700 hover:bg-brand-50 dark:border-brand-700 dark:text-brand-300 dark:hover:bg-brand-950"
            >
              + new
            </button>
          </div>
          {loading ? (
            <div className="flex items-center gap-2 p-3 text-slate-500">
              <Loader2 className="h-3 w-3 animate-spin" /> loading…
            </div>
          ) : items.length === 0 ? (
            <div className="p-3 text-slate-500">No prior sessions.</div>
          ) : (
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {items.map((s) => {
                const isActive = s.id === activeSessionId;
                return (
                  <li key={s.id}>
                    <button
                      type="button"
                      disabled={isActive}
                      onClick={() => {
                        onResume(s.id);
                        setOpen(false);
                      }}
                      className={
                        "block w-full px-3 py-2 text-left hover:bg-slate-50 dark:hover:bg-slate-900 " +
                        (isActive
                          ? "bg-brand-50 text-brand-900 dark:bg-brand-950/40 dark:text-brand-200"
                          : "")
                      }
                    >
                      <div className="flex items-center justify-between gap-2">
                        <code className="truncate text-[10px] text-slate-500">
                          {s.id.slice(0, 8)}…
                        </code>
                        <span
                          className={
                            "rounded-full px-1.5 py-0.5 text-[10px] " +
                            (s.status === "active"
                              ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
                              : "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400")
                          }
                        >
                          {s.status}
                        </span>
                      </div>
                      <div className="mt-1 flex justify-between text-[10px] text-slate-500">
                        <span>{s.total_messages} msgs</span>
                        <span>
                          {(s.total_cost_cents / 100).toFixed(2)}¢
                        </span>
                        <span>
                          {s.last_activity_at
                            ? new Date(s.last_activity_at).toLocaleString(
                                undefined,
                                {
                                  month: "short",
                                  day: "numeric",
                                  hour: "2-digit",
                                  minute: "2-digit",
                                },
                              )
                            : "—"}
                        </span>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}
