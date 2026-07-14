"use client";

import { Loader2, Stethoscope } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";

import { cn } from "@/lib/utils";
import { useWorkflowStore } from "@/stores/workflowStore";
import type { SelfHealConfig, SelfHealPolicy } from "@/types/api";

interface SelfHealMenuProps {
  workflowId: string;
  current?: SelfHealConfig | null;
}

const POLICY_OPTIONS: { value: SelfHealPolicy; label: string; hint: string }[] = [
  { value: "off", label: "Off", hint: "Diagnose only — never change the workflow." },
  {
    value: "safe",
    label: "Safe",
    hint: "Auto-draft a validated fix; a human publishes it. Live workflows are left untouched.",
  },
  {
    value: "autonomous",
    label: "Autonomous",
    hint: "Auto-publish a verified fix (previously-live workflows only). Rolls back on failure.",
  },
];

const COOLDOWN_OPTIONS: { value: number; label: string }[] = [
  { value: 3600, label: "1 hour" },
  { value: 21600, label: "6 hours" },
  { value: 86400, label: "24 hours" },
];

export function SelfHealMenu({ workflowId, current }: SelfHealMenuProps) {
  const setSelfHeal = useWorkflowStore((s) => s.setSelfHeal);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const [enabled, setEnabled] = useState(current?.enabled ?? false);
  const [policy, setPolicy] = useState<SelfHealPolicy>(current?.policy ?? "safe");
  const [cooldown, setCooldown] = useState<number>(current?.cooldown_seconds ?? 21600);

  // Re-sync the form to the persisted config whenever the menu is opened.
  useEffect(() => {
    if (open) {
      setEnabled(current?.enabled ?? false);
      setPolicy(current?.policy ?? "safe");
      setCooldown(current?.cooldown_seconds ?? 21600);
    }
  }, [open, current]);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onEsc(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  const isOn = current?.enabled ?? false;

  async function save() {
    setSaving(true);
    try {
      await setSelfHeal(workflowId, {
        enabled,
        policy,
        cooldown_seconds: cooldown,
      });
      toast.success("Auto-heal settings saved");
      setOpen(false);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to save auto-heal settings");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={cn(
          "inline-flex items-center gap-2 rounded-xl border px-4 py-2 text-sm dark:border-slate-700",
          isOn
            ? "border-brand-600 text-brand-700 dark:border-brand-400 dark:text-brand-300"
            : "border-slate-300 text-slate-600 dark:text-slate-300",
        )}
        title="Autonomous self-heal settings"
      >
        <Stethoscope className="h-4 w-4" />
        Auto-heal: {isOn ? `On · ${current?.policy}` : "Off"}
      </button>

      {open ? (
        <div className="absolute right-0 z-40 mt-2 w-80 rounded-xl border border-slate-200 bg-white p-4 shadow-xl dark:border-slate-700 dark:bg-slate-900">
          <p className="text-[13px] font-semibold text-slate-800 dark:text-slate-100">
            Autonomous self-heal
          </p>
          <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
            Let the monitor diagnose this workflow from its runs and fix it. The
            interactive &ldquo;Diagnose &amp; Heal&rdquo; button is unaffected — it always
            asks first.
          </p>

          <label className="mt-3 flex items-center gap-2 text-[12px] text-slate-700 dark:text-slate-300">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-slate-300 dark:border-slate-700"
            />
            Enable automatic healing
          </label>

          <div className={cn("mt-3 space-y-1", !enabled && "opacity-50")}>
            <span className="text-[11px] font-medium text-slate-600 dark:text-slate-400">
              Policy
            </span>
            <select
              value={policy}
              disabled={!enabled}
              onChange={(e) => setPolicy(e.target.value as SelfHealPolicy)}
              className="w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[12px] dark:border-slate-700 dark:bg-slate-950"
            >
              {POLICY_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <p className="text-[11px] text-slate-500 dark:text-slate-400">
              {POLICY_OPTIONS.find((o) => o.value === policy)?.hint}
            </p>
          </div>

          <div className={cn("mt-3 space-y-1", !enabled && "opacity-50")}>
            <span className="text-[11px] font-medium text-slate-600 dark:text-slate-400">
              Cooldown between heals
            </span>
            <select
              value={cooldown}
              disabled={!enabled}
              onChange={(e) => setCooldown(Number(e.target.value))}
              className="w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[12px] dark:border-slate-700 dark:bg-slate-950"
            >
              {COOLDOWN_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>

          {enabled && policy === "autonomous" ? (
            <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-[11px] text-amber-700 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
              Autonomous also requires the platform monitor to be enabled and its
              auto-apply ceiling set to &ldquo;autonomous&rdquo;.
            </p>
          ) : null}

          <div className="mt-4 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-[12px] font-semibold text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => void save()}
              disabled={saving}
              className="inline-flex items-center gap-1.5 rounded-md bg-brand-600 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
            >
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
              Save
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
