"use client";

export default function SettingsPlaceholder() {
  return (
    <div className="rounded-3xl border bg-white p-8 dark:bg-slate-950 dark:border-slate-800">
      <h1 className="text-2xl font-semibold">Workspace Settings</h1>
      <p className="mt-2 max-w-xl text-sm text-slate-600 dark:text-slate-400">
        User lifecycle, SSO, API keys — scheduled for hardened Phase 7 admin flows.
      </p>
    </div>
  );
}
