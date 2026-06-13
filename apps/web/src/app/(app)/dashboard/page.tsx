"use client";

import { formatDistanceToNow } from "date-fns";
import { MessageSquare, Plus, ScrollText, Upload, Workflow } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { api, getErrorMessage } from "@/lib/api";
import { WorkflowStatusBadge } from "@/components/workflow/WorkflowStatusBadge";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/authStore";
import type {
  DocumentListResponse,
  IntegrationResponse,
  WorkflowExecutionsEnvelope,
  WorkflowListOut,
  WorkflowSummaryOut,
} from "@/types/api";

interface RecentRow {
  workflowName: string;
  workflowId: string;
  executionId: string;
  status: string;
  started: string | null;
}

export default function DashboardPage() {
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const hasPerm = useAuthStore((s) => s.hasPermission);
  const [stats, setStats] = useState<{
    workflows: number;
    published: number;
    draft: number;
    docsIndexed: number;
    integrations: number;
    executionsToday: number;
  } | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowSummaryOut[]>([]);
  const [recent, setRecent] = useState<RecentRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!workspaceId) return;

    async function load() {
      try {
        setError(null);

        const wfResp = await api.get<WorkflowListOut>("/api/v1/workflows/", {
          params: { workspace_id: workspaceId, page_size: 50 },
        });
        const workflows = wfResp.data.items;
        setWorkflows(workflows);

        const published = workflows.filter((w) => w.status === "published").length;
        const draft = workflows.filter((w) => w.status === "draft").length;

        let docsIndexed = 0;
        try {
          const docResp = await api.get<DocumentListResponse>(
            "/api/v1/documents",
            {
              params: {
                workspace_id: workspaceId,
                page_size: 1,
                status: "indexed",
              },
            },
          );
          docsIndexed = docResp.data.total;
        } catch {
          /* optional */
        }

        let integrations = 0;
        try {
          const intResp = await api.get<IntegrationResponse[]>(
            "/api/v1/integrations",
            {
              params: { workspace_id: workspaceId },
            },
          );
          integrations = intResp.data.filter((i) => i.status === "connected")
            .length;
        } catch {
          /* optional */
        }

        const execSamples: RecentRow[] = [];
        const executionsInWindowIds = new Set<string>();
        const since = Date.now() - 24 * 60 * 60 * 1000;
        await Promise.all(
          workflows.slice(0, 8).map(async (w: WorkflowSummaryOut) => {
            try {
              const ex = await api.get<WorkflowExecutionsEnvelope>(
                `/api/v1/workflows/${w.id}/executions`,
                { params: { page_size: 10 } },
              );
              ex.data.items.forEach((row) => {
                if (
                  row.started_at &&
                  Date.parse(row.started_at) >= since
                ) {
                  executionsInWindowIds.add(row.id);
                }
                execSamples.push({
                  workflowName: w.name,
                  workflowId: w.id,
                  executionId: row.id,
                  status: row.status,
                  started: row.started_at,
                });
              });
            } catch {
              /* ignore */
            }
          }),
        );

        execSamples.sort(
          (a, b) =>
            (b.started ? Date.parse(b.started) : 0) -
            (a.started ? Date.parse(a.started) : 0),
        );

        setStats({
          workflows: workflows.length,
          published,
          draft,
          docsIndexed,
          integrations,
          executionsToday: executionsInWindowIds.size,
        });
        setRecent(execSamples.slice(0, 6));
      } catch (e) {
        setError(getErrorMessage(e));
      }
    }

    void load();
  }, [workspaceId]);

  const canUpload = hasPerm("document:upload");
  const hasChatPerm = hasPerm("workflow:read");

  return (
    <div className="mx-auto max-w-6xl space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-50">
            Dashboard
          </h1>
          <p className="text-sm text-slate-600 dark:text-slate-400">
            Overview of workspaces, executions, documents, and automations.
          </p>
        </div>
        <Link
          href="/workflows/new"
          className="inline-flex items-center gap-2 rounded-xl bg-brand-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-brand-700"
        >
          <Plus className="h-4 w-4" /> New Workflow
        </Link>
      </div>

      {error ? (
        <div className="rounded-xl border border-error/40 bg-red-50 px-4 py-3 text-sm text-red-900 dark:bg-red-950/40 dark:text-red-50">
          {error}
        </div>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          title="Workflows"
          value={stats?.workflows ?? "—"}
          subtitle={
            stats && stats.workflows > 0
              ? `${stats.published} published · ${stats.draft} draft`
              : undefined
          }
        />
        <MetricCard
          title="Executions (24h)"
          value={stats?.executionsToday ?? "—"}
        />
        <MetricCard
          title="Documents indexed"
          value={stats?.docsIndexed ?? "—"}
        />
        <MetricCard
          title="Active integrations"
          value={stats?.integrations ?? "—"}
        />
      </div>

      <section className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <div className="rounded-2xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
                Your workflows
              </h2>
              <Link
                href="/workflows"
                className="text-xs font-medium text-brand-700 hover:underline dark:text-brand-300"
              >
                View all
              </Link>
            </div>
            {!workspaceId ? (
              <EmptyState>Select a workspace.</EmptyState>
            ) : workflows.length === 0 ? (
              <EmptyState>No workflows yet.</EmptyState>
            ) : (
              <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                {workflows.slice(0, 5).map((wf) => (
                  <li
                    key={wf.id}
                    className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm first:pt-0 last:pb-0"
                  >
                    <div className="min-w-0">
                      <Link
                        href={`/workflows/${wf.id}`}
                        className="font-medium text-brand-700 hover:underline dark:text-brand-300"
                      >
                        {wf.name}
                      </Link>
                      <p className="text-xs text-slate-500">
                        v{wf.current_version}
                        {wf.published_at
                          ? ` · published ${formatDistanceToNow(new Date(wf.published_at), { addSuffix: true })}`
                          : ""}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <WorkflowStatusBadge status={wf.status} />
                      <Link
                        href={`/workflows/${wf.id}/run`}
                        className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
                      >
                        Run
                      </Link>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="rounded-2xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
            <h2 className="mb-4 text-lg font-semibold text-slate-900 dark:text-slate-50">
              Recent executions
            </h2>
            {!workspaceId ? (
              <EmptyState>Select a workspace.</EmptyState>
            ) : recent.length === 0 ? (
              <EmptyState>No recent runs.</EmptyState>
            ) : (
              <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                {recent.map((r) => (
                  <li
                    key={`${r.workflowId}-${r.executionId}`}
                    className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm first:pt-0 last:pb-0"
                  >
                    <div>
                      <Link
                        href={`/workflows/${r.workflowId}/run`}
                        className="font-medium text-brand-700 hover:underline dark:text-brand-300"
                      >
                        {r.workflowName}
                      </Link>
                      <p className="text-xs text-slate-500">
                        {r.started ?
                          formatDistanceToNow(new Date(r.started), {
                            addSuffix: true,
                          })
                        : "—"}
                      </p>
                    </div>
                    <StatusBadge status={r.status} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="space-y-3 rounded-2xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
          <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
            Quick actions
          </h2>
          <div className="flex flex-col gap-2">
            <ActionButton href="/workflows/new">
              <Plus className="h-4 w-4" /> New Workflow
            </ActionButton>
            {hasChatPerm ? (
              <ActionButton href="/chat">
                <MessageSquare className="h-4 w-4" /> New Chat
              </ActionButton>
            ) : null}
            {canUpload ? (
              <ActionButton href="/documents">
                <Upload className="h-4 w-4" /> Upload Documents
              </ActionButton>
            ) : (
              <ActionButton href="/documents">
                <ScrollText className="h-4 w-4" /> Browse Documents
              </ActionButton>
            )}
          </div>
          <Link
            href="/workflows"
            className="inline-flex items-center gap-2 text-sm font-medium text-brand-700 hover:underline dark:text-brand-300"
          >
            <Workflow className="h-4 w-4" /> Open workflow library
          </Link>
        </div>
      </section>
    </div>
  );
}

function MetricCard({
  title,
  value,
  subtitle,
}: {
  title: string;
  value: string | number;
  subtitle?: string;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
      <p className="text-xs uppercase tracking-wide text-slate-500">{title}</p>
      <p className="mt-2 text-3xl font-semibold text-slate-900 dark:text-slate-50">
        {value}
      </p>
      {subtitle ? (
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{subtitle}</p>
      ) : null}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const s = status.toLowerCase();
  const cls =
    s.includes("running") ?
      "bg-blue-50 text-blue-900 dark:bg-blue-950 dark:text-blue-100"
    : s.includes("complete") || s === "indexed" ?
      "bg-emerald-50 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100"
    : s.includes("fail") || s.includes("cancel") ?
      "bg-red-50 text-red-900 dark:bg-red-950 dark:text-red-50"
    : "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-100";
  return (
    <span
      className={cn(
        "rounded-full px-2.5 py-0.5 text-xs font-medium capitalize",
        cls,
      )}
    >
      {status}
    </span>
  );
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-200 px-4 py-6 text-center text-sm text-slate-500 dark:border-slate-700">
      {children}
    </div>
  );
}

function ActionButton({
  href,
  children,
}: {
  href: string;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      className="flex items-center gap-2 rounded-xl border border-slate-200 px-3 py-2 text-sm font-medium hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
    >
      {children}
    </Link>
  );
}
