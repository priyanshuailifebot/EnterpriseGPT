"use client";

/**
 * Per-node-kind React Flow renderers — shared by the read-only
 * ``VisualEditor`` and the interactive ``InteractiveCanvas``.
 *
 * Extracted out of ``VisualEditor.tsx`` so the interactive editor can
 * reuse the exact same visual treatment without re-implementing every
 * node card. The components themselves are pure read-only views — they
 * never mutate; mutations are driven through the editor store.
 */

import {
  type EdgeProps,
  getBezierPath,
  Handle,
  type NodeProps,
  Position,
} from "@xyflow/react";
import {
  Brain,
  Braces,
  Clock,
  Cpu,
  GitBranch,
  GitMerge,
  Repeat2,
  Sparkles,
  Table2,
  Webhook,
  Wrench,
  Zap,
} from "lucide-react";

import {
  resolveProviderForSlug,
  uniqueProvidersForTools,
  type ProviderTag,
} from "@/components/workflow/integration-icons";
import { type FlowNodeData, type SatelliteEntry } from "./workflow-topology";
import { cn } from "@/lib/utils";
import type {
  ActionNode,
  AgentNode,
  ConditionNode,
  DataStoreNode,
  ForEachNode,
  IfNode,
  MemoryNode,
  OutputParserNode,
  TriggerNode,
  WaitForWebhookNode,
  WorkflowNode,
} from "@/types/api";

// ---------------------------------------------------------------------------
// Integration chip
// ---------------------------------------------------------------------------

function IntegrationChip({ tag }: { tag: ProviderTag }) {
  return (
    <span
      title={tag.label}
      aria-label={tag.label}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-medium",
        tag.bg,
        tag.fg,
      )}
    >
      {tag.icon}
      <span className="leading-none">{tag.label}</span>
    </span>
  );
}

function IntegrationStrip({ tools }: { tools: string[] }) {
  const tags = uniqueProvidersForTools(tools);
  if (tags.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {tags.map((t) => (
        <IntegrationChip key={t.id} tag={t} />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Agent
// ---------------------------------------------------------------------------

export function AgentFlowNode(props: NodeProps) {
  const data = props.data as unknown as FlowNodeData & {
    satelliteCount?: { tools: number; memory: boolean; parser: boolean; model: boolean };
  };
  const ag = data.raw as AgentNode;
  const counts = data.satelliteCount;
  const hasSatellites =
    !!counts && (counts.tools > 0 || counts.memory || counts.parser || counts.model);
  return (
    <div
      className={cn(
        "rounded-2xl border-2 bg-white px-4 py-3 text-left shadow-lg dark:bg-slate-950",
        data.checkpoint
          ? "border-dashed border-amber-400 ring-2 ring-amber-200 dark:border-amber-500 dark:ring-amber-950"
          : hasSatellites
            ? "border-brand-500 dark:border-brand-400"
            : "border-slate-300 dark:border-slate-700",
        props.selected && "ring-2 ring-brand-400",
      )}
      style={{ minWidth: 240, maxWidth: 300 }}
    >
      <Handle
        id="tgt"
        type="target"
        position={Position.Left}
        className="!border-slate-400 !bg-slate-900 dark:!border-slate-600 dark:!bg-slate-200"
      />
      <Handle
        id="src"
        type="source"
        position={Position.Right}
        className="!border-brand-600 !bg-brand-500"
      />
      <div className="flex items-center gap-2">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-brand-50 text-brand-700 dark:bg-brand-950 dark:text-brand-300">
          <Sparkles className="h-3.5 w-3.5" />
        </div>
        <p className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
          {ag.name}
        </p>
        {data.checkpoint ? (
          <Clock className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
        ) : null}
      </div>
      <p className="mt-1 line-clamp-2 text-xs text-slate-600 dark:text-slate-400">
        {ag.role || "Agent"}
      </p>
      {hasSatellites ? (
        <div className="mt-2 flex flex-wrap gap-1 text-[10px]">
          {counts!.model ? (
            <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-slate-600 dark:bg-slate-800 dark:text-slate-300">
              model
            </span>
          ) : null}
          {counts!.memory ? (
            <span className="rounded-full bg-violet-100 px-1.5 py-0.5 text-violet-700 dark:bg-violet-950 dark:text-violet-300">
              memory
            </span>
          ) : null}
          {counts!.tools > 0 ? (
            <span className="rounded-full bg-brand-50 px-1.5 py-0.5 text-brand-700 dark:bg-brand-950 dark:text-brand-300">
              {counts!.tools} tool{counts!.tools === 1 ? "" : "s"}
            </span>
          ) : null}
          {counts!.parser ? (
            <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-amber-700 dark:bg-amber-950 dark:text-amber-300">
              parser
            </span>
          ) : null}
        </div>
      ) : (
        <IntegrationStrip tools={ag.tools} />
      )}
    </div>
  );
}

export function ConditionFlowNode(props: NodeProps) {
  const data = props.data as unknown as FlowNodeData;
  const cn_ = data.raw as ConditionNode;
  return (
    <div
      className={cn(
        "rounded-2xl border-2 border-indigo-500/60 bg-indigo-50/80 px-4 py-3 text-left shadow-lg dark:border-indigo-400/50 dark:bg-indigo-950/70",
        props.selected && "ring-2 ring-indigo-400",
      )}
      style={{ minWidth: 220, maxWidth: 280 }}
    >
      <Handle id="tgt" type="target" position={Position.Left} className="!bg-indigo-600" />
      <Handle id="src" type="source" position={Position.Right} className="!bg-indigo-600" />
      <div className="flex items-center gap-2 text-indigo-700 dark:text-indigo-300">
        <GitBranch className="h-4 w-4" />
        <span className="text-[10px] font-semibold uppercase tracking-wide">
          Condition
        </span>
      </div>
      <p className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
        {cn_.name}
      </p>
      <p className="line-clamp-2 text-xs text-slate-600 dark:text-slate-400">
        {cn_.expression}
      </p>
      <div className="mt-2 flex flex-wrap gap-1">
        {cn_.branches.map((b) => (
          <span
            key={b}
            className="rounded-full bg-indigo-100 px-2 py-0.5 text-[10px] font-medium text-indigo-700 dark:bg-indigo-900 dark:text-indigo-200"
          >
            {b}
          </span>
        ))}
      </div>
    </div>
  );
}

export function ForEachFlowNode(props: NodeProps) {
  const data = props.data as unknown as FlowNodeData;
  const fe = data.raw as ForEachNode;
  return (
    <div
      className={cn(
        "rounded-2xl border-2 border-dashed border-emerald-500/70 bg-emerald-50/80 px-4 py-3 text-left shadow-lg dark:border-emerald-400/60 dark:bg-emerald-950/60",
        props.selected && "ring-2 ring-emerald-400",
      )}
      style={{ minWidth: 220, maxWidth: 280 }}
    >
      <Handle id="tgt" type="target" position={Position.Left} className="!bg-emerald-600" />
      <Handle id="src" type="source" position={Position.Right} className="!bg-emerald-600" />
      <div className="flex items-center gap-2 text-emerald-700 dark:text-emerald-300">
        <Repeat2 className="h-4 w-4" />
        <span className="text-[10px] font-semibold uppercase tracking-wide">
          For each
        </span>
      </div>
      <p className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
        {fe.name}
      </p>
      <p className="line-clamp-2 text-xs text-slate-600 dark:text-slate-400">
        from <code className="rounded bg-emerald-100 px-1 dark:bg-emerald-900">{fe.items_from || "—"}</code>
        {" "}as <code className="rounded bg-emerald-100 px-1 dark:bg-emerald-900">{fe.item_var}</code>
      </p>
      <div className="mt-2 flex flex-wrap gap-1 text-[10px] text-emerald-700 dark:text-emerald-300">
        <span className="rounded-full bg-emerald-100 px-2 py-0.5 dark:bg-emerald-900">
          body: {fe.body.length}
        </span>
        <span className="rounded-full bg-emerald-100 px-2 py-0.5 dark:bg-emerald-900">
          ⤴ concurrency {fe.max_concurrency}
        </span>
      </div>
    </div>
  );
}

export function MergeFlowNode(props: NodeProps) {
  const data = props.data as unknown as FlowNodeData;
  return (
    <div
      className={cn(
        "rounded-2xl border-2 border-slate-400 bg-slate-50 px-4 py-3 text-center shadow-md dark:border-slate-500 dark:bg-slate-900",
        props.selected && "ring-2 ring-slate-400",
      )}
      style={{ minWidth: 160 }}
    >
      <Handle id="tgt" type="target" position={Position.Left} className="!bg-slate-700" />
      <Handle id="src" type="source" position={Position.Right} className="!bg-slate-700" />
      <p className="text-[10px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        Merge
      </p>
      <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">
        {(data.raw as WorkflowNode).name}
      </p>
    </div>
  );
}

export function WaitForWebhookFlowNode(props: NodeProps) {
  const data = props.data as unknown as FlowNodeData;
  const w = data.raw as WaitForWebhookNode;
  const hours = Math.round(w.timeout_seconds / 3600);
  return (
    <div
      className={cn(
        "rounded-2xl border-2 border-amber-400/70 bg-amber-50/80 px-4 py-3 text-left shadow-lg dark:border-amber-400/60 dark:bg-amber-950/40",
        props.selected && "ring-2 ring-amber-400",
      )}
      style={{ minWidth: 220, maxWidth: 280 }}
    >
      <Handle id="tgt" type="target" position={Position.Left} className="!bg-amber-600" />
      <Handle id="src" type="source" position={Position.Right} className="!bg-amber-600" />
      <div className="flex items-center gap-2 text-amber-700 dark:text-amber-300">
        <Webhook className="h-4 w-4" />
        <span className="text-[10px] font-semibold uppercase tracking-wide">
          Wait for webhook
        </span>
      </div>
      <p className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
        {w.name}
      </p>
      {w.description ? (
        <p className="line-clamp-2 text-xs text-slate-600 dark:text-slate-400">
          {w.description}
        </p>
      ) : null}
      <p className="mt-2 text-[10px] text-amber-700 dark:text-amber-300">
        ⏱ up to {hours >= 1 ? `${hours}h` : `${w.timeout_seconds}s`}
      </p>
    </div>
  );
}

export function TriggerFlowNode(props: NodeProps) {
  const data = props.data as unknown as FlowNodeData;
  const t = data.raw as TriggerNode;
  const sub = t.trigger_type;
  const accent =
    sub === "form" ? "border-sky-500 bg-sky-50 dark:bg-sky-950/50"
    : sub === "webhook" ? "border-rose-500 bg-rose-50 dark:bg-rose-950/50"
    : sub === "schedule" ? "border-fuchsia-500 bg-fuchsia-50 dark:bg-fuchsia-950/50"
    : sub === "chat" ? "border-violet-500 bg-violet-50 dark:bg-violet-950/50"
    : "border-emerald-500 bg-emerald-50 dark:bg-emerald-950/50";
  return (
    <div
      className={cn(
        "rounded-2xl border-2 px-4 py-3 text-left shadow-lg",
        accent,
        props.selected && "ring-2 ring-emerald-400",
      )}
      style={{ minWidth: 220 }}
    >
      <Handle id="src" type="source" position={Position.Right} className="!bg-emerald-600" />
      <div className="flex items-center gap-2 text-emerald-700 dark:text-emerald-300">
        <Zap className="h-4 w-4" />
        <span className="text-[10px] font-semibold uppercase tracking-wide">
          Trigger · {sub}
        </span>
      </div>
      <p className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
        {t.name}
      </p>
      {t.slug ? (
        <p className="mt-1 truncate font-mono text-[10px] text-slate-500 dark:text-slate-400">
          /{sub === "form" ? "forms" : sub === "chat" ? "chat" : "triggers"}/{t.slug}
        </p>
      ) : null}
    </div>
  );
}

export function ActionFlowNode(props: NodeProps) {
  const data = props.data as unknown as FlowNodeData;
  const a = data.raw as ActionNode;
  const tag =
    resolveProviderForSlug(a.action_slug) ??
    (a.provider ? _providerTagForId(a.provider) : null);
  return (
    <div
      className={cn(
        "rounded-2xl border border-slate-300 bg-white px-3 py-3 text-left shadow-lg dark:border-slate-700 dark:bg-slate-950",
        props.selected && "ring-2 ring-brand-400",
      )}
      style={{ minWidth: 220, maxWidth: 280 }}
    >
      <Handle id="tgt" type="target" position={Position.Left} className="!bg-slate-700" />
      <Handle id="src" type="source" position={Position.Right} className="!bg-slate-700" />
      <div className="flex items-start gap-3">
        <div
          className={cn(
            "flex h-10 w-10 shrink-0 items-center justify-center rounded-xl",
            tag ? tag.bg : "bg-slate-100 dark:bg-slate-800",
            tag ? tag.fg : "text-slate-600 dark:text-slate-300",
          )}
        >
          {tag ? tag.icon : <Sparkles className="h-4 w-4" />}
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
            {a.name}
          </p>
          <p className="truncate font-mono text-[10px] text-slate-500 dark:text-slate-400">
            {tag?.label ?? a.provider} · {a.action_slug}
          </p>
        </div>
      </div>
      {a.allow_dry_run ? (
        <div className="mt-2 flex items-center gap-1 text-[10px] text-amber-700 dark:text-amber-400">
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-amber-500" />
          dry-run if no connection
        </div>
      ) : null}
    </div>
  );
}

function _providerTagForId(id: string): ProviderTag | null {
  const synthetic = `${id}_`;
  return resolveProviderForSlug(synthetic) ?? resolveProviderForSlug(id);
}

export function IfFlowNode(props: NodeProps) {
  const data = props.data as unknown as FlowNodeData;
  const i = data.raw as IfNode;
  return (
    <div
      className={cn(
        "rounded-2xl border-2 border-cyan-500/60 bg-cyan-50/80 px-4 py-3 text-left shadow-lg dark:border-cyan-400/50 dark:bg-cyan-950/50",
        props.selected && "ring-2 ring-cyan-400",
      )}
      style={{ minWidth: 220, maxWidth: 280 }}
    >
      <Handle id="tgt" type="target" position={Position.Left} className="!bg-cyan-600" />
      <Handle id="src" type="source" position={Position.Right} className="!bg-cyan-600" />
      <div className="flex items-center gap-2 text-cyan-700 dark:text-cyan-300">
        <GitMerge className="h-4 w-4 rotate-90" />
        <span className="text-[10px] font-semibold uppercase tracking-wide">If</span>
      </div>
      <p className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
        {i.name}
      </p>
      <p className="mt-1 truncate font-mono text-[10px] text-slate-600 dark:text-slate-400">
        {i.expression}
      </p>
      <div className="mt-2 flex gap-1">
        <span className="rounded-full bg-cyan-100 px-2 py-0.5 text-[10px] font-medium text-cyan-700 dark:bg-cyan-900 dark:text-cyan-200">
          true
        </span>
        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-300">
          false
        </span>
      </div>
    </div>
  );
}

export function DataStoreFlowNode(props: NodeProps) {
  const data = props.data as unknown as FlowNodeData;
  const d = data.raw as DataStoreNode;
  const opAccent =
    d.op === "write" ? "text-rose-700 dark:text-rose-300"
    : d.op === "read" ? "text-blue-700 dark:text-blue-300"
    : "text-violet-700 dark:text-violet-300";
  return (
    <div
      className={cn(
        "rounded-2xl border-2 border-slate-300 bg-slate-50 px-4 py-3 text-left shadow-lg dark:border-slate-700 dark:bg-slate-900",
        props.selected && "ring-2 ring-slate-400",
      )}
      style={{ minWidth: 220, maxWidth: 280 }}
    >
      <Handle id="tgt" type="target" position={Position.Left} className="!bg-slate-700" />
      <Handle id="src" type="source" position={Position.Right} className="!bg-slate-700" />
      <div className={cn("flex items-center gap-2", opAccent)}>
        <Table2 className="h-4 w-4" />
        <span className="text-[10px] font-semibold uppercase tracking-wide">
          Data · {d.op}
        </span>
      </div>
      <p className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
        {d.name}
      </p>
      <p className="mt-1 truncate font-mono text-[10px] text-slate-500 dark:text-slate-400">
        table: {d.table}
        {d.key ? ` · key: ${d.key}` : ""}
      </p>
    </div>
  );
}

// Memory / OutputParser placeholder cards for when they appear as
// standalone nodes (shared between an agent and a trigger, for example,
// or before the user binds them to a parent).

export function MemoryFlowNode(props: NodeProps) {
  const data = props.data as unknown as FlowNodeData;
  const m = data.raw as MemoryNode;
  return (
    <div
      className={cn(
        "rounded-2xl border-2 border-violet-400/70 bg-violet-50 px-4 py-3 text-left shadow-md dark:border-violet-500/60 dark:bg-violet-950/40",
        props.selected && "ring-2 ring-violet-400",
      )}
      style={{ minWidth: 200, maxWidth: 260 }}
    >
      <Handle id="tgt" type="target" position={Position.Left} className="!bg-violet-600" />
      <Handle id="src" type="source" position={Position.Right} className="!bg-violet-600" />
      <div className="flex items-center gap-2 text-violet-700 dark:text-violet-300">
        <Brain className="h-4 w-4" />
        <span className="text-[10px] font-semibold uppercase tracking-wide">Memory</span>
      </div>
      <p className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
        {m.name}
      </p>
      <p className="text-[10px] text-slate-500 dark:text-slate-400">
        {m.scope} · {m.store} · {m.max_turns} turns
      </p>
    </div>
  );
}

export function OutputParserFlowNode(props: NodeProps) {
  const data = props.data as unknown as FlowNodeData;
  const p = data.raw as OutputParserNode;
  return (
    <div
      className={cn(
        "rounded-2xl border-2 border-amber-400/70 bg-amber-50 px-4 py-3 text-left shadow-md dark:border-amber-500/60 dark:bg-amber-950/40",
        props.selected && "ring-2 ring-amber-400",
      )}
      style={{ minWidth: 200, maxWidth: 260 }}
    >
      <Handle id="tgt" type="target" position={Position.Left} className="!bg-amber-600" />
      <Handle id="src" type="source" position={Position.Right} className="!bg-amber-600" />
      <div className="flex items-center gap-2 text-amber-700 dark:text-amber-300">
        <Braces className="h-4 w-4" />
        <span className="text-[10px] font-semibold uppercase tracking-wide">Output Parser</span>
      </div>
      <p className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
        {p.name}
      </p>
      <p className="text-[10px] text-slate-500 dark:text-slate-400">
        {Object.keys(p.json_schema ?? {}).length} schema field
        {Object.keys(p.json_schema ?? {}).length === 1 ? "" : "s"} · retries {p.max_retries}
      </p>
    </div>
  );
}

// Satellite chip — the small floating renderer used by the read-only
// editor. Kept here for re-use; the interactive canvas doesn't render
// satellites as graph nodes (they live under their parent agent visually).

export interface SatelliteFlowData extends Record<string, unknown> {
  slot: "model" | "memory" | "tool" | "output_parser";
  node: WorkflowNode | { kind: "model"; label: string; provider: string };
  agentId: string;
}

export function SatelliteFlowNode(props: NodeProps) {
  const data = props.data as unknown as SatelliteFlowData;
  const slotLabel =
    data.slot === "model" ? "Chat Model"
    : data.slot === "memory" ? "Memory"
    : data.slot === "output_parser" ? "Output Parser"
    : "Tool";

  let icon = <Wrench className="h-4 w-4" />;
  let title = "Tool";
  let subtitle = "";
  let accent = "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200";

  if (data.slot === "model") {
    const m = data.node as { label: string; provider: string };
    title = m.label;
    subtitle = m.provider;
    icon = <Cpu className="h-4 w-4" />;
    const prov = resolveProviderForSlug(m.provider);
    if (prov) accent = `${prov.bg} ${prov.fg}`;
  } else if (data.slot === "memory") {
    const m = data.node as MemoryNode;
    title = m.name;
    subtitle = `${m.scope} · ${m.store}`;
    icon = <Brain className="h-4 w-4" />;
    accent = "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300";
  } else if (data.slot === "output_parser") {
    const p = data.node as OutputParserNode;
    title = p.name;
    subtitle = `${Object.keys(p.json_schema ?? {}).length} field${
      Object.keys(p.json_schema ?? {}).length === 1 ? "" : "s"
    }`;
    icon = <Braces className="h-4 w-4" />;
    accent = "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300";
  } else {
    if ((data.node as ActionNode).kind === "action") {
      const a = data.node as ActionNode;
      title = a.name;
      subtitle = a.action_slug;
      const prov = resolveProviderForSlug(a.action_slug);
      if (prov) {
        accent = `${prov.bg} ${prov.fg}`;
        icon = <span className="inline-flex">{prov.icon}</span>;
      }
    } else if ((data.node as DataStoreNode).kind === "data_store") {
      const d = data.node as DataStoreNode;
      title = d.name;
      subtitle = `${d.op}: ${d.table}`;
      icon = <Table2 className="h-4 w-4" />;
    }
  }

  return (
    <div className="text-center" style={{ width: 110 }}>
      <Handle
        id="tgt"
        type="target"
        position={Position.Top}
        className="!h-2 !w-2 !border-slate-400 !bg-transparent"
      />
      <div
        className={cn(
          "mx-auto flex h-14 w-14 items-center justify-center rounded-2xl border border-slate-300 shadow-sm dark:border-slate-700",
          accent,
        )}
      >
        {icon}
      </div>
      <p className="mt-2 truncate text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {slotLabel}
      </p>
      <p
        className="mt-0.5 truncate text-[11px] font-semibold text-slate-900 dark:text-slate-100"
        title={title}
      >
        {title}
      </p>
      {subtitle ? (
        <p
          className="truncate font-mono text-[10px] text-slate-500 dark:text-slate-400"
          title={subtitle}
        >
          {subtitle}
        </p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Edge renderer — branch label pill rendered at the midpoint.
// ---------------------------------------------------------------------------

interface BranchEdgeData {
  branchLabel?: string;
  fromForEach?: boolean;
}

export function BranchEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  markerEnd,
}: EdgeProps) {
  const [path, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });
  const d = (data ?? {}) as BranchEdgeData;
  const stroke = d.branchLabel ? "#6366f1" : d.fromForEach ? "#059669" : "#94a3b8";
  return (
    <>
      <path
        id={id}
        d={path}
        fill="none"
        stroke={stroke}
        strokeWidth={d.branchLabel || d.fromForEach ? 2 : 1.5}
        markerEnd={markerEnd}
      />
      {d.branchLabel ? (
        <foreignObject
          x={labelX - 36}
          y={labelY - 12}
          width={72}
          height={24}
        >
          <div className="flex h-6 items-center justify-center rounded-full bg-indigo-600 px-2 text-[10px] font-medium text-white shadow">
            {d.branchLabel}
          </div>
        </foreignObject>
      ) : null}
    </>
  );
}

// ---------------------------------------------------------------------------
// Maps
// ---------------------------------------------------------------------------

export const AGENT_FLOW_NODE_TYPES = {
  agentFlow: AgentFlowNode,
  actionFlow: ActionFlowNode,
  conditionFlow: ConditionFlowNode,
  ifFlow: IfFlowNode,
  forEachFlow: ForEachFlowNode,
  mergeFlow: MergeFlowNode,
  waitFlow: WaitForWebhookFlowNode,
  triggerFlow: TriggerFlowNode,
  dataStoreFlow: DataStoreFlowNode,
  memoryFlow: MemoryFlowNode,
  outputParserFlow: OutputParserFlowNode,
  satelliteFlow: SatelliteFlowNode,
};

export const BRANCH_EDGE_TYPES = {
  branch: BranchEdge,
};

export function flowTypeForKind(kind: FlowNodeData["kind"]): keyof typeof AGENT_FLOW_NODE_TYPES {
  switch (kind) {
    case "action":
      return "actionFlow";
    case "condition":
      return "conditionFlow";
    case "if":
      return "ifFlow";
    case "for_each":
      return "forEachFlow";
    case "merge":
      return "mergeFlow";
    case "wait_for_webhook":
      return "waitFlow";
    case "trigger":
      return "triggerFlow";
    case "data_store":
      return "dataStoreFlow";
    case "memory":
      return "memoryFlow";
    case "output_parser":
      return "outputParserFlow";
    default:
      return "agentFlow";
  }
}

// Re-export helpers used by VisualEditor's overrides.
export type { ProviderTag };
export { Wrench, Clock, Cpu };
export type SatelliteEntryExport = SatelliteEntry;
