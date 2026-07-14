"use client";

import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Stethoscope,
  Wand2,
  X,
} from "lucide-react";
import { useMemo } from "react";

import { cn } from "@/lib/utils";
import type {
  HealFinding,
  HealHealth,
  HealSeverity,
  WorkflowDefinition,
} from "@/types/api";

import { useHealStream } from "./useHealStream";

interface HealPanelProps {
  open: boolean;
  onClose: () => void;
  workflowId: string | null;
  disabled: boolean;
  /** A diff preview is already pending on the canvas. */
  pendingPreview: boolean;
  /** Hand the proposed patch to the canvas' existing diff-preview flow. */
  onProposeFix: (proposed: WorkflowDefinition) => void;
}

const SEVERITY_ORDER: Record<HealSeverity, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};

const SEVERITY_STYLES: Record<HealSeverity, string> = {
  critical:
    "border-rose-300 bg-rose-50 text-rose-800 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-200",
  high:
    "border-rose-300 bg-rose-50 text-rose-800 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-200",
  medium:
    "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200",
  low:
    "border-slate-300 bg-slate-100 text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

const HEALTH_STYLES: Record<HealHealth, string> = {
  healthy:
    "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  degraded:
    "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200",
  broken:
    "border-rose-300 bg-rose-50 text-rose-800 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-200",
  unknown:
    "border-slate-300 bg-slate-100 text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

const PHASE_LABELS: Record<string, string> = {
  heal_start: "Starting…",
  evidence: "Gathering run evidence…",
  diagnosis: "Diagnosing…",
  validation: "Validating the fix against the engine…",
  verification: "Simulating the repaired workflow…",
  propose: "Ready to review.",
  healing_report: "Done.",
};

function Finding({ finding }: { finding: HealFinding }) {
  return (
    <li className="rounded-lg border border-slate-200 p-2.5 dark:border-slate-800">
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase",
            SEVERITY_STYLES[finding.severity],
          )}
        >
          {finding.severity}
        </span>
        <span className="text-[10px] font-medium text-slate-500 dark:text-slate-400">
          {finding.category.replace(/_/g, " ")}
        </span>
        {finding.auto_fixable ? (
          <span className="ml-auto inline-flex items-center gap-1 rounded-full bg-brand-50 px-2 py-0.5 text-[10px] font-medium text-brand-800 dark:bg-brand-950 dark:text-brand-200">
            <Wand2 className="h-3 w-3" />
            auto-fixable
          </span>
        ) : (
          <span className="ml-auto rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-500 dark:bg-slate-800 dark:text-slate-400">
            needs you
          </span>
        )}
      </div>
      <p className="mt-1.5 text-[12px] font-medium text-slate-800 dark:text-slate-100">
        {finding.summary}
      </p>
      {finding.node_ids.length > 0 ? (
        <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
          Nodes: {finding.node_ids.join(", ")}
        </p>
      ) : null}
      {finding.proposed_fix ? (
        <p className="mt-1 text-[11px] text-slate-600 dark:text-slate-300">
          <span className="font-semibold">Fix:</span> {finding.proposed_fix}
        </p>
      ) : null}
    </li>
  );
}

export function HealPanel({
  open,
  onClose,
  workflowId,
  disabled,
  pendingPreview,
  onProposeFix,
}: HealPanelProps) {
  const { state, start, reset } = useHealStream(workflowId);

  const findings = useMemo(
    () =>
      [...(state.report?.findings ?? [])].sort(
        (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity],
      ),
    [state.report],
  );

  if (!open) return null;

  const isRunning = state.status === "running";
  const canReview = state.proposed !== null;

  return (
    <div className="fixed right-0 top-0 z-30 flex h-full w-[400px] max-w-[92vw] flex-col border-l border-slate-200 bg-white shadow-2xl dark:border-slate-800 dark:bg-slate-950">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
        <Stethoscope className="h-4 w-4 text-brand-600 dark:text-brand-300" />
        <span className="text-[13px] font-semibold text-slate-800 dark:text-slate-100">
          Diagnose &amp; Heal
        </span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto rounded-md p-1 text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800"
          title="Close"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 space-y-3 overflow-y-auto p-4 text-slate-700 dark:text-slate-200">
        {disabled ? (
          <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
            Save the workflow before diagnosing.
          </p>
        ) : state.status === "idle" ? (
          <p className="text-[12px] text-slate-500 dark:text-slate-400">
            Analyze this workflow&apos;s recent runs (or a simulated run if it
            has none), surface what&apos;s wrong, and propose an
            engine-validated fix for you to review — nothing is saved until you
            accept it.
          </p>
        ) : null}

        {isRunning ? (
          <p className="flex items-center gap-2 text-[12px] text-slate-600 dark:text-slate-300">
            <Loader2 className="h-4 w-4 animate-spin" />
            {PHASE_LABELS[state.phase ?? ""] ?? "Working…"}
          </p>
        ) : null}

        {state.error ? (
          <p className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] text-rose-700 dark:border-rose-950 dark:bg-rose-950/40 dark:text-rose-300">
            {state.error}
          </p>
        ) : null}

        {state.report ? (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  "rounded-full border px-2.5 py-0.5 text-[11px] font-semibold capitalize",
                  HEALTH_STYLES[state.report.health],
                )}
              >
                {state.report.health}
              </span>
              <span className="text-[11px] text-slate-500 dark:text-slate-400">
                evidence: {state.evidenceSource ?? state.report.evidence_source}
              </span>
            </div>
            {state.report.summary ? (
              <p className="text-[12px] text-slate-600 dark:text-slate-300">
                {state.report.summary}
              </p>
            ) : null}
          </div>
        ) : null}

        {findings.length > 0 ? (
          <ul className="space-y-2">
            {findings.map((f) => (
              <Finding key={f.finding_id} finding={f} />
            ))}
          </ul>
        ) : null}

        {state.changes.length > 0 ? (
          <div className="rounded-lg border border-slate-200 p-2.5 dark:border-slate-800">
            <p className="text-[11px] font-semibold text-slate-700 dark:text-slate-200">
              Proposed change{state.changes.length === 1 ? "" : "s"}
            </p>
            <ul className="mt-1.5 space-y-0.5 text-[11px]">
              {state.changes.map((c, i) => (
                <li key={i} className="flex gap-1.5">
                  <span className="opacity-50">•</span>
                  <span>{c}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {state.scopeWarnings.length > 0 ? (
          <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
            <p className="flex items-center gap-1 font-semibold">
              <AlertTriangle className="h-3 w-3" />
              Touched nodes outside the fix&apos;s scope
            </p>
            <p className="mt-0.5">
              Review the diff carefully: {state.scopeWarnings.join(", ")}
            </p>
          </div>
        ) : null}

        {state.requiredProviders.length > 0 ? (
          <p className="text-[11px] text-slate-500 dark:text-slate-400">
            Needs connected integrations: {state.requiredProviders.join(", ")}
          </p>
        ) : null}

        {state.verification ? (
          <div className="rounded-md border border-slate-200 px-3 py-2 text-[11px] dark:border-slate-800">
            <span className="font-semibold">Verification: </span>
            <span className="capitalize">{state.verification.verdict}</span>
            {state.verification.reason ? ` — ${state.verification.reason}` : ""}
          </div>
        ) : null}
      </div>

      {/* Footer actions */}
      <div className="border-t border-slate-200 p-3 dark:border-slate-800">
        {canReview ? (
          <button
            type="button"
            disabled={pendingPreview}
            onClick={() => {
              if (state.proposed) onProposeFix(state.proposed);
              onClose();
            }}
            className="flex w-full items-center justify-center gap-1.5 rounded-md bg-emerald-600 px-3 py-2 text-[12px] font-semibold text-white hover:bg-emerald-700 disabled:opacity-60"
            title={
              pendingPreview
                ? "Resolve the pending preview first"
                : "Show the proposed fix on the canvas"
            }
          >
            <CheckCircle2 className="h-4 w-4" />
            Review fix on canvas
          </button>
        ) : (
          <button
            type="button"
            disabled={disabled || isRunning}
            onClick={() => {
              reset();
              void start({ simulate: false });
            }}
            className="flex w-full items-center justify-center gap-1.5 rounded-md bg-brand-600 px-3 py-2 text-[12px] font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
          >
            {isRunning ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Stethoscope className="h-4 w-4" />
            )}
            {state.status === "done" || state.status === "error"
              ? "Diagnose again"
              : "Diagnose"}
          </button>
        )}
      </div>
    </div>
  );
}
