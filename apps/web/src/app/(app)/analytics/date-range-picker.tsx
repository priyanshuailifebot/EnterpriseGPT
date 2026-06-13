"use client";

import { format } from "date-fns";

type Props = {
  start: Date;
  end: Date;
  onChange: (start: Date, end: Date) => void;
};

export function DateRangePicker({ start, end, onChange }: Props) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <label className="flex items-center gap-2 text-slate-600 dark:text-slate-400">
        <span className="whitespace-nowrap">From</span>
        <input
          type="date"
          value={format(start, "yyyy-MM-dd")}
          max={format(end, "yyyy-MM-dd")}
          onChange={(e) => {
            const d = new Date(e.target.value + "T12:00:00Z");
            if (!Number.isNaN(d.getTime())) onChange(d, end);
          }}
          className="rounded-lg border border-slate-200 bg-white px-2 py-1.5 dark:border-slate-700 dark:bg-slate-900"
        />
      </label>
      <label className="flex items-center gap-2 text-slate-600 dark:text-slate-400">
        <span className="whitespace-nowrap">To</span>
        <input
          type="date"
          value={format(end, "yyyy-MM-dd")}
          min={format(start, "yyyy-MM-dd")}
          onChange={(e) => {
            const d = new Date(e.target.value + "T12:00:00Z");
            if (!Number.isNaN(d.getTime())) onChange(start, d);
          }}
          className="rounded-lg border border-slate-200 bg-white px-2 py-1.5 dark:border-slate-700 dark:bg-slate-900"
        />
      </label>
    </div>
  );
}
