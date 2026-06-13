"use client";

import { type PropsWithChildren, Suspense } from "react";

export default function AuthLayout({ children }: PropsWithChildren) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-brand-50 via-white to-brand-50 px-4 py-10 dark:from-slate-950 dark:via-slate-900 dark:to-slate-950">
      <Suspense
        fallback={
          <div className="flex w-full max-w-md items-center justify-center rounded-2xl border border-slate-200 bg-white p-10 shadow-xl dark:border-slate-800 dark:bg-slate-900">
            <span className="text-sm text-slate-500">Loading…</span>
          </div>
        }
      >
        <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white/90 p-8 shadow-xl backdrop-blur-sm dark:border-slate-800 dark:bg-slate-900/80">
          {children}
        </div>
      </Suspense>
    </div>
  );
}
