"use client";

import * as Collapsible from "@radix-ui/react-collapsible";
import { ChevronDown, Cpu, Loader2, Mail, CheckCircle2, AlertCircle, Clock, Download, Copy } from "lucide-react";
import { motion } from "framer-motion";
import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";
import { copyReportToClipboard, downloadPdfBase64, downloadReportAsPdf } from "@/lib/report-export";
import type { ActionStepView, AgentStepView, ExecRuntimeStatus } from "@/stores/executionStore";

function tryParseJson(value: string): unknown | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (!/^[\[{]/.test(trimmed)) return null;
  try {
    return JSON.parse(trimmed);
  } catch {
    return null;
  }
}

function extractReadable(parsed: unknown): string | null {
  if (!parsed || typeof parsed !== "object") return null;
  // Common shapes from Dynamiq: { <agent_id>: { output: { content: "..." } } }
  // or { output: { content: "..." } } or { content: "..." }.
  const visit = (node: unknown, depth: number): string | null => {
    if (depth > 4 || !node || typeof node !== "object") return null;
    const rec = node as Record<string, unknown>;
    for (const key of ["content", "message", "text", "final_answer", "answer"]) {
      const v = rec[key];
      if (typeof v === "string" && v.trim().length > 0) return v;
    }
    for (const key of ["output", "result", "data"]) {
      const inner = rec[key];
      const found = visit(inner, depth + 1);
      if (found) return found;
    }
    // Single-key wrapper: { <agent_id>: {...} }
    const keys = Object.keys(rec);
    if (keys.length === 1) {
      const found = visit(rec[keys[0]!], depth + 1);
      if (found) return found;
    }
    return null;
  };
  return visit(parsed, 0);
}

function StatusBadge({ status }: { status: ExecRuntimeStatus }) {
  const palette: Record<ExecRuntimeStatus, string> = {
    idle:
      "border-slate-200 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200",
    running:
      "border-blue-200 bg-blue-50 text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-100",
    complete:
      "border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-100",
    error:
      "border-red-200 bg-red-50 text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-100",
    awaiting_approval:
      "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100",
  };

  return (
    <span
      className={cn(
        "rounded-full border px-3 py-1 text-xs font-semibold capitalize",
        palette[status],
      )}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

function initials(name: string) {
  const [a, b] = name.split(/\s+/).filter(Boolean);
  return ((a?.[0] ?? "?") + (b?.[0] ?? "")).toUpperCase();
}

function Typewriter({ text }: { text: string }) {
  const [slice, setSlice] = useState(0);
  useEffect(() => {
    setSlice(0);
  }, [text]);
  useEffect(() => {
    if (!text) return;
    if (slice >= text.length) return;
    const id = window.setTimeout(() => setSlice(slice + Math.max(4, Math.floor(Math.random()*6))), 32);
    return () => window.clearTimeout(id);
  }, [slice, text]);
  return <span>{text.slice(0, slice)}</span>;
}

const mdComponents = {
  p: ({ children }: React.PropsWithChildren) => (
    <p className="mb-2 last:mb-0 text-sm leading-relaxed text-slate-700 dark:text-slate-300">
      {children}
    </p>
  ),
  strong: ({ children }: React.PropsWithChildren) => (
    <strong className="font-semibold text-slate-900 dark:text-slate-100">{children}</strong>
  ),
  ul: ({ children }: React.PropsWithChildren) => (
    <ul className="mb-2 ml-4 list-disc space-y-0.5 text-sm text-slate-700 dark:text-slate-300">
      {children}
    </ul>
  ),
  ol: ({ children }: React.PropsWithChildren) => (
    <ol className="mb-2 ml-4 list-decimal space-y-0.5 text-sm text-slate-700 dark:text-slate-300">
      {children}
    </ol>
  ),
  li: ({ children }: React.PropsWithChildren) => <li className="leading-relaxed">{children}</li>,
  a: ({ href, children }: React.PropsWithChildren<{ href?: string }>) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-brand-600 underline hover:text-brand-800 dark:text-brand-400"
    >
      {children}
    </a>
  ),
  h1: ({ children }: React.PropsWithChildren) => (
    <h1 className="mb-2 text-base font-bold text-slate-900 dark:text-slate-100">{children}</h1>
  ),
  h2: ({ children }: React.PropsWithChildren) => (
    <h2 className="mb-1.5 text-sm font-semibold text-slate-800 dark:text-slate-200">{children}</h2>
  ),
  h3: ({ children }: React.PropsWithChildren) => (
    <h3 className="mb-1 text-sm font-medium text-slate-700 dark:text-slate-300">{children}</h3>
  ),
  code: ({ children }: React.PropsWithChildren) => (
    <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[11px] text-slate-800 dark:bg-slate-800 dark:text-slate-200">
      {children}
    </code>
  ),
  pre: ({ children }: React.PropsWithChildren) => (
    <pre className="mb-2 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] text-slate-100">
      {children}
    </pre>
  ),
  hr: () => <hr className="my-3 border-slate-200 dark:border-slate-700" />,
};

function MarkdownOutput({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
      {text}
    </ReactMarkdown>
  );
}

function ReportToolbar({ title, markdown, meta }: { title: string; markdown: string; meta?: string }) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3 dark:border-slate-800">
      <button
        type="button"
        onClick={() => void downloadReportAsPdf({ title, markdown, meta })}
        className="inline-flex items-center gap-1.5 rounded-lg bg-brand-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-brand-700"
      >
        <Download className="h-3.5 w-3.5" /> Download as PDF
      </button>
      <button
        type="button"
        onClick={() => void copyReportToClipboard(markdown)}
        className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
      >
        <Copy className="h-3.5 w-3.5" /> Copy
      </button>
      <span className="ml-auto text-[10px] text-slate-400 dark:text-slate-500">
        PDF is generated on the server and saved directly to your downloads folder
      </span>
    </div>
  );
}

function AgentOutput({
  text,
  status,
  agentName,
}: {
  text: string;
  status: ExecRuntimeStatus;
  agentName?: string;
}) {
  const parsed = useMemo(() => tryParseJson(text), [text]);
  const readable = useMemo(
    () => (parsed ? extractReadable(parsed) : null),
    [parsed],
  );

  const reportTitle = agentName ? `${agentName} — Report` : "Agent Report";
  const isDone = status === "complete";
  const reportText = readable ?? (parsed ? null : text);
  const showToolbar = isDone && typeof reportText === "string" && reportText.trim().length > 80;

  if (parsed && readable) {
    return (
      <div className="space-y-2">
        {status === "running" ? (
          <div className="whitespace-pre-wrap text-sm leading-relaxed text-slate-700 dark:text-slate-300">
            <Typewriter text={readable} />
          </div>
        ) : (
          <MarkdownOutput text={readable} />
        )}
        <Collapsible.Root>
          <Collapsible.Trigger className="inline-flex items-center gap-1 text-[11px] font-medium text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200">
            <ChevronDown className="h-3 w-3" />
            View raw payload
          </Collapsible.Trigger>
          <Collapsible.Content>
            <pre className="mt-2 max-h-72 overflow-auto rounded-lg border border-slate-200 bg-slate-50 p-3 text-[11px] text-slate-700 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200">
              {JSON.stringify(parsed, null, 2)}
            </pre>
          </Collapsible.Content>
        </Collapsible.Root>
        {showToolbar && reportText ? <ReportToolbar title={reportTitle} markdown={reportText} meta={agentName} /> : null}
      </div>
    );
  }

  if (parsed) {
    return (
      <pre className="max-h-72 overflow-auto rounded-lg border border-slate-200 bg-slate-50 p-3 font-mono text-[11px] leading-relaxed text-slate-700 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200">
        {JSON.stringify(parsed, null, 2)}
      </pre>
    );
  }

  return (
    <div className="space-y-2">
      {status === "running" ? (
        <div className="whitespace-pre-wrap text-sm leading-relaxed text-slate-700 dark:text-slate-300">
          <Typewriter text={text} />
        </div>
      ) : (
        <MarkdownOutput text={text} />
      )}
      {showToolbar && reportText ? <ReportToolbar title={reportTitle} markdown={reportText} meta={agentName} /> : null}
    </div>
  );
}

function ActionStepCard({ step }: { step: ActionStepView }) {
  const palette: Record<ActionStepView["status"], { border: string; bg: string; icon: React.ReactNode; label: string }> = {
    ok: {
      border: "border-emerald-200 dark:border-emerald-900",
      bg: "bg-emerald-50 dark:bg-emerald-950",
      icon: <CheckCircle2 className="h-5 w-5 text-emerald-600 dark:text-emerald-400" />,
      label: "Completed",
    },
    dry_run: {
      border: "border-amber-200 dark:border-amber-900",
      bg: "bg-amber-50 dark:bg-amber-950",
      icon: <AlertCircle className="h-5 w-5 text-amber-600 dark:text-amber-400" />,
      label: "Preview \u2014 publish to run live",
    },
    hitl: {
      border: "border-blue-200 dark:border-blue-900",
      bg: "bg-blue-50 dark:bg-blue-950",
      icon: <Clock className="h-5 w-5 text-blue-600 dark:text-blue-400" />,
      label: "Approval gate",
    },
    error: {
      border: "border-red-200 dark:border-red-900",
      bg: "bg-red-50 dark:bg-red-950",
      icon: <AlertCircle className="h-5 w-5 text-red-600 dark:text-red-400" />,
      label: "Failed",
    },
  };

  const p = palette[step.status];
  const isEmail = step.provider === "gmail" || step.actionSlug.toLowerCase().includes("email");
  const icon = isEmail ? <Mail className="h-5 w-5 text-slate-500" /> : p.icon;

  const note = step.result
    ? (step.result.data as Record<string, unknown> | undefined)?.note as string | undefined
    : step.message;

  const pdfBase64 = step.result
    ? ((step.result.data as Record<string, unknown> | undefined)?.pdf_base64 as
        | string
        | undefined)
    : undefined;
  const pdfFilename = step.result
    ? String(
        (step.result.data as Record<string, unknown> | undefined)?.filename ??
          "report.pdf",
      )
    : "report.pdf";

  function downloadPdf() {
    if (!pdfBase64) return;
    downloadPdfBase64(pdfBase64, pdfFilename);
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn("rounded-2xl border p-4 shadow-sm", p.border, p.bg)}
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5 shrink-0">{icon}</div>
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">{step.name}</p>
            <span className="rounded-full border px-2 py-0.5 text-[11px] font-medium border-current opacity-70">
              {p.label}
            </span>
          </div>
          {note ? (
            <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">{note}</p>
          ) : null}
          {step.status === "dry_run" && isEmail ? (
            <p className="mt-1.5 text-xs text-amber-800 dark:text-amber-300">
              To send this email, add a <code className="rounded bg-amber-100 px-1 dark:bg-amber-900">recipient_email</code> field to the workflow&apos;s trigger form and re-run.
            </p>
          ) : null}
          {pdfBase64 ? (
            <button
              type="button"
              onClick={downloadPdf}
              className="mt-2 inline-flex items-center gap-1.5 rounded-lg bg-brand-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm hover:bg-brand-700"
            >
              <Download className="h-3.5 w-3.5" /> Download PDF
            </button>
          ) : null}
          {step.status === "ok" && step.result ? (
            <Collapsible.Root>
              <Collapsible.Trigger className="mt-2 inline-flex items-center gap-1 text-[11px] font-medium text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200">
                <ChevronDown className="h-3 w-3" /> View result
              </Collapsible.Trigger>
              <Collapsible.Content>
                <pre className="mt-2 max-h-52 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] text-slate-100">
                  {JSON.stringify(step.result, null, 2)}
                </pre>
              </Collapsible.Content>
            </Collapsible.Root>
          ) : null}
        </div>
      </div>
    </motion.div>
  );
}

export function StepTimeline({
  steps,
  actionSteps = [],
}: {
  steps: AgentStepView[];
  actionSteps?: ActionStepView[];
}) {
  return (
    <div className="space-y-4">
      {steps.map((step, idx) => (
        <motion.div
          key={step.agentId}
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: idx * 0.04 }}
          className={cn(
            "rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-950",
            step.status === "running" &&
              "border-brand-200 ring-1 ring-brand-100 dark:border-brand-900 dark:ring-brand-950",
          )}
        >
          <div className="rounded-2xl p-4">
            <div className="flex flex-wrap gap-4">
              <div
                className={cn(
                  "relative flex h-12 w-12 items-center justify-center rounded-xl bg-brand-600 text-sm font-semibold text-white shadow-sm",
                  step.status === "running" &&
                    "after:absolute after:inset-0 after:rounded-xl after:ring-2 after:ring-brand-400 after:animate-ping",
                )}
              >
                {initials(step.agentName)}
              </div>
              <div className="flex-1 space-y-3">
                <div className="flex flex-wrap items-center gap-3">
                  <p className="text-base font-semibold text-slate-900 dark:text-slate-100">
                    {step.agentName}
                  </p>
                  <StatusBadge status={step.status} />
                  {step.status === "running" ? (
                    <span className="inline-flex items-center gap-1 text-[11px] text-brand-700 dark:text-brand-300">
                      <Loader2 className="h-3 w-3 animate-spin" />
                      thinking
                    </span>
                  ) : null}
                </div>
                {step.output ? (
                  <AgentOutput text={step.output} status={step.status} agentName={step.agentName} />
                ) : step.status === "running" ? (
                  <div className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                    <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-brand-500" />
                    Agent reasoning & tool routing…
                  </div>
                ) : null}

                {step.tools.length ? (
                  <div className="space-y-2">
                    <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                      Tool transcripts
                    </p>
                    {step.tools.map((t, ti) => (
                      <Collapsible.Root key={`${step.agentId}-${t.tool}-${ti}`}>
                        <Collapsible.Trigger className="flex w-full items-center gap-2 rounded-xl border bg-slate-50 px-3 py-2 text-left text-xs font-medium text-slate-700 hover:bg-slate-100 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800">
                          <Cpu className="h-4 w-4" />
                          {t.tool}
                          <ChevronDown className="ml-auto h-4 w-4" />
                        </Collapsible.Trigger>
                        <Collapsible.Content className="mt-2 space-y-3 rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900">
                          <div>
                            <p className="text-[11px] font-semibold text-slate-500">
                              Input
                            </p>
                            <pre className="mt-1 max-h-52 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] text-slate-100">
                              {JSON.stringify(t.input ?? {}, null, 2)}
                            </pre>
                          </div>
                          <div>
                            <p className="text-[11px] font-semibold text-slate-500">
                              Output
                            </p>
                            <pre className="mt-1 max-h-52 overflow-auto rounded-lg bg-slate-950 p-3 text-[11px] text-slate-100">
                              {JSON.stringify(t.output ?? {}, null, 2)}
                            </pre>
                          </div>
                        </Collapsible.Content>
                      </Collapsible.Root>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
          </div>
        </motion.div>
      ))}
      {actionSteps.map((step, idx) => (
        <ActionStepCard key={`${step.nodeId}-${idx}`} step={step} />
      ))}
    </div>
  );
}

export function SummaryCard(props: {
  status: ExecRuntimeStatus;
  totalTools: number;
  agentsFinished: number;
  agentsRun?: number;
  agentsSkipped?: number;
  actionsSucceeded?: number;
  actionsDryRun?: number;
  nodesSkipped?: number;
}) {
  const agentsRun = props.agentsRun ?? props.agentsFinished;
  const agentsSkipped = props.agentsSkipped ?? 0;
  const actionsSucceeded = props.actionsSucceeded ?? props.totalTools;
  const actionsDryRun = props.actionsDryRun ?? 0;
  const nodesSkipped = props.nodesSkipped ?? 0;
  return (
    <div className="rounded-3xl border border-emerald-200 bg-emerald-50 p-6 text-sm text-emerald-900 shadow-sm dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-100">
      <p className="text-xs uppercase tracking-wide opacity-75">
        Workflow finished
      </p>
      <p className="mt-3 text-xl font-semibold">
        Agents · {agentsRun}
        {agentsSkipped > 0 ? (
          <span className="opacity-70"> ({agentsSkipped} skipped)</span>
        ) : null}{" "}
        <span className="opacity-60">·</span> Actions · {actionsSucceeded}
        {actionsDryRun > 0 ? (
          <span className="opacity-70"> ({actionsDryRun} dry-run)</span>
        ) : null}{" "}
        <span className="opacity-60">·</span> Skipped · {nodesSkipped}
      </p>
      <p className="mt-3 text-xs opacity-80">
        Latest status:&nbsp;<strong>{props.status}</strong>
      </p>
    </div>
  );
}
