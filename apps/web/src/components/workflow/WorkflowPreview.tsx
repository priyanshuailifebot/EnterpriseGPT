"use client";

import {
  Braces,
  Brain,
  GitBranch,
  GitMerge,
  Plus,
  Repeat2,
  Sigma,
  Sparkles,
  Table2,
  Webhook,
  Zap,
} from "lucide-react";
import toast from "react-hot-toast";

import { AgentCard } from "@/components/workflow/AgentCard";
import { resolveProviderForSlug } from "@/components/workflow/integration-icons";
import {
  isSatellite,
  unifiedNodes,
} from "@/components/workflow/workflow-topology";
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
  WorkflowDefinition,
  WorkflowNode,
} from "@/types/api";

export interface WorkflowPreviewProps {
  definition: WorkflowDefinition;
  toolsCatalog: string[];
  saving: boolean;
  onDefinitionChange: (next: WorkflowDefinition) => void;
  onSaved: () => Promise<void>;
}

export function WorkflowPreview({
  definition,
  toolsCatalog,
  saving,
  onDefinitionChange,
  onSaved,
}: WorkflowPreviewProps) {
  function addAgent(): void {
    const id =
      typeof crypto.randomUUID === "function" ?
        crypto.randomUUID().slice(0, 8)
      : `agent_${definition.agents.length + 1}`;

    onDefinitionChange({
      ...definition,
      agents: [
        ...definition.agents,
        {
          id,
          name: `New agent (${id})`,
          role: "Specialist",
          instructions: "Describe responsibilities…",
          tools: [],
          depends_on:
            definition.agents.at(-1)?.id ?
              [definition.agents.at(-1)!.id]
            : [],
          is_parallel: false,
        },
      ],
    });
  }

  function updateAgent(agentId: string, nextAgent: WorkflowDefinition["agents"][0]) {
    onDefinitionChange({
      ...definition,
      agents: definition.agents.map((a) =>
        a.id === agentId ? nextAgent : a,
      ),
    });
  }

  function remove(agentId: string) {
    onDefinitionChange({
      ...definition,
      agents:
        definition.agents
          .filter((a) => a.id !== agentId)
          .map((a) => ({
            ...a,
            depends_on: a.depends_on.filter((d) => d !== agentId),
          })),
      human_checkpoints:
        definition.human_checkpoints.filter((h) => h !== agentId),
    });
  }

  function toggleCp(agentId: string, on: boolean) {
    const set = new Set(definition.human_checkpoints);
    if (on) set.add(agentId);
    else set.delete(agentId);
    onDefinitionChange({
      ...definition,
      human_checkpoints: [...set],
    });
  }

  async function handleSave(): Promise<void> {
    try {
      await onSaved();
    } catch (e: unknown) {
      toast.error(
        typeof e === "object" && e && "message" in e ?
          String((e as Error).message)
        : "Save failed",
      );
    }
  }

  const parallelGroups = bucketParallel(definition.agents);

  return (
    <div className="flex flex-col gap-6 lg:flex-row">
      <div className="min-w-0 flex-1 space-y-6">
        <div className="rounded-2xl border border-slate-200 bg-slate-50/60 p-4 dark:border-slate-800 dark:bg-slate-900/60">
          <label className="text-xs uppercase text-slate-500">
            Workflow name
          </label>
          <input
            value={definition.name}
            onChange={(e) =>
              onDefinitionChange({
                ...definition,
                name: e.target.value,
              })
            }
            className="mt-2 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-lg font-semibold dark:border-slate-700 dark:bg-slate-950"
          />
          <label className="mt-4 block text-xs uppercase text-slate-500">
            Description / trigger hint
          </label>
          <textarea
            value={definition.trigger || definition.description}
            onChange={(e) =>
              onDefinitionChange({
                ...definition,
                description: e.target.value,
                trigger: e.target.value,
              })
            }
            rows={3}
            className="mt-2 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
          />
        </div>

        {parallelGroups.length ?
          <div className="rounded-xl border border-slate-200 bg-white p-3 text-xs dark:border-slate-800 dark:bg-slate-900">
            <p className="font-semibold text-slate-700 dark:text-slate-100">
              Parallel groups (same dependency tier):
            </p>
            <ul className="mt-2 space-y-1 text-slate-600 dark:text-slate-300">
              {parallelGroups.map((g, i) => (
                <li key={i}>
                  • Tier {i + 1}: {g.map((id) => id).join(", ")}
                </li>
              ))}
            </ul>
          </div>
        : null}

        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={addAgent}
            className="inline-flex items-center gap-2 rounded-xl border border-slate-200 px-3 py-2 text-sm font-semibold hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
          >
            <Plus className="h-4 w-4" /> Add agent node
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={() => void handleSave()}
            className="rounded-xl bg-brand-600 px-5 py-2 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
          >
            {saving ? "Saving…" : "Save Workflow"}
          </button>
        </div>

        <div className="space-y-4">
          {definition.nodes && definition.nodes.length > 0 ?
            <V2NodeList
              definition={definition}
              toolsCatalog={toolsCatalog}
              onToggleCheckpoint={toggleCp}
              onChange={onDefinitionChange}
            />
          : definition.agents.map((ag, idx) => (
              <AgentCard
                key={ag.id}
                agent={ag}
                index={idx}
                allAgents={definition.agents}
                toolsCatalog={toolsCatalog}
                humanCheckpoint={definition.human_checkpoints.includes(ag.id)}
                onToggleCheckpoint={toggleCp}
                onChange={(na) => updateAgent(ag.id, na)}
                onRemove={remove}
              />
            ))
          }
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// V2 node list — read-only summary cards for non-agent kinds, full
// ``AgentCard`` editing for agent kinds.
// ---------------------------------------------------------------------------

function V2NodeList({
  definition,
  toolsCatalog,
  onToggleCheckpoint,
  onChange,
}: {
  definition: WorkflowDefinition;
  toolsCatalog: string[];
  onToggleCheckpoint: (id: string, on: boolean) => void;
  onChange: (next: WorkflowDefinition) => void;
}) {
  const nodes = unifiedNodes(definition);
  // For editing back-compat: when the user edits an agent inside a v2 graph,
  // we mutate the ``nodes`` array. The legacy ``agents`` field is left in
  // place (the API tolerates both; ``nodes`` is authoritative).
  const allAgents = nodes
    .filter((n): n is AgentNode => n.kind === "agent")
    .map((a) => ({
      id: a.id,
      name: a.name,
      role: a.role,
      instructions: a.instructions,
      tools: a.tools,
      depends_on: a.depends_on,
      is_parallel: a.is_parallel,
      activate_on: a.activate_on,
    }));

  function updateAgentNode(id: string, patched: AgentNode) {
    onChange({
      ...definition,
      nodes: nodes.map((n) => (n.id === id ? patched : n)),
    });
  }

  // Satellites are shown inline beneath their parent agent card below,
  // not as siblings in the top-level list.
  const topLevel = nodes.filter((n) => !isSatellite(n));
  const satellitesByParent = new Map<string, WorkflowNode[]>();
  for (const n of nodes) {
    if (!isSatellite(n)) continue;
    const pid = (n as unknown as { parent_agent_id?: string | null }).parent_agent_id ?? "";
    if (!pid) continue;
    const list = satellitesByParent.get(pid) ?? [];
    list.push(n);
    satellitesByParent.set(pid, list);
  }

  return (
    <>
      {topLevel.map((n, idx) => {
        if (n.kind === "agent") {
          const sats = satellitesByParent.get(n.id) ?? [];
          return (
            <div key={n.id} className="space-y-2">
              <AgentCard
                agent={{
                  id: n.id,
                  name: n.name,
                  role: n.role,
                  instructions: n.instructions,
                  tools: n.tools,
                  depends_on: n.depends_on,
                  is_parallel: n.is_parallel,
                  activate_on: n.activate_on,
                }}
                index={idx}
                allAgents={allAgents}
                toolsCatalog={toolsCatalog}
                humanCheckpoint={definition.human_checkpoints.includes(n.id)}
                onToggleCheckpoint={onToggleCheckpoint}
                onChange={(na) =>
                  updateAgentNode(n.id, {
                    ...n,
                    name: na.name,
                    role: na.role,
                    instructions: na.instructions,
                    tools: na.tools,
                    depends_on: na.depends_on,
                    is_parallel: na.is_parallel,
                    activate_on: na.activate_on ?? null,
                  })
                }
                onRemove={(id) =>
                  onChange({
                    ...definition,
                    nodes: nodes.filter((x) => x.id !== id),
                  })
                }
              />
              {sats.length > 0 ? (
                <div className="ml-6 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                  {sats.map((s) => (
                    <SatelliteSummary key={s.id} node={s} />
                  ))}
                </div>
              ) : null}
            </div>
          );
        }
        return <ControlFlowSummaryCard key={n.id} node={n} />;
      })}
    </>
  );
}

function ControlFlowSummaryCard({ node }: { node: WorkflowNode }) {
  if (node.kind === "condition") {
    const c = node as ConditionNode;
    return (
      <article className="rounded-2xl border-2 border-indigo-500/40 bg-indigo-50/60 p-4 dark:border-indigo-400/40 dark:bg-indigo-950/40">
        <div className="flex items-center gap-2 text-indigo-700 dark:text-indigo-300">
          <GitBranch className="h-4 w-4" />
          <span className="text-[10px] font-semibold uppercase tracking-wide">
            Condition
          </span>
        </div>
        <h3 className="mt-1 text-base font-semibold text-slate-900 dark:text-slate-100">
          {c.name}
        </h3>
        <p className="mt-1 text-sm text-slate-700 dark:text-slate-300">
          {c.expression}
        </p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {c.branches.map((b) => (
            <span
              key={b}
              className="rounded-full bg-indigo-100 px-2.5 py-0.5 text-[11px] font-medium text-indigo-700 dark:bg-indigo-900 dark:text-indigo-200"
            >
              {b}
            </span>
          ))}
        </div>
      </article>
    );
  }
  if (node.kind === "for_each") {
    const fe = node as ForEachNode;
    return (
      <article className="rounded-2xl border-2 border-dashed border-emerald-500/50 bg-emerald-50/60 p-4 dark:border-emerald-400/50 dark:bg-emerald-950/40">
        <div className="flex items-center gap-2 text-emerald-700 dark:text-emerald-300">
          <Repeat2 className="h-4 w-4" />
          <span className="text-[10px] font-semibold uppercase tracking-wide">
            For each
          </span>
        </div>
        <h3 className="mt-1 text-base font-semibold text-slate-900 dark:text-slate-100">
          {fe.name}
        </h3>
        <p className="mt-1 text-sm text-slate-700 dark:text-slate-300">
          For each <code className="rounded bg-emerald-100 px-1 dark:bg-emerald-900">{fe.item_var}</code> in <code className="rounded bg-emerald-100 px-1 dark:bg-emerald-900">{fe.items_from}{fe.items_path !== "$" ? `:${fe.items_path}` : ""}</code> · max {fe.max_concurrency} concurrent
        </p>
        <p className="mt-2 text-xs text-emerald-700 dark:text-emerald-300">
          Body: {fe.body.join(" → ") || "(empty)"}
        </p>
      </article>
    );
  }
  if (node.kind === "wait_for_webhook") {
    const w = node as WaitForWebhookNode;
    const hours = Math.round(w.timeout_seconds / 3600);
    return (
      <article className="rounded-2xl border-2 border-amber-400/60 bg-amber-50/60 p-4 dark:border-amber-400/50 dark:bg-amber-950/30">
        <div className="flex items-center gap-2 text-amber-700 dark:text-amber-300">
          <Webhook className="h-4 w-4" />
          <span className="text-[10px] font-semibold uppercase tracking-wide">
            Wait for webhook
          </span>
        </div>
        <h3 className="mt-1 text-base font-semibold text-slate-900 dark:text-slate-100">
          {w.name}
        </h3>
        {w.description ?
          <p className="mt-1 text-sm text-slate-700 dark:text-slate-300">
            {w.description}
          </p>
        : null}
        <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
          ⏱ parks for up to {hours >= 1 ? `${hours}h` : `${w.timeout_seconds}s`}
        </p>
      </article>
    );
  }
  if (node.kind === "trigger") {
    const t = node as TriggerNode;
    return (
      <article className="rounded-2xl border-2 border-emerald-500/60 bg-emerald-50/60 p-4 dark:border-emerald-400/50 dark:bg-emerald-950/40">
        <div className="flex items-center gap-2 text-emerald-700 dark:text-emerald-300">
          <Zap className="h-4 w-4" />
          <span className="text-[10px] font-semibold uppercase tracking-wide">
            Trigger · {t.trigger_type}
          </span>
        </div>
        <h3 className="mt-1 text-base font-semibold text-slate-900 dark:text-slate-100">
          {t.name}
        </h3>
        {t.slug ?
          <p className="mt-1 font-mono text-xs text-slate-600 dark:text-slate-400">
            /{t.trigger_type === "form" ? "forms" : "triggers"}/{t.slug}
          </p>
        : null}
        {t.trigger_type === "form" && t.form_fields.length > 0 ?
          <ul className="mt-2 space-y-1 text-xs text-slate-700 dark:text-slate-300">
            {t.form_fields.map((f) => (
              <li key={f.key}>
                <code className="rounded bg-emerald-100 px-1 dark:bg-emerald-900">
                  {f.key}
                </code>{" "}
                — {f.label}
                {f.required ? " *" : ""}
              </li>
            ))}
          </ul>
        : null}
      </article>
    );
  }
  if (node.kind === "action") {
    const a = node as ActionNode;
    const tag = resolveProviderForSlug(a.action_slug);
    return (
      <article className="rounded-2xl border border-slate-300 bg-white p-4 dark:border-slate-700 dark:bg-slate-950">
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
            <h3 className="text-base font-semibold text-slate-900 dark:text-slate-100">
              {a.name}
            </h3>
            <p className="font-mono text-xs text-slate-500 dark:text-slate-400">
              {tag?.label ?? a.provider} · {a.action_slug}
            </p>
            {a.allow_dry_run ?
              <p className="mt-2 text-[11px] text-amber-700 dark:text-amber-400">
                ● dry-run if no connection for{" "}
                <code className="rounded bg-amber-100 px-1 dark:bg-amber-950">
                  {a.provider}
                </code>
              </p>
            : null}
          </div>
        </div>
      </article>
    );
  }
  if (node.kind === "if") {
    const i = node as IfNode;
    return (
      <article className="rounded-2xl border-2 border-cyan-500/50 bg-cyan-50/60 p-4 dark:border-cyan-400/50 dark:bg-cyan-950/40">
        <div className="flex items-center gap-2 text-cyan-700 dark:text-cyan-300">
          <GitMerge className="h-4 w-4 rotate-90" />
          <span className="text-[10px] font-semibold uppercase tracking-wide">
            If
          </span>
        </div>
        <h3 className="mt-1 text-base font-semibold text-slate-900 dark:text-slate-100">
          {i.name}
        </h3>
        <p className="mt-1 font-mono text-xs text-slate-600 dark:text-slate-400">
          {i.expression}
        </p>
      </article>
    );
  }
  if (node.kind === "memory") {
    const m = node as MemoryNode;
    return (
      <article className="rounded-2xl border-2 border-violet-400/60 bg-violet-50/60 p-4 dark:border-violet-400/50 dark:bg-violet-950/40">
        <div className="flex items-center gap-2 text-violet-700 dark:text-violet-300">
          <Brain className="h-4 w-4" />
          <span className="text-[10px] font-semibold uppercase tracking-wide">
            Memory
          </span>
        </div>
        <h3 className="mt-1 text-base font-semibold text-slate-900 dark:text-slate-100">
          {m.name}
        </h3>
        <p className="mt-1 font-mono text-xs text-slate-600 dark:text-slate-400">
          {m.scope} · {m.store} · {m.max_turns} turns · ttl {Math.round(m.ttl_seconds / 60)}m
        </p>
      </article>
    );
  }
  if (node.kind === "output_parser") {
    const p = node as OutputParserNode;
    return (
      <article className="rounded-2xl border-2 border-amber-400/60 bg-amber-50/60 p-4 dark:border-amber-400/50 dark:bg-amber-950/40">
        <div className="flex items-center gap-2 text-amber-700 dark:text-amber-300">
          <Braces className="h-4 w-4" />
          <span className="text-[10px] font-semibold uppercase tracking-wide">
            Output Parser
          </span>
        </div>
        <h3 className="mt-1 text-base font-semibold text-slate-900 dark:text-slate-100">
          {p.name}
        </h3>
        <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">
          retries: {p.max_retries} · fields: {Object.keys(p.json_schema ?? {}).length}
        </p>
      </article>
    );
  }
  if (node.kind === "data_store") {
    const d = node as DataStoreNode;
    return (
      <article className="rounded-2xl border-2 border-slate-300 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-900">
        <div className="flex items-center gap-2 text-slate-700 dark:text-slate-300">
          <Table2 className="h-4 w-4" />
          <span className="text-[10px] font-semibold uppercase tracking-wide">
            Data · {d.op}
          </span>
        </div>
        <h3 className="mt-1 text-base font-semibold text-slate-900 dark:text-slate-100">
          {d.name}
        </h3>
        <p className="mt-1 font-mono text-xs text-slate-500 dark:text-slate-400">
          table: {d.table}
          {d.key ? ` · key: ${d.key}` : ""}
        </p>
      </article>
    );
  }
  // merge
  return (
    <article className="rounded-2xl border-2 border-slate-400 bg-slate-50 p-3 dark:border-slate-500 dark:bg-slate-900">
      <div className="flex items-center gap-2 text-slate-600 dark:text-slate-300">
        <Sigma className="h-4 w-4" />
        <span className="text-[10px] font-semibold uppercase tracking-wide">
          Merge
        </span>
      </div>
      <h3 className="mt-1 text-base font-semibold text-slate-900 dark:text-slate-100">
        {node.name}
      </h3>
      <p className="mt-1 text-xs text-slate-500">
        Joins: {node.depends_on.join(", ") || "(no upstreams)"}
      </p>
    </article>
  );
}

/** Buckets sequential tiers with more than one agent as “parallel-ish” tiers. */

function bucketParallel(agents: WorkflowDefinition["agents"]): string[][] {
  const byId = new Map(agents.map((a) => [a.id, a]));
  function depth(id: string, seen = new Set<string>()): number {
    if (seen.has(id)) return 0;
    seen.add(id);
    const ag = byId.get(id);
    if (!ag || ag.depends_on.length === 0) return 0;
    return 1 + Math.max(...ag.depends_on.map((d) => depth(d, new Set(seen))));
  }

  const levels = new Map<number, string[]>();
  agents.forEach((ag) => {
    const lv = depth(ag.id);
    if (!levels.has(lv)) levels.set(lv, []);
    levels.get(lv)!.push(ag.name);
  });
  const list: string[][] = [];
  [...levels.entries()]
    .sort((a, b) => a[0] - b[0])
    .forEach(([, ids]) => {
      if (ids.length >= 2) list.push(ids);
    });
  return list;
}

function SatelliteSummary({ node }: { node: WorkflowNode }) {
  // Compact representation of a satellite node — rendered below its parent
  // agent in the preview pane so the editing surface mirrors the canvas.
  if (node.kind === "memory") {
    const m = node as MemoryNode;
    return (
      <article className="rounded-xl border border-violet-300 bg-violet-50/70 p-2 text-xs dark:border-violet-800 dark:bg-violet-950/50">
        <div className="flex items-center gap-1 text-violet-700 dark:text-violet-300">
          <Brain className="h-3 w-3" />
          <span className="text-[10px] font-semibold uppercase tracking-wide">Memory</span>
        </div>
        <p className="mt-1 truncate font-semibold text-slate-900 dark:text-slate-100">{m.name}</p>
        <p className="truncate font-mono text-[10px] text-slate-500 dark:text-slate-400">{m.scope}</p>
      </article>
    );
  }
  if (node.kind === "output_parser") {
    const p = node as OutputParserNode;
    return (
      <article className="rounded-xl border border-amber-300 bg-amber-50/70 p-2 text-xs dark:border-amber-800 dark:bg-amber-950/50">
        <div className="flex items-center gap-1 text-amber-700 dark:text-amber-300">
          <Braces className="h-3 w-3" />
          <span className="text-[10px] font-semibold uppercase tracking-wide">Parser</span>
        </div>
        <p className="mt-1 truncate font-semibold text-slate-900 dark:text-slate-100">{p.name}</p>
      </article>
    );
  }
  if (node.kind === "action") {
    const a = node as ActionNode;
    const tag = resolveProviderForSlug(a.action_slug);
    return (
      <article className="rounded-xl border border-slate-300 bg-white p-2 text-xs dark:border-slate-700 dark:bg-slate-900">
        <div className="flex items-center gap-1">
          <div className={cn("flex h-5 w-5 items-center justify-center rounded", tag ? tag.bg : "bg-slate-100 dark:bg-slate-800", tag ? tag.fg : "text-slate-500")}>
            {tag ? tag.icon : <Sparkles className="h-3 w-3" />}
          </div>
          <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">Tool</span>
        </div>
        <p className="mt-1 truncate font-semibold text-slate-900 dark:text-slate-100">{a.name}</p>
        <p className="truncate font-mono text-[10px] text-slate-500 dark:text-slate-400">{a.action_slug}</p>
      </article>
    );
  }
  if (node.kind === "data_store") {
    const d = node as DataStoreNode;
    return (
      <article className="rounded-xl border border-slate-300 bg-slate-50 p-2 text-xs dark:border-slate-700 dark:bg-slate-900">
        <div className="flex items-center gap-1 text-slate-700 dark:text-slate-300">
          <Table2 className="h-3 w-3" />
          <span className="text-[10px] font-semibold uppercase tracking-wide">Tool · data</span>
        </div>
        <p className="mt-1 truncate font-semibold text-slate-900 dark:text-slate-100">{d.name}</p>
        <p className="truncate font-mono text-[10px] text-slate-500 dark:text-slate-400">{d.op}: {d.table}</p>
      </article>
    );
  }
  return null;
}

