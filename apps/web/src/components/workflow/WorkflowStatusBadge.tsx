import { cn } from "@/lib/utils";
import type { WorkflowStatus } from "@/types/api";

const STYLES: Record<
  WorkflowStatus,
  { label: string; className: string; title: string }
> = {
  published: {
    label: "Published",
    className:
      "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
    title: "Live — production runs perform real actions",
  },
  draft: {
    label: "Draft",
    className:
      "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200",
    title: "Draft — runs preview only until published",
  },
  archived: {
    label: "Archived",
    className:
      "border-slate-300 bg-slate-100 text-slate-600 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300",
    title: "Archived — hidden from active use",
  },
};

export function WorkflowStatusBadge({
  status,
  className,
}: {
  status: WorkflowStatus;
  className?: string;
}) {
  const cfg = STYLES[status] ?? STYLES.draft;
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-full border px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
        cfg.className,
        className,
      )}
      title={cfg.title}
    >
      {cfg.label}
    </span>
  );
}
