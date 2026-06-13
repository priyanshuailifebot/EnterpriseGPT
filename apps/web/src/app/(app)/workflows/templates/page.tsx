"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";

interface TemplateSummary {
  slug: string;
  title: string;
  summary: string;
  category: string;
  prompt: string;
  required_integrations: string[];
  // The full WorkflowDefinition is returned but the gallery only renders
  // a count — the user picks "Use prompt" (rebuild via clarification) or
  // "Use as-is" (skip clarification and save the bundled definition).
  definition: {
    name: string;
    nodes?: { kind: string; id: string; name: string }[];
    agents?: unknown[];
  };
}

interface Catalog {
  templates: TemplateSummary[];
}

const _CATEGORY_LABEL: Record<string, string> = {
  "customer-service": "Customer Service",
  hr: "HR Recruitment",
};

export default function WorkflowTemplatesPage() {
  const [data, setData] = useState<Catalog | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        const { data: payload } = await api.get<Catalog>(
          "/api/v1/workflows/templates",
        );
        setData(payload);
      } catch (e: unknown) {
        setErr(e instanceof Error ? e.message : "failed to load templates");
      }
    }
    void load();
  }, []);

  if (err) {
    return (
      <div className="mx-auto max-w-5xl py-12">
        <p className="text-sm text-red-600">Failed to load templates: {err}</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-8 py-8">
      <header>
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-50">
          Workflow templates
        </h1>
        <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
          One-click starting points. Each template ships a prebaked v2
          definition (control flow, fan-out, webhook pauses) and the original
          natural-language prompt — use either path.
        </p>
      </header>

      {data === null ?
        <p className="text-sm text-slate-500">Loading…</p>
      : <div className="space-y-5">
          {data.templates.map((t) => (
            <TemplateCard key={t.slug} t={t} />
          ))}
        </div>
      }
    </div>
  );
}

function TemplateCard({ t }: { t: TemplateSummary }) {
  const nodeCount = t.definition.nodes?.length ?? t.definition.agents?.length ?? 0;
  const kinds = (t.definition.nodes ?? [])
    .map((n) => n.kind)
    .reduce<Record<string, number>>((acc, k) => {
      acc[k] = (acc[k] ?? 0) + 1;
      return acc;
    }, {});

  return (
    <article className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-950">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <span className="inline-block rounded-full bg-brand-50 px-2.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-brand-700 dark:bg-brand-950 dark:text-brand-300">
            {_CATEGORY_LABEL[t.category] ?? t.category}
          </span>
          <h2 className="mt-2 text-lg font-semibold text-slate-900 dark:text-slate-100">
            {t.title}
          </h2>
          <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
            {t.summary}
          </p>

          <div className="mt-4 flex flex-wrap gap-3 text-xs text-slate-500 dark:text-slate-400">
            <span>
              <strong>{nodeCount}</strong> nodes
            </span>
            {Object.entries(kinds).map(([k, n]) => (
              <span key={k}>
                <strong>{n}</strong> {k}
              </span>
            ))}
          </div>

          {t.required_integrations.length > 0 ?
            <p className="mt-4 text-xs text-slate-500 dark:text-slate-400">
              Requires:&nbsp;
              {t.required_integrations.map((id, i) => (
                <span key={id}>
                  <code className="rounded bg-slate-100 px-1.5 py-0.5 dark:bg-slate-800">
                    {id}
                  </code>
                  {i < t.required_integrations.length - 1 ? ", " : ""}
                </span>
              ))}
            </p>
          : null}
        </div>

        <div className="flex shrink-0 flex-col gap-2">
          <Link
            href={`/workflows/new?prompt=${encodeURIComponent(t.prompt)}&template=${t.slug}`}
            className="rounded-xl bg-brand-600 px-4 py-2 text-center text-sm font-medium text-white shadow-sm hover:bg-brand-700"
          >
            Use this template
          </Link>
          <button
            type="button"
            onClick={() => navigator.clipboard?.writeText(t.prompt)}
            className="rounded-xl border border-slate-300 px-4 py-2 text-center text-sm font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-900"
          >
            Copy prompt
          </button>
        </div>
      </div>
    </article>
  );
}
