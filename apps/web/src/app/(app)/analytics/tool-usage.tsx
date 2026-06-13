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

import type { ToolUsageStat } from "@/types/api";

export function ToolUsage({ tools }: { tools: ToolUsageStat[] | null }) {
  if (!tools) {
    return (
      <div className="rounded-2xl border border-slate-200 bg-white p-6 text-sm text-slate-500 dark:border-slate-800 dark:bg-slate-950">
        Loading tool usage…
      </div>
    );
  }

  if (tools.length === 0) {
    return (
      <div className="rounded-2xl border border-dashed border-slate-200 bg-white p-6 text-sm text-slate-500 dark:border-slate-800 dark:bg-slate-950">
        No tool executions recorded for this workspace yet.
      </div>
    );
  }

  const chartData = [...tools]
    .sort((a, b) => b.call_count - a.call_count)
    .slice(0, 15)
    .map((t) => ({
      name:
        t.tool_name.length > 28
          ? `${t.tool_name.slice(0, 26)}…`
          : t.tool_name,
      fullName: t.tool_name,
      calls: t.call_count,
    }));

  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-950">
      <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">Tool calls</h3>
      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
        MCP / Composio tool invocations tied to workflow runs (top 15 by volume).
      </p>
      <div className="mt-4 h-72 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={chartData} layout="vertical" margin={{ left: 8, right: 16 }}>
            <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-800" />
            <XAxis type="number" allowDecimals={false} tick={{ fontSize: 10 }} />
            <YAxis
              type="category"
              dataKey="name"
              width={120}
              tick={{ fontSize: 10 }}
            />
            <Tooltip
              formatter={(value: number, _n, item) => [value, "Calls"]}
              labelFormatter={(_, payload) =>
                payload?.[0]?.payload?.fullName as string | undefined
              }
            />
            <Bar dataKey="calls" fill="#0d9488" radius={[0, 4, 4, 0]} name="Calls" />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
