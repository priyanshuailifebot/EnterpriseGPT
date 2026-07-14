"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";

// Public, unauthenticated candidate-facing page. The signed `ctx` from the
// invite email carries the candidate + workspace; this page collects the slot
// and language and POSTs to the id-free slug trigger, which verifies the ctx.
const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const LANGUAGES: { value: string; label: string }[] = [
  { value: "en-IN", label: "English" },
  { value: "hi-IN", label: "Hindi" },
  { value: "ta-IN", label: "Tamil" },
  { value: "te-IN", label: "Telugu" },
  { value: "mr-IN", label: "Marathi" },
];

type Status = "idle" | "submitting" | "done" | "error";

function SlotForm() {
  const ctx = useSearchParams().get("ctx");
  const [slot, setSlot] = useState("");
  const [language, setLanguage] = useState("en-IN");
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
          Your interview slot is booked
        </h2>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          You&apos;ll receive a call at your selected time. You can close this page.
        </p>
      </div>
    );
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!slot) return;
    setStatus("submitting");
    setError("");
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/workflows/slug/hr-slot?ctx=${encodeURIComponent(ctx!)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            slot_iso: new Date(slot).toISOString(),
            language,
          }),
        },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail || body?.error || `Request failed (${res.status})`);
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
          Preferred interview time
        </label>
        <input
          type="datetime-local"
          value={slot}
          onChange={(e) => setSlot(e.target.value)}
          required
          className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
        />
      </div>
      <div>
        <label className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-200">
          Preferred language
        </label>
        <select
          value={language}
          onChange={(e) => setLanguage(e.target.value)}
          className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
        >
          {LANGUAGES.map((l) => (
            <option key={l.value} value={l.value}>
              {l.label}
            </option>
          ))}
        </select>
      </div>
      {error ? (
        <p className="text-sm text-rose-600 dark:text-rose-400">{error}</p>
      ) : null}
      <button
        type="submit"
        disabled={status === "submitting" || !slot}
        className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
      >
        {status === "submitting" ? "Booking…" : "Confirm slot"}
      </button>
    </form>
  );
}

export default function SlotSelectionPage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-4 py-10">
      <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
          Schedule your interview
        </h1>
        <p className="mt-1 mb-5 text-sm text-slate-600 dark:text-slate-400">
          Pick a time and your preferred language. We&apos;ll call you at the
          selected slot.
        </p>
        <Suspense fallback={<p className="text-sm text-slate-500">Loading…</p>}>
          <SlotForm />
        </Suspense>
      </div>
    </main>
  );
}
