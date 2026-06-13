/**
 * React hook that runs a workflow (real or demo) and exposes:
 *   - live ``ExecutionRunState`` derived via ``applyExecutionEvent``
 *   - per-node status map keyed by node id (for canvas overlays)
 *   - raw event log (for the timeline panel)
 *   - ``start`` / ``stop`` / ``reset`` controls
 *
 * The hook is intentionally framework-light: it talks directly to
 * ``ssePostStream`` and accumulates state in a ref, then flushes to
 * React state in batches via ``setExecution`` so a chatty stream
 * doesn't trigger a re-render per chunk.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ssePostStream } from "@/lib/api";
import type { ExecutionEvent } from "@/types/api";

import {
  applyExecutionEvent,
  INITIAL_EXECUTION_STATE,
  type ExecutionRunState,
} from "./execution-status";

export interface UseExecutionStreamOptions {
  workflowId: string;
}

export interface StartExecutionOptions {
  inputData: Record<string, unknown>;
  demo: boolean;
  /** When ``demo`` is true, optionally call the real Azure LLM for agent
   *  nodes. Integrations remain dry-run. Ignored when ``demo`` is false
   *  (production runs always use the real LLM). */
  useRealLlm?: boolean;
  /** Demo only: force specific condition/if nodes down a chosen branch so a
   *  test can exercise a particular path (``{node_id: branch_label}``). */
  branchOverrides?: Record<string, string>;
}

export interface UseExecutionStreamResult {
  state: ExecutionRunState;
  events: ExecutionEvent[];
  isRunning: boolean;
  start: (opts: StartExecutionOptions) => Promise<void>;
  stop: () => void;
  reset: () => void;
}

export function useExecutionStream(
  opts: UseExecutionStreamOptions,
): UseExecutionStreamResult {
  const { workflowId } = opts;
  const [state, setState] = useState<ExecutionRunState>(INITIAL_EXECUTION_STATE);
  const [events, setEvents] = useState<ExecutionEvent[]>([]);
  const [isRunning, setIsRunning] = useState(false);

  // We accumulate into refs so a chatty stream batches into one React
  // setState per microtask.
  const stateRef = useRef<ExecutionRunState>(INITIAL_EXECUTION_STATE);
  const pendingRef = useRef<ExecutionEvent[]>([]);
  const flushScheduledRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    // Cleanup on unmount — abort the stream so it doesn't continue
    // pushing into a stale state setter.
    return () => abortRef.current?.abort();
  }, []);

  const flushPending = useCallback(() => {
    flushScheduledRef.current = false;
    if (pendingRef.current.length === 0) return;
    const batch = pendingRef.current;
    pendingRef.current = [];
    setEvents((prev) => [...prev, ...batch]);
    setState(stateRef.current);
  }, []);

  const scheduleFlush = useCallback(() => {
    if (flushScheduledRef.current) return;
    flushScheduledRef.current = true;
    // queueMicrotask coalesces multiple events arriving in the same tick.
    queueMicrotask(flushPending);
  }, [flushPending]);

  const ingest = useCallback(
    (raw: Record<string, unknown>) => {
      const ev = raw as unknown as ExecutionEvent;
      stateRef.current = applyExecutionEvent(stateRef.current, ev);
      pendingRef.current.push(ev);
      scheduleFlush();
    },
    [scheduleFlush],
  );

  const reset = useCallback(() => {
    stateRef.current = INITIAL_EXECUTION_STATE;
    pendingRef.current = [];
    setEvents([]);
    setState(INITIAL_EXECUTION_STATE);
  }, []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    setIsRunning(false);
  }, []);

  const start = useCallback(
    async ({ inputData, demo, useRealLlm = false, branchOverrides }: StartExecutionOptions) => {
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      reset();
      setIsRunning(true);
      try {
        await ssePostStream({
          path: `/api/v1/workflows/${workflowId}/execute`,
          body: {
            input_data: inputData,
            variables: {},
            demo,
            // Only meaningful when demo=true; the backend ignores it otherwise.
            use_real_llm: demo ? useRealLlm : false,
            branch_overrides: branchOverrides ?? {},
          },
          signal: ctrl.signal,
          onEvent: ingest,
        });
      } catch (e: unknown) {
        if ((e as Error).name !== "AbortError") {
          stateRef.current = {
            ...stateRef.current,
            graphStatus: "error",
            errorMessage:
              e instanceof Error ? e.message : "Execution failed",
          };
          scheduleFlush();
        }
      } finally {
        // Ensure any buffered events make it into React state before
        // we flip isRunning off.
        flushPending();
        setIsRunning(false);
      }
    },
    [workflowId, ingest, reset, scheduleFlush, flushPending],
  );

  return { state, events, isRunning, start, stop, reset };
}
