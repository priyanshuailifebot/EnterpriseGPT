"use client";

import { Fragment, useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

interface RankRow {
  rank: number;
  candidate_id: string;
  name: string;
  role_title?: string | null;
  score: number | null;
  status?: string | null;
  current_round_name?: string | null;
  assessment?: string | null;
}

const STATUS_STYLES: Record<string, string> = {
  offer_extended: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
  advanced: "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300",
  scored: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200",
  not_advanced: "bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300",
};

function ScoreBar({ score }: { score: number | null }) {
  if (score === null) {
    return <span className="text-xs text-slate-400">—</span>;
  }
  const pct = Math.max(0, Math.min(100, score));
  const hue = Math.round((pct / 100) * 120); // red→green
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 w-24 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
        <div
          className="h-full rounded-full"
          style={{ width: `${pct}%`, backgroundColor: `hsl(${hue} 70% 45%)` }}
        />
      </div>
      <span className="w-8 text-sm font-semibold tabular-nums text-slate-800 dark:text-slate-100">
        {Math.round(score)}
      </span>
    </div>
  );
}

export default function LeaderboardPage() {
  const ws = useAuthStore((s) => s.workspaceId);
  const [rows, setRows] = useState<RankRow[]>([]);
  const [status, setStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [error, setError] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!ws) return;
    setStatus("loading");
    setError("");
    try {
      const { data } = await api.get<{ ranking: RankRow[] }>(
        "/api/v1/workflows/hr/leaderboard",
        { params: { workspace_id: ws } },
      );
      setRows(data.ranking ?? []);
      setStatus("done");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load leaderboard");
      setStatus("error");
    }
  }, [ws]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-50">
            Candidate Leaderboard
          </h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            Interviewed candidates ranked by overall score. Updates live as rounds
            are scored.
          </p>
        </div>
        <button
          onClick={() => void load()}
          className="rounded-xl border border-slate-200 px-4 py-2 text-sm font-medium hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
        >
          Refresh
        </button>
      </div>

      {!ws ? (
        <p className="mt-8 text-sm text-slate-500">Select a workspace to view the leaderboard.</p>
      ) : status === "loading" ? (
        <p className="mt-8 text-sm text-slate-500">Loading…</p>
      ) : status === "error" ? (
        <p className="mt-8 text-sm text-rose-600 dark:text-rose-400">{error}</p>
      ) : rows.length === 0 ? (
        <p className="mt-8 text-sm text-slate-500">
          No scored candidates yet. Run HR Sourcing and let candidates complete a round.
        </p>
      ) : (
        <div className="mt-6 overflow-x-auto rounded-2xl border border-slate-200 dark:border-slate-800">
          <table className="w-full min-w-[640px] text-left text-sm">
            <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500 dark:bg-slate-900 dark:text-slate-400">
              <tr>
                <th className="px-4 py-3 w-12">#</th>
                <th className="px-4 py-3">Candidate</th>
                <th className="px-4 py-3">Score</th>
                <th className="px-4 py-3">Latest round</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3 w-20"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {rows.map((r) => (
                <Fragment key={r.candidate_id}>
                  <tr className="bg-white dark:bg-slate-950">
                    <td className="px-4 py-3 font-semibold text-slate-400">{r.rank}</td>
                    <td className="px-4 py-3">
                      <div className="font-medium text-slate-900 dark:text-slate-100">{r.name}</div>
                      {r.role_title ? (
                        <div className="text-xs text-slate-500">{r.role_title}</div>
                      ) : null}
                    </td>
                    <td className="px-4 py-3">
                      <ScoreBar score={r.score} />
                    </td>
                    <td className="px-4 py-3 text-slate-600 dark:text-slate-300">
                      {r.current_round_name ?? "—"}
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
                          STATUS_STYLES[r.status ?? ""] ??
                          "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                        }`}
                      >
                        {(r.status ?? "—").replace(/_/g, " ")}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {r.assessment ? (
                        <button
                          onClick={() =>
                            setOpenId(openId === r.candidate_id ? null : r.candidate_id)
                          }
                          className="text-xs font-medium text-brand-600 hover:underline"
                        >
                          {openId === r.candidate_id ? "Hide" : "Assessment"}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                  {openId === r.candidate_id && r.assessment ? (
                    <tr className="bg-slate-50 dark:bg-slate-900">
                      <td />
                      <td colSpan={5} className="px-4 py-3 text-sm text-slate-700 dark:text-slate-300">
                        <div dangerouslySetInnerHTML={{ __html: r.assessment }} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
