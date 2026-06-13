"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { RagAnalytics } from "@/types/api";

export function RAGAnalytics({ data }: { data: RagAnalytics | null }) {
  if (!data) {
    return (
      <div className="rounded-2xl border border-slate-200 bg-white p-6 text-sm text-slate-500 dark:border-slate-800 dark:bg-slate-950">
        Loading RAG analytics…
      </div>
    );
  }

  const unanswerableRate =
    data.total_queries > 0 ? data.unanswerable_count / data.total_queries : 0;

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-950">
        <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
          Confidence distribution
        </h3>
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
          Bucketed retrieval scores for document Q&amp;A (10 deciles).
        </p>
        <div className="mt-4 h-56 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data.confidence_buckets}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-800" />
              <XAxis dataKey="label" tick={{ fontSize: 10 }} />
              <YAxis allowDecimals={false} tick={{ fontSize: 10 }} />
              <Tooltip />
              <Bar dataKey="count" fill="#4f46e5" radius={[4, 4, 0, 0]} name="Queries" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div className="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-950">
        <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
          Unanswerable rate
        </h3>
        <p className="mt-3 text-3xl font-semibold tabular-nums text-slate-900 dark:text-slate-100">
          {(unanswerableRate * 100).toFixed(1)}%
        </p>
        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
          {data.unanswerable_count} of {data.total_queries} queries flagged as unanswerable in this range.
        </p>
        {data.top_documents.length > 0 ? (
          <div className="mt-4 border-t border-slate-100 pt-4 dark:border-slate-800">
            <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Top documents</p>
            <ul className="mt-2 space-y-1 text-sm">
              {data.top_documents.slice(0, 5).map((d) => (
                <li key={d.document_id} className="flex justify-between gap-2">
                  <span className="truncate text-slate-700 dark:text-slate-300">{d.title}</span>
                  <span className="shrink-0 tabular-nums text-slate-500">{d.query_count}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </div>
  );
}
