"use client";

import { subDays } from "date-fns";
import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { DateRangePicker } from "@/app/(app)/analytics/date-range-picker";
import { RAGAnalytics } from "@/app/(app)/analytics/rag-analytics";
import { ToolUsage } from "@/app/(app)/analytics/tool-usage";
import {
  fetchAnalyticsCosts,
  fetchAnalyticsOverview,
  fetchAnalyticsRag,
  fetchAnalyticsTools,
  getErrorMessage,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/authStore";
import type { CostStats, OverviewStats, RagAnalytics, ToolUsageStat } from "@/types/api";

function MetricCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-2xl border border-slate-200 bg-white p-4 shadow-sm",
        "dark:border-slate-800 dark:bg-slate-950",
      )}
    >
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </p>
      <p className="mt-2 text-2xl font-semibold tabular-nums text-slate-900 dark:text-slate-50">
        {value}
      </p>
      {hint ? (
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{hint}</p>
      ) : null}
    </div>
  );
}

export default function AnalyticsPage() {
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const [start, setStart] = useState(() => subDays(new Date(), 30));
  const [end, setEnd] = useState(() => new Date());
  const [overview, setOverview] = useState<OverviewStats | null>(null);
  const [rag, setRag] = useState<RagAnalytics | null>(null);
  const [tools, setTools] = useState<ToolUsageStat[] | null>(null);
  const [costs, setCosts] = useState<CostStats | null>(null);
  const [error, setError] = useState<string | null>(null);

  const range = useMemo(() => ({ start, end }), [start, end]);

  useEffect(() => {
    if (!workspaceId) return;
    const ws = workspaceId;

    let cancelled = false;
    async function load() {
      setError(null);
      try {
        const [o, r, t, c] = await Promise.all([
          fetchAnalyticsOverview(ws, range.start, range.end),
          fetchAnalyticsRag(ws, range.start, range.end),
          fetchAnalyticsTools(ws),
          fetchAnalyticsCosts(ws, range.start, range.end),
        ]);
        if (!cancelled) {
          setOverview(o);
          setRag(r);
          setTools(t);
          setCosts(c);
        }
      } catch (e) {
        if (!cancelled) setError(getErrorMessage(e));
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, range.start, range.end]);

  if (!workspaceId) {
    return (
      <p className="text-sm text-slate-600 dark:text-slate-400">Select a workspace to view analytics.</p>
    );
  }

  return (
    <div className="space-y-8">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-50">Analytics</h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            Workflow execution, RAG quality, tool usage, and rough LLM cost estimates.
          </p>
        </div>
        <DateRangePicker
          start={start}
          end={end}
          onChange={(s, e) => {
            setStart(s);
            setEnd(e);
          }}
        />
      </div>

      {error ? (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
          {error}
        </div>
      ) : null}

      <section>
        <h2 className="mb-3 text-sm font-medium text-slate-700 dark:text-slate-300">Overview</h2>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <MetricCard
            label="Total executions"
            value={overview ? String(overview.total_executions) : "—"}
          />
          <MetricCard
            label="Success rate"
            value={
              overview && overview.total_executions > 0
                ? `${((overview.successful_executions / overview.total_executions) * 100).toFixed(1)}%`
                : "—"
            }
            hint={
              overview
                ? `${overview.successful_executions} ok / ${overview.failed_executions} failed`
                : undefined
            }
          />
          <MetricCard
            label="Avg duration"
            value={
              overview?.avg_duration_ms != null
                ? `${Math.round(overview.avg_duration_ms)} ms`
                : "—"
            }
          />
          <MetricCard
            label="Tokens (logged)"
            value={overview ? String(overview.total_tokens_used) : "—"}
            hint="From persisted execution payloads when available"
          />
        </div>

        <div className="mt-4 rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-950">
          <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
            Executions over time
          </h3>
          <div className="mt-4 h-64 w-full">
            {overview && overview.executions_by_day.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={overview.executions_by_day}>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-800" />
                  <XAxis dataKey="date" tick={{ fontSize: 10 }} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 10 }} />
                  <Tooltip />
                  <Line type="monotone" dataKey="count" stroke="#4f46e5" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <p className="flex h-full items-center justify-center text-sm text-slate-500">
                No executions in this date range.
              </p>
            )}
          </div>
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-medium text-slate-700 dark:text-slate-300">RAG</h2>
        <RAGAnalytics data={rag} />
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-medium text-slate-700 dark:text-slate-300">Tools</h2>
        <ToolUsage tools={tools} />
      </section>

      <section>
        <h2 className="text-sm font-medium text-slate-700 dark:text-slate-300">Estimated costs</h2>
        <div className="mt-3 rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-950">
          {costs ? (
            <>
              <p className="text-lg font-semibold tabular-nums text-slate-900 dark:text-slate-50">
                ${costs.total_estimated_usd.toFixed(4)}{" "}
                <span className="text-sm font-normal text-slate-500">USD (approx.)</span>
              </p>
              {costs.by_model.length > 0 ? (
                <ul className="mt-3 divide-y divide-slate-100 text-sm dark:divide-slate-800">
                  {costs.by_model.map((m) => (
                    <li key={m.model} className="flex justify-between gap-4 py-2">
                      <span className="font-mono text-xs text-slate-700 dark:text-slate-300">
                        {m.model}
                      </span>
                      <span className="tabular-nums text-slate-600 dark:text-slate-400">
                        in {m.input_tokens.toLocaleString()} / out {m.output_tokens.toLocaleString()} · $
                        {m.estimated_cost_usd.toFixed(4)}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-sm text-slate-500">
                  No token usage found in execution payloads for this window. Costs appear when runs persist
                  `usage` in `output_data`.
                </p>
              )}
            </>
          ) : (
            <p className="text-sm text-slate-500">Loading cost estimate…</p>
          )}
        </div>
      </section>
    </div>
  );
}
