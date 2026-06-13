"use client";

/**
 * Chat-session state machine + streaming client for the Tools-Agent runtime.
 *
 * Owns:
 *  - session lifecycle (open / close / reset)
 *  - the in-memory message list, including the *in-flight* assistant turn
 *    being streamed token by token
 *  - tool call lifecycle (pending → resolved with result + duration)
 *  - cancellation (aborts the SSE stream cleanly)
 *
 * Surface kept intentionally minimal so the ``ChatPanel`` component can
 * stay declarative: every render reads from ``messages`` + ``status`` +
 * ``memory`` and writes via ``send`` / ``cancel`` / ``reset`` / ``close``.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { createEventSourceWithAuth } from "@/lib/sse";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ChatRole = "user" | "assistant" | "tool" | "system";

export interface ToolInvocation {
  id: string;
  name: string;
  args: Record<string, unknown>;
  result?: unknown;
  durationMs?: number;
  status: "pending" | "ok" | "error";
}

export interface ChatMessage {
  /** Stable client-side id; messages from history are keyed on server id. */
  clientId: string;
  role: ChatRole;
  content: string;
  /** Tool calls the assistant made during this turn. */
  toolCalls?: ToolInvocation[];
  /** True while the assistant is still streaming this message. */
  inFlight?: boolean;
  /** ``ok`` / ``failed`` / null — populated after the parser runs. */
  parserStatus?: "ok" | "failed" | null;
  parserError?: string | null;
  /** Validated structured value (when an OutputParserNode is attached). */
  structured?: unknown;
  /** Timestamp (ms) when the message first appeared in the client. */
  createdAt: number;
}

export interface MemoryInspect {
  scope: string;
  scope_id: string | null;
  count: number;
  ttl: number;
  max_turns: number;
}

export interface UsageSnapshot {
  session_id: string;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_messages: number;
  total_cost_cents: number;
  rate_limits: {
    messages_per_minute?: number;
    max_total_tokens?: number;
    max_total_cost_cents?: number;
  } | null;
}

export interface RateLimitInfo {
  reason: string;
  retry_after_seconds: number | null;
  snapshot: Record<string, unknown> | null;
}

export type ChatStatus =
  | "idle"
  | "opening"
  | "ready"
  | "streaming"
  | "tool_running"
  | "validating"
  | "closed"
  | "error";

interface OpenSessionResponse {
  session_id: string;
  workspace_id: string;
  workflow_id: string;
  trigger_slug: string;
  agent_node_id: string;
  welcome_message: string | null;
  created_at: string;
}

interface UseChatSessionOpts {
  workspaceId: string;
  workflowId: string;
  triggerSlug: string;
  /** Surfaced to backend as session metadata (customer id, locale, etc). */
  metadata?: Record<string, unknown>;
  /** Optional secret if the chat trigger declared ``secret_required``. */
  secret?: string;
  /**
   * Resume an existing session instead of opening a new one. When set,
   * the hook skips ``POST /sessions`` and fetches the durable message
   * history via ``GET /messages`` to populate the panel.
   */
  resumeSessionId?: string | null;
}

interface HistoryMessage {
  id: string;
  role: string;
  content: string;
  tool_calls: unknown[] | null;
  tool_call_id: string | null;
  tool_name: string | null;
  created_at: string;
  parser_status: "ok" | "failed" | null;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

let _clientIdCounter = 0;
function _newClientId(prefix: string): string {
  _clientIdCounter += 1;
  return `${prefix}_${Date.now()}_${_clientIdCounter}`;
}

export function useChatSession(opts: UseChatSessionOpts | null) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [welcome, setWelcome] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<ChatStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [memory, setMemory] = useState<MemoryInspect | null>(null);
  const [usage, setUsage] = useState<UsageSnapshot | null>(null);
  const [rateLimit, setRateLimit] = useState<RateLimitInfo | null>(null);
  const [telemetry, setTelemetry] = useState<{
    promptTokens: number;
    completionTokens: number;
    toolCalls: number;
  }>({ promptTokens: 0, completionTokens: 0, toolCalls: 0 });

  // Latest in-flight assistant message id — used by the SSE handler to
  // accumulate deltas without scanning the whole list every event.
  const inFlightIdRef = useRef<string | null>(null);
  // Active stream abort handle.
  const cancelRef = useRef<(() => void) | null>(null);
  // Mounted guard for setState-after-unmount safety.
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cancelRef.current?.();
    };
  }, []);

  // -------------------------------------------------------------------
  // Open / close
  // -------------------------------------------------------------------

  useEffect(() => {
    if (!opts) return;
    let cancelled = false;
    setStatus("opening");
    setError(null);
    void (async () => {
      try {
        let resolvedSessionId: string;
        let resolvedWelcome: string | null = null;

        if (opts.resumeSessionId) {
          // Resume path — no POST /sessions; fetch durable history.
          resolvedSessionId = opts.resumeSessionId;
        } else {
          const { data } = await api.post<OpenSessionResponse>(
            `/api/v1/chat/${encodeURIComponent(opts.triggerSlug)}/sessions`,
            {
              workspace_id: opts.workspaceId,
              workflow_id: opts.workflowId,
              metadata: opts.metadata ?? {},
              secret: opts.secret ?? "",
            },
          );
          if (cancelled || !mountedRef.current) return;
          resolvedSessionId = data.session_id;
          resolvedWelcome = data.welcome_message ?? null;
        }

        setSessionId(resolvedSessionId);
        setWelcome(resolvedWelcome);

        if (opts.resumeSessionId) {
          // Rehydrate durable history. Tool turns are folded under the
          // assistant turn that issued them — the runtime persists
          // tool_calls on the assistant row already, so we reconstruct
          // ToolInvocation badges by walking the message list.
          try {
            const { data: hist } = await api.get<{ items: HistoryMessage[] }>(
              `/api/v1/chat/sessions/${resolvedSessionId}/messages?page=1&page_size=100`,
            );
            if (cancelled || !mountedRef.current) return;
            setMessages(_rehydrateHistory(hist.items));
          } catch {
            // History fetch failed — leave the panel empty so the user
            // can still continue the conversation.
          }
        } else if (resolvedWelcome) {
          setMessages([
            {
              clientId: _newClientId("welcome"),
              role: "assistant",
              content: resolvedWelcome,
              createdAt: Date.now(),
            },
          ]);
        }

        setStatus("ready");
      } catch (e: unknown) {
        if (cancelled || !mountedRef.current) return;
        setStatus("error");
        setError(e instanceof Error ? e.message : "failed to open chat session");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    opts?.workspaceId,
    opts?.workflowId,
    opts?.triggerSlug,
    opts?.resumeSessionId,
    // metadata is reference-stable for the caller's lifetime by convention
    // (see ChatPanel) — we don't track it as a dep to avoid re-opening
    // sessions when the parent re-renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ]);

  // -------------------------------------------------------------------
  // Send a user message via the streaming endpoint.
  // -------------------------------------------------------------------

  const send = useCallback(
    (content: string) => {
      if (!sessionId || status === "closed") return;
      cancelRef.current?.(); // cancel any prior in-flight stream

      const userMsg: ChatMessage = {
        clientId: _newClientId("user"),
        role: "user",
        content,
        createdAt: Date.now(),
      };
      const assistantId = _newClientId("a");
      const assistantMsg: ChatMessage = {
        clientId: assistantId,
        role: "assistant",
        content: "",
        toolCalls: [],
        inFlight: true,
        createdAt: Date.now(),
      };
      inFlightIdRef.current = assistantId;
      setMessages((m) => [...m, userMsg, assistantMsg]);
      setStatus("streaming");
      setError(null);

      const cancel = createEventSourceWithAuth(
        `/api/v1/chat/sessions/${sessionId}/messages/stream`,
        { content },
        (evt) => _handleEvent(evt, assistantId),
        (err) => {
          if (!mountedRef.current) return;
          setStatus("error");
          setError(err.message || "stream failed");
          _finalizeInFlight(assistantId, { failed: true });
        },
        () => {
          if (!mountedRef.current) return;
          setStatus("ready");
          _finalizeInFlight(assistantId);
        },
        { maxReconnectAttempts: 1 }, // reconnect mid-LLM-stream is unsafe
      );
      cancelRef.current = cancel;
    },
    // The handlers reference setState via closures; nothing extra needed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sessionId, status],
  );

  // -------------------------------------------------------------------
  // SSE event dispatch
  // -------------------------------------------------------------------

  const _handleEvent = useCallback(
    (evt: Record<string, unknown>, assistantId: string) => {
      const type = String(evt.type ?? "");
      switch (type) {
        case "ready":
          // No-op for now — could surface tools/memory metadata if the
          // panel wants to display them per turn.
          break;
        case "assistant_delta": {
          const delta = String(evt.delta ?? "");
          if (!delta) break;
          setMessages((m) =>
            m.map((msg) =>
              msg.clientId === assistantId
                ? { ...msg, content: msg.content + delta }
                : msg,
            ),
          );
          setStatus("streaming");
          break;
        }
        case "tool_call": {
          const inv: ToolInvocation = {
            id: String(evt.id ?? ""),
            name: String(evt.name ?? ""),
            args:
              (evt.args as Record<string, unknown> | undefined) ?? {},
            status: "pending",
          };
          setMessages((m) =>
            m.map((msg) =>
              msg.clientId === assistantId
                ? { ...msg, toolCalls: [...(msg.toolCalls ?? []), inv] }
                : msg,
            ),
          );
          setStatus("tool_running");
          break;
        }
        case "tool_result": {
          const id = String(evt.id ?? "");
          const result = evt.result;
          const durationMs = Number(evt.duration_ms ?? 0) || undefined;
          const ok =
            result && typeof result === "object" && "ok" in result
              ? (result as { ok?: unknown }).ok !== false
              : true;
          setMessages((m) =>
            m.map((msg) => {
              if (msg.clientId !== assistantId) return msg;
              return {
                ...msg,
                toolCalls: (msg.toolCalls ?? []).map((tc) =>
                  tc.id === id
                    ? {
                        ...tc,
                        result,
                        durationMs,
                        status: ok ? "ok" : "error",
                      }
                    : tc,
                ),
              };
            }),
          );
          setTelemetry((t) => ({ ...t, toolCalls: t.toolCalls + 1 }));
          setStatus("streaming"); // back to LLM for the next round
          break;
        }
        case "parser_validating":
          setStatus("validating");
          break;
        case "parser_retry":
          // Surface the validation error inline (non-fatal — the runtime
          // re-prompted and will continue streaming).
          setMessages((m) =>
            m.map((msg) =>
              msg.clientId === assistantId
                ? {
                    ...msg,
                    parserStatus: "failed",
                    parserError: String(evt.error ?? "schema mismatch"),
                  }
                : msg,
            ),
          );
          setStatus("streaming");
          break;
        case "turn_complete": {
          const assistantText = String(evt.assistant_text ?? "");
          const structured = evt.structured ?? null;
          const parserStatus =
            (evt.parser_status as "ok" | "failed" | null | undefined) ?? null;
          const parserError = (evt.parser_error as string | null) ?? null;
          const prompt = Number(evt.prompt_tokens ?? 0) || 0;
          const completion = Number(evt.completion_tokens ?? 0) || 0;
          setTelemetry((t) => ({
            promptTokens: t.promptTokens + prompt,
            completionTokens: t.completionTokens + completion,
            toolCalls: t.toolCalls,
          }));
          setMessages((m) =>
            m.map((msg) =>
              msg.clientId === assistantId
                ? {
                    ...msg,
                    content: assistantText || msg.content,
                    inFlight: false,
                    parserStatus,
                    parserError,
                    structured,
                  }
                : msg,
            ),
          );
          setStatus("ready");
          break;
        }
        case "rate_limited": {
          const info: RateLimitInfo = {
            reason: String(evt.reason ?? "rate_limited"),
            retry_after_seconds:
              evt.retry_after_seconds == null
                ? null
                : Number(evt.retry_after_seconds),
            snapshot:
              (evt.snapshot as Record<string, unknown> | null) ?? null,
          };
          setRateLimit(info);
          setError(
            info.retry_after_seconds
              ? `Rate limited — retry in ${info.retry_after_seconds}s`
              : `Rate limited (${info.reason})`,
          );
          setStatus("error");
          _finalizeInFlight(assistantId, { failed: true });
          break;
        }
        case "error": {
          const message = String(evt.message ?? "unknown error");
          setError(message);
          setStatus("error");
          _finalizeInFlight(assistantId, { failed: true });
          break;
        }
        case "heartbeat":
        default:
          // Ignore.
          break;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const _finalizeInFlight = useCallback(
    (assistantId: string, opts2?: { failed?: boolean }) => {
      setMessages((m) =>
        m.map((msg) =>
          msg.clientId === assistantId && msg.inFlight
            ? {
                ...msg,
                inFlight: false,
                parserStatus: opts2?.failed ? "failed" : msg.parserStatus,
              }
            : msg,
        ),
      );
      inFlightIdRef.current = null;
      cancelRef.current = null;
    },
    [],
  );

  // -------------------------------------------------------------------
  // Cancel / close / reset / memory inspect
  // -------------------------------------------------------------------

  const cancel = useCallback(() => {
    cancelRef.current?.();
    cancelRef.current = null;
    if (inFlightIdRef.current) {
      _finalizeInFlight(inFlightIdRef.current, { failed: true });
    }
    setStatus("ready");
  }, [_finalizeInFlight]);

  const close = useCallback(async () => {
    cancel();
    if (!sessionId) return;
    try {
      await api.delete(`/api/v1/chat/sessions/${sessionId}`);
    } catch {
      // Best-effort — the session expires server-side on its own.
    }
    if (mountedRef.current) setStatus("closed");
  }, [cancel, sessionId]);

  const reset = useCallback(async () => {
    if (!sessionId) return;
    try {
      await api.delete(`/api/v1/chat/sessions/${sessionId}`);
    } catch {
      // Ignore — we'll just open a new session below.
    }
    if (!mountedRef.current) return;
    setMessages([]);
    setSessionId(null);
    setStatus("idle");
    setTelemetry({ promptTokens: 0, completionTokens: 0, toolCalls: 0 });
    // Caller is expected to re-mount the hook (or unset/reset `opts`) to
    // open a fresh session. This avoids a hidden re-open race.
  }, [sessionId]);

  const refreshMemory = useCallback(async () => {
    if (!sessionId) return;
    try {
      const { data } = await api.get<MemoryInspect>(
        `/api/v1/chat/sessions/${sessionId}/memory`,
      );
      if (mountedRef.current) setMemory(data);
    } catch {
      if (mountedRef.current) setMemory(null);
    }
  }, [sessionId]);

  const refreshUsage = useCallback(async () => {
    if (!sessionId) return;
    try {
      const { data } = await api.get<UsageSnapshot>(
        `/api/v1/chat/sessions/${sessionId}/usage`,
      );
      if (mountedRef.current) setUsage(data);
    } catch {
      if (mountedRef.current) setUsage(null);
    }
  }, [sessionId]);

  // After every turn completes (status flips back to ``ready``) the
  // session's cumulative cost grew — refresh transparently so the UI's
  // usage badge stays current without the user clicking anything.
  useEffect(() => {
    if (status === "ready" && sessionId) void refreshUsage();
  }, [status, sessionId, refreshUsage]);

  return {
    sessionId,
    welcome,
    messages,
    status,
    error,
    memory,
    usage,
    rateLimit,
    telemetry,
    send,
    cancel,
    close,
    reset,
    refreshMemory,
    refreshUsage,
  };
}

// ---------------------------------------------------------------------------
// History rehydration — fold persisted tool turns under their initiating
// assistant message so the UI matches the live-stream rendering shape.
// ---------------------------------------------------------------------------

function _rehydrateHistory(items: HistoryMessage[]): ChatMessage[] {
  // Build a quick index: tool rows are keyed by their tool_call_id and
  // attached as ToolInvocation entries on the assistant row that owns
  // the matching tool_calls[].id. Assistant rows without tool_calls
  // pass through unchanged.
  const toolRowsById = new Map<string, HistoryMessage>();
  for (const m of items) {
    if (m.role === "tool" && m.tool_call_id) toolRowsById.set(m.tool_call_id, m);
  }
  const out: ChatMessage[] = [];
  for (const m of items) {
    if (m.role === "tool") continue; // attached below to its assistant
    if (m.role === "user") {
      out.push({
        clientId: m.id,
        role: "user",
        content: m.content,
        createdAt: new Date(m.created_at).getTime(),
      });
      continue;
    }
    if (m.role === "assistant") {
      const calls = (m.tool_calls as unknown[] | null) ?? [];
      const toolCalls: ToolInvocation[] = [];
      for (const raw of calls) {
        const tc = raw as {
          id?: string;
          function?: { name?: string; arguments?: string };
        };
        const id = tc.id ?? "";
        const fn = tc.function ?? {};
        let args: Record<string, unknown> = {};
        try {
          args = fn.arguments ? JSON.parse(fn.arguments) : {};
        } catch {
          args = { _raw: fn.arguments ?? "" };
        }
        const resultRow = id ? toolRowsById.get(id) : undefined;
        let parsedResult: unknown = undefined;
        if (resultRow?.content) {
          try {
            parsedResult = JSON.parse(resultRow.content);
          } catch {
            parsedResult = resultRow.content;
          }
        }
        const ok =
          parsedResult &&
          typeof parsedResult === "object" &&
          "ok" in (parsedResult as Record<string, unknown>)
            ? (parsedResult as { ok?: unknown }).ok !== false
            : true;
        toolCalls.push({
          id,
          name: fn.name ?? "",
          args,
          result: parsedResult,
          status: resultRow ? (ok ? "ok" : "error") : "pending",
        });
      }
      out.push({
        clientId: m.id,
        role: "assistant",
        content: m.content,
        toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
        parserStatus: m.parser_status ?? null,
        createdAt: new Date(m.created_at).getTime(),
      });
      continue;
    }
    // system or unknown: skip
  }
  return out;
}
