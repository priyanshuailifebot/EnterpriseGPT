"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";

// Public, unauthenticated interviewer-facing page for HUMAN interview rounds.
// The signed `ctx` from the brief email carries the candidate, round, and
// workspace; this page collects the interviewer's rating + notes and POSTs to
// the scoring slug trigger (the same gate AI rounds use), which verifies the
// ctx and drafts the recruiter assessment + Approve/Reject email.
const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Status = "idle" | "submitting" | "done" | "error";

function FeedbackForm() {
  const ctx = useSearchParams().get("ctx");
  const [rating, setRating] = useState(70);
  const [notes, setNotes] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState("");

  if (!ctx) {
    return (
      <p className="text-sm text-rose-600 dark:text-rose-400">
        This link is invalid or has expired. Please use the link from your email.
      </p>
    );
  }

  if (status === "done") {
    return (
      <div className="text-center">
        <div className="text-4xl">✅</div>
        <h2 className="mt-3 text-lg font-semibold text-slate-800 dark:text-slate-100">
          Feedback submitted
        </h2>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          Thanks — the recruiter has been notified with your assessment. You can
          close this page.
        </p>
      </div>
    );
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!notes.trim()) return;
    setStatus("submitting");
    setError("");
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/workflows/slug/hr-scoring?ctx=${encodeURIComponent(ctx!)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            source: "human_feedback",
            rating,
            feedback: notes.trim(),
          }),
        },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(
          body?.detail || body?.error || `Request failed (${res.status})`,
        );
      }
      setStatus("done");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
      setStatus("error");
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <div>
        <label className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-200">
          Overall rating: <span className="font-semibold">{rating}</span> / 100
        </label>
        <input
          type="range"
          min={0}
          max={100}
          step={1}
          value={rating}
          onChange={(e) => setRating(Number(e.target.value))}
          className="w-full"
        />
      </div>
      <div>
        <label className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-200">
          Interview notes &amp; assessment
        </label>
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          required
          rows={7}
          placeholder="Strengths, gaps, and your recommendation for this round…"
          className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
        />
      </div>
      {error ? (
        <p className="text-sm text-rose-600 dark:text-rose-400">{error}</p>
      ) : null}
      <button
        type="submit"
        disabled={status === "submitting" || !notes.trim()}
        className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
      >
        {status === "submitting" ? "Submitting…" : "Submit feedback"}
      </button>
      <p className="text-xs text-slate-500 dark:text-slate-400">
        Your notes are scored by the assistant into a recruiter-ready assessment;
        the recruiter still approves or rejects.
      </p>
    </form>
  );
}

function RoundHeading() {
  const roundName = useSearchParams().get("round_name");
  return (
    <p className="mt-1 mb-5 text-sm text-slate-600 dark:text-slate-400">
      {roundName
        ? `Submit your assessment for the “${roundName}” round.`
        : "Submit your interview assessment for this round."}
    </p>
  );
}

export default function FeedbackPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-4 py-10">
      <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
          Interviewer feedback
        </h1>
        <Suspense fallback={<p className="text-sm text-slate-500">Loading…</p>}>
          <RoundHeading />
          <FeedbackForm />
        </Suspense>
      </div>
    </main>
  );
}
