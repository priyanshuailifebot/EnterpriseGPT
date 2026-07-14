"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ssePostStream } from "@/lib/api";
import type {
  HealEvent,
  HealingReport,
  HealVerdict,
  WorkflowDefinition,
} from "@/types/api";

export type HealStatus = "idle" | "running" | "done" | "error";

export interface HealState {
  status: HealStatus;
  phase: HealEvent["type"] | null;
  evidenceSource: string | null;
  report: HealingReport | null;
  proposed: WorkflowDefinition | null;
  changes: string[];
  scopeWarnings: string[];
  requiredProviders: string[];
  verification: { verdict: HealVerdict; reason: string } | null;
  error: string | null;
}

const INITIAL: HealState = {
  status: "idle",
  phase: null,
  evidenceSource: null,
  report: null,
  proposed: null,
  changes: [],
  scopeWarnings: [],
  requiredProviders: [],
  verification: null,
  error: null,
};

export interface StartHealOptions {
  simulate?: boolean;
  selectedFindingIds?: string[] | null;
  complaint?: string;
}

function reduce(prev: HealState, ev: HealEvent): HealState {
  switch (ev.type) {
    case "heartbeat":
    case "heal_start":
      return prev;
    case "evidence":
      return { ...prev, phase: ev.type, evidenceSource: ev.source ?? null };
    case "diagnosis":
      return { ...prev, phase: ev.type, report: ev.report ?? prev.report };
    case "validation":
      return {
        ...prev,
        phase: ev.type,
        changes: ev.changes ?? [],
        scopeWarnings: ev.scope_warnings ?? [],
        requiredProviders: ev.required_providers ?? [],
      };
    case "verification":
      return {
        ...prev,
        phase: ev.type,
        verification: {
          verdict: ev.verdict ?? "unknown",
          reason: ev.reason ?? "",
        },
      };
    case "propose":
      return {
        ...prev,
        phase: ev.type,
        report: ev.report ?? prev.report,
        proposed: ev.proposed_definition ?? null,
        changes: ev.changes ?? prev.changes,
        scopeWarnings: ev.scope_warnings ?? prev.scopeWarnings,
        requiredProviders: ev.required_providers ?? prev.requiredProviders,
      };
    case "healing_report":
      // Terminal, no auto-fixable patch (diagnosis-only outcome).
      return { ...prev, phase: ev.type, report: ev.report ?? prev.report };
    case "patch_failed":
      return {
        ...prev,
        phase: ev.type,
        error: ev.content ?? "Patch generation failed.",
      };
    case "error":
      return {
        ...prev,
        phase: ev.type,
        status: "error",
        error: ev.content ?? ev.error ?? "Heal failed.",
      };
    default:
      return prev;
  }
}

export interface UseHealStreamResult {
  state: HealState;
  start: (opts?: StartHealOptions) => Promise<void>;
  stop: () => void;
  reset: () => void;
}

/**
 * Consumes the POST /workflows/{id}/heal SSE stream. Mirrors
 * {@link useExecutionStream} but the heal stream is low-frequency (a handful of
 * events), so it updates state per event without microtask batching.
 */
export function useHealStream(workflowId: string | null): UseHealStreamResult {
  const [state, setState] = useState<HealState>(INITIAL);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setState(INITIAL);
  }, []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setState((prev) =>
      prev.status === "running" ? { ...prev, status: "idle" } : prev,
    );
  }, []);

  const start = useCallback(
    async (opts?: StartHealOptions) => {
      if (!workflowId) return;
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      setState({ ...INITIAL, status: "running" });
      try {
        await ssePostStream({
          path: `/api/v1/workflows/${workflowId}/heal`,
          body: {
            mode: "agent",
            complaint: opts?.complaint ?? "",
            selected_finding_ids: opts?.selectedFindingIds ?? null,
            simulate: opts?.simulate ?? false,
          },
          signal: ctrl.signal,
          onEvent: (raw) =>
            setState((prev) => reduce(prev, raw as unknown as HealEvent)),
        });
        setState((prev) =>
          prev.status === "error" ? prev : { ...prev, status: "done" },
        );
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setState((prev) => ({
          ...prev,
          status: "error",
          error: e instanceof Error ? e.message : "Heal failed.",
        }));
      }
    },
    [workflowId],
  );

  return { state, start, stop, reset };
}
