"use client";

/**
 * Slide-out drawer that shows what flowed in and out of one node during a
 * test run — the n8n "click a node, inspect its data" affordance.
 *
 * Reads purely from the canvas's ``ExecutionRunState`` (the per-node tree the
 * SSE reducer maintains). It does not fetch; the data is whatever the live or
 * just-finished run pushed via ``node_complete`` events. Edit-mode selection
 * (PropertyInspector) is suppressed by the canvas while this is open, so the
 * two right-rail panels never collide.
 */

import * as Dialog from "@radix-ui/react-dialog";
import { ArrowDownToLine, ArrowUpFromLine, Clock, X } from "lucide-react";

import { cn } from "@/lib/utils";

import type { NodeRunState } from "./execution-status";

interface NodeInspectDrawerProps {
  open: boolean;
  onClose: () => void;
  nodeId: string | null;
  runState: NodeRunState | undefined;
}

const STATUS_STYLES: Record<string, string> = {
  done: "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
  running:
    "border-blue-200 bg-blue-50 text-blue-800 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-200",
  error:
    "border-red-200 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200",
  skipped:
    "border-slate-200 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200",
  waiting:
    "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200",
  idle: "border-slate-200 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200",
};

function SnapshotBlock({
  title,
  icon,
  value,
}: {
  title: string;
  icon: React.ReactNode;
  value: unknown;
}) {
  const isEmpty =
    value == null ||
    (typeof value === "object" && Object.keys(value as object).length === 0);
  return (
    <section className="space-y-1.5">
      <p className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {icon}
        {title}
      </p>
      {isEmpty ? (
        <p className="rounded-lg border border-dashed border-slate-200 px-3 py-2 text-[11px] text-slate-400 dark:border-slate-700">
          No data captured for this node.
        </p>
      ) : (
        <pre className="max-h-72 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-100">
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </section>
  );
}

export function NodeInspectDrawer({
  open,
  onClose,
  nodeId,
  runState,
}: NodeInspectDrawerProps) {
  const status = runState?.status ?? "idle";
  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/20 backdrop-blur-sm" />
        <Dialog.Content
          className={cn(
            "fixed right-0 top-0 z-50 flex h-full w-[440px] flex-col gap-4 overflow-y-auto border-l border-slate-200 bg-white p-5 shadow-2xl",
            "dark:border-slate-800 dark:bg-slate-950",
          )}
        >
          <header className="flex items-start justify-between gap-2">
            <div>
              <Dialog.Title className="font-mono text-sm font-semibold text-slate-900 dark:text-slate-100">
                {nodeId ?? "node"}
              </Dialog.Title>
              <Dialog.Description className="text-[11px] text-slate-500 dark:text-slate-400">
                Test-run inspection — the data this node received and produced.
              </Dialog.Description>
            </div>
            <Dialog.Close className="rounded-md p-1 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800">
              <X className="h-4 w-4" />
            </Dialog.Close>
          </header>

          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                "rounded-full border px-2.5 py-0.5 text-[11px] font-semibold capitalize",
                STATUS_STYLES[status] ?? STATUS_STYLES.idle,
              )}
            >
              {status}
            </span>
            {runState?.nodeKind ? (
              <span className="rounded-full border border-slate-200 px-2.5 py-0.5 text-[11px] font-medium text-slate-600 dark:border-slate-700 dark:text-slate-300">
                {runState.nodeKind}
              </span>
            ) : null}
            {runState?.dryRun ? (
              <span className="rounded-full border border-amber-200 bg-amber-50 px-2.5 py-0.5 text-[11px] font-medium text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
                dry-run
              </span>
            ) : null}
            {typeof runState?.durationMs === "number" ? (
              <span className="flex items-center gap-1 text-[11px] text-slate-500 dark:text-slate-400">
                <Clock className="h-3 w-3" />
                {runState.durationMs} ms
              </span>
            ) : null}
          </div>

          {runState?.errorMessage ? (
            <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
              {runState.errorMessage}
            </p>
          ) : null}

          <SnapshotBlock
            title="Input"
            icon={<ArrowDownToLine className="h-3.5 w-3.5" />}
            value={runState?.inputSnapshot}
          />
          <SnapshotBlock
            title="Output"
            icon={<ArrowUpFromLine className="h-3.5 w-3.5" />}
            value={runState?.outputSnapshot}
          />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
