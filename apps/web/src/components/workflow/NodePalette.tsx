"use client";

/**
 * Left-rail palette of node kinds the user can drag onto the canvas.
 *
 * Drag-and-drop uses the native HTML5 API (no extra library) — we set
 * ``application/x-egpt-node-kind`` on the drag payload, the canvas
 * reads it in ``onDrop`` and inserts the right node at the drop point.
 */

import {
  Braces,
  Brain,
  Clock,
  Cpu,
  GitBranch,
  GitMerge,
  Repeat2,
  Sparkles,
  Table2,
  Wand2,
  Webhook,
  Wrench,
  Zap,
} from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import {
  NODE_KIND_CATALOG,
  type NodeKind,
  type NodeKindCatalogEntry,
} from "./workflow-mutations";

export const PALETTE_DRAG_MIME = "application/x-egpt-node-kind";

const ICONS: Record<NodeKind, ReactNode> = {
  trigger: <Zap className="h-4 w-4" />,
  agent: <Sparkles className="h-4 w-4" />,
  action: <Wand2 className="h-4 w-4" />,
  condition: <GitBranch className="h-4 w-4" />,
  if: <GitMerge className="h-4 w-4 rotate-90" />,
  for_each: <Repeat2 className="h-4 w-4" />,
  merge: <GitMerge className="h-4 w-4" />,
  wait_for_webhook: <Webhook className="h-4 w-4" />,
  data_store: <Table2 className="h-4 w-4" />,
  memory: <Brain className="h-4 w-4" />,
  output_parser: <Braces className="h-4 w-4" />,
};

const CATEGORY_LABELS: Record<NodeKindCatalogEntry["category"], string> = {
  trigger: "Trigger",
  logic: "Control Flow",
  agent: "Agents",
  tool: "Tools",
  data: "Data",
};

const CATEGORY_ORDER: NodeKindCatalogEntry["category"][] = [
  "trigger",
  "agent",
  "logic",
  "tool",
  "data",
];

interface NodePaletteProps {
  /** Optional click handler — used as a fallback when DnD isn't supported. */
  onAddNode?: (kind: NodeKind) => void;
}

export function NodePalette({ onAddNode }: NodePaletteProps) {
  const grouped = new Map<NodeKindCatalogEntry["category"], NodeKindCatalogEntry[]>();
  for (const entry of NODE_KIND_CATALOG) {
    if (!grouped.has(entry.category)) grouped.set(entry.category, []);
    grouped.get(entry.category)!.push(entry);
  }

  return (
    <aside
      aria-label="Node palette"
      className="flex h-full w-64 shrink-0 flex-col gap-4 overflow-y-auto rounded-2xl border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-800 dark:bg-slate-950"
    >
      <div className="px-1">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
          Nodes
        </p>
        <p className="mt-0.5 text-[11px] leading-tight text-slate-500 dark:text-slate-500">
          Drag onto the canvas, or click to insert at the centre.
        </p>
      </div>

      {CATEGORY_ORDER.map((cat) => {
        const items = grouped.get(cat);
        if (!items) return null;
        return (
          <section key={cat} className="flex flex-col gap-1.5">
            <h3 className="px-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">
              {CATEGORY_LABELS[cat]}
            </h3>
            <ul className="flex flex-col gap-1">
              {items.map((entry) => (
                <li key={entry.kind}>
                  <PaletteItem entry={entry} onAddNode={onAddNode} />
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </aside>
  );
}

function PaletteItem({
  entry,
  onAddNode,
}: {
  entry: NodeKindCatalogEntry;
  onAddNode?: (kind: NodeKind) => void;
}) {
  return (
    <button
      type="button"
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData(PALETTE_DRAG_MIME, entry.kind);
        e.dataTransfer.effectAllowed = "copy";
      }}
      onClick={() => onAddNode?.(entry.kind)}
      title={entry.description}
      className={cn(
        "group flex w-full cursor-grab items-start gap-2 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2 text-left transition-colors",
        "hover:border-brand-400 hover:bg-brand-50 active:cursor-grabbing",
        "dark:border-slate-800 dark:bg-slate-900 dark:hover:border-brand-500 dark:hover:bg-brand-950/40",
      )}
    >
      <span
        className={cn(
          "mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md",
          categoryAccent(entry.category),
        )}
        aria-hidden
      >
        {ICONS[entry.kind]}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-[12px] font-semibold leading-tight text-slate-900 dark:text-slate-100">
          {entry.label}
        </span>
        <span className="mt-0.5 line-clamp-2 block text-[10px] leading-tight text-slate-500 dark:text-slate-400">
          {entry.description}
        </span>
      </span>
    </button>
  );
}

function categoryAccent(cat: NodeKindCatalogEntry["category"]): string {
  switch (cat) {
    case "trigger":
      return "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300";
    case "agent":
      return "bg-brand-100 text-brand-700 dark:bg-brand-950/60 dark:text-brand-300";
    case "logic":
      return "bg-indigo-100 text-indigo-700 dark:bg-indigo-950/60 dark:text-indigo-300";
    case "tool":
      return "bg-amber-100 text-amber-700 dark:bg-amber-950/60 dark:text-amber-300";
    case "data":
      return "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300";
    default:
      return "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300";
  }
}

// Re-export Wrench so callers don't have to dive back into lucide-react for
// the unfamiliar lookup; keeps the editor module compact.
export { Wrench, Clock, Cpu };
