"use client";

import { Sparkles } from "lucide-react";
import Link from "next/link";

import { useAuthStore } from "@/stores/authStore";

export default function HomePage() {
  const appName = process.env.NEXT_PUBLIC_APP_NAME ?? "EnterpriseGPT";
  const user = useAuthStore((s) => s.user);

  return (
    <main className="flex min-h-screen flex-col items-center justify-center bg-gradient-to-br from-brand-50 via-white to-brand-50 px-6 py-12 dark:from-slate-950 dark:via-slate-900 dark:to-slate-950">
      <div className="animate-fade-in flex w-full max-w-2xl flex-col items-center gap-6 rounded-2xl border border-slate-200 bg-white/80 p-10 shadow-lg backdrop-blur-md dark:border-slate-800 dark:bg-slate-900/70">
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-brand-500 text-white shadow-sm">
          <Sparkles className="h-7 w-7" />
        </div>

        <div className="space-y-3 text-center">
          <h1 className="text-4xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
            Welcome to {appName}
          </h1>
          <p className="text-balance text-base text-slate-600 dark:text-slate-400">
            Turn natural-language commands into audited, SSE-transparent WorkFlow™ graphs.
          </p>
        </div>

        <div className="flex flex-wrap items-center justify-center gap-3">
          <Link
            href="/login"
            className="rounded-xl border border-brand-600 px-5 py-2 text-sm font-semibold text-brand-700 hover:bg-brand-50 dark:hover:bg-brand-950"
          >
            Sign in
          </Link>
          <Link
            href="/signup"
            className="rounded-xl bg-brand-600 px-5 py-2 text-sm font-semibold text-white shadow-sm hover:bg-brand-700"
          >
            Create account
          </Link>
          {user ?
            <Link
              href="/dashboard"
              className="rounded-xl bg-brand-600 px-5 py-2 text-sm font-semibold text-white"
            >
              Open dashboard
            </Link>
          : null}
        </div>

        <p className="text-xs text-slate-500 dark:text-slate-400">
          API base{" "}
          <span className="font-mono text-[11px]">
            {process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}
          </span>
        </p>
      </div>
    </main>
  );
}
