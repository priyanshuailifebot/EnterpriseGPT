"use client";

interface NLInputProps {
  value: string;
  onChange: (next: string) => void;
  onGenerate: () => void;
  loading: boolean;
  disabled?: boolean;
}

const EXAMPLES = [
  'e.g. Every morning, fetch my unresolved Jira tickets, summarize them, and email me the report',
  "When a Salesforce lead hits MQL, pull CRM context and draft an intro email.",
  "On new PDF upload, extract action items and post to Slack.",
];

export function NLInput({
  value,
  onChange,
  onGenerate,
  loading,
  disabled,
}: NLInputProps) {
  return (
    <div className="space-y-4">
      <label className="block text-sm font-medium text-slate-800 dark:text-slate-200">
        Describe your workflow in plain English…
      </label>
      <textarea
        value={value}
        disabled={disabled || loading}
        onChange={(e) => onChange(e.target.value)}
        rows={7}
        className="w-full rounded-xl border border-slate-200 bg-white px-3 py-3 text-sm leading-relaxed text-slate-900 shadow-inner dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
        placeholder="What should your agents automate end-to-end?"
      />
      <div className="space-y-1">
        <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
          Inspiration
        </p>
        <ul className="space-y-1 text-xs text-slate-600 dark:text-slate-400">
          {EXAMPLES.map((ex) => (
            <li key={ex}>• {ex}</li>
          ))}
        </ul>
      </div>
      <button
        type="button"
        disabled={disabled || loading}
        onClick={onGenerate}
        className="inline-flex rounded-lg bg-brand-600 px-6 py-2.5 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
      >
        {loading ? "Generating…" : "Generate workflow"}
      </button>
    </div>
  );
}
