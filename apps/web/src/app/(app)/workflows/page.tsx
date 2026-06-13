"use client";

import { Plus } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { WorkflowStatusBadge } from "@/components/workflow/WorkflowStatusBadge";
import { useAuthStore } from "@/stores/authStore";
import { useWorkflowStore } from "@/stores/workflowStore";
import type { WorkflowStatus, WorkflowSummaryOut } from "@/types/api";

interface WorkflowCardProps {
  wf: WorkflowSummaryOut;
}

function WorkflowCardInner({ wf }: WorkflowCardProps) {
  const [meta, setMeta] = useState<{
    lastRun?: string | null;
    runs: number;
  }>();

  useEffect(() => {
    async function stats() {
      try {
        const { data } = await api.get<{
          items: { started_at?: string | null }[];
          total: number;
        }>(`/api/v1/workflows/${wf.id}/executions`, {
          params: { page_size: 1 },
        });
        const lastRun = data.items[0]?.started_at ?? null;
        setMeta({ lastRun, runs: data.total });
      } catch {
        setMeta(undefined);
      }
    }
    void stats();
  }, [wf.id]);

  return (
    <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-950">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Link
              href={`/workflows/${wf.id}`}
              className="text-lg font-semibold text-slate-900 hover:underline dark:text-slate-100"
            >
              {wf.name}
            </Link>
            <WorkflowStatusBadge status={wf.status} />
          </div>
          <div className="mt-3 flex flex-wrap gap-4 text-xs text-slate-500">
            <span>
              Runs tracked:&nbsp;<strong>{meta?.runs ?? "—"}</strong>
            </span>
            <span>
              Last run:&nbsp;
              <strong>
                {meta?.lastRun ? new Date(meta.lastRun).toLocaleString() : "—"}
              </strong>
            </span>
          </div>
        </div>
        {!wf.is_active ? (
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-600 dark:bg-slate-800 dark:text-slate-200">
            inactive
          </span>
        ) : null}
      </div>
      <div className="mt-6 flex flex-wrap gap-3">
        <Link
          href={`/workflows/${wf.id}/run`}
          className="rounded-xl bg-brand-600 px-4 py-2 text-sm font-semibold text-white"
        >
          Run
        </Link>
        <Link
          href={`/workflows/${wf.id}`}
          className="rounded-xl border border-slate-200 px-4 py-2 text-sm dark:border-slate-700"
        >
          Detail
        </Link>
      </div>
    </div>
  );
}

export default function WorkflowListPage() {
  const ws = useAuthStore((s) => s.workspaceId);
  const workflows = useWorkflowStore((s) => s.workflows);
  const fetchWorkflows = useWorkflowStore((s) => s.fetchWorkflows);
  const [filter, setFilter] = useState<"all" | WorkflowStatus>("all");
  const [q, setQ] = useState("");

  useEffect(() => {
    if (ws) void fetchWorkflows(ws);
  }, [fetchWorkflows, ws]);

  const filtered = workflows.filter((w) => {
    if (filter !== "all" && w.status !== filter) return false;
    const haystack = `${w.name} ${w.slug}`.toLowerCase();
    return haystack.includes(q.trim().toLowerCase());
  });

  const statusCounts = workflows.reduce(
    (acc, w) => {
      acc[w.status] = (acc[w.status] ?? 0) + 1;
      return acc;
    },
    {} as Partial<Record<WorkflowStatus, number>>,
  );

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-50">
            Workflows
          </h1>
          <p className="text-sm text-slate-600 dark:text-slate-400">
            Design and run Dynamiq-backed WorkFlow™ automations.
          </p>
        </div>
        {ws ?
          <Link
            href="/workflows/new"
            className="inline-flex items-center gap-2 rounded-xl bg-brand-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-brand-700"
          >
            <Plus className="h-4 w-4" /> New workflow
          </Link>
        : null}
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <input
          value={q}
          placeholder="Search by name…"
          onChange={(e) => setQ(e.target.value)}
          className="w-full rounded-xl border px-4 py-2 text-sm dark:border-slate-700 dark:bg-slate-950 md:max-w-sm"
        />
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value as typeof filter)}
          className="rounded-xl border px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
        >
          <option value="all">All statuses</option>
          <option value="draft">
            Draft{statusCounts.draft ? ` (${statusCounts.draft})` : ""}
          </option>
          <option value="published">
            Published{statusCounts.published ? ` (${statusCounts.published})` : ""}
          </option>
          <option value="archived">
            Archived{statusCounts.archived ? ` (${statusCounts.archived})` : ""}
          </option>
        </select>
      </div>

      {!ws ? (
        <p>Select a workspace to list workflows.</p>
      ) : filtered.length === 0 ? (
        <div className="rounded-3xl border border-dashed border-slate-300 px-10 py-12 text-center text-sm text-slate-600 dark:border-slate-700">
          <p>Create your first workflow from natural language.</p>
          <Link
            href="/workflows/new"
            className="mt-4 inline-flex items-center gap-2 rounded-xl bg-brand-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-brand-700"
          >
            <Plus className="h-4 w-4" /> Launch builder
          </Link>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {filtered.map((wf) => (
            <WorkflowCardInner key={wf.id} wf={wf} />
          ))}
        </div>
      )}
    </div>
  );
}
