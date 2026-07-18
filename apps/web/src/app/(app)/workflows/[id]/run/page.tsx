"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";

import { SummaryCard, StepTimeline } from "@/components/execution/StepTimeline";
import { api, ssePostStream } from "@/lib/api";
import { useExecutionStore } from "@/stores/executionStore";
import { draftDefinitionFromDetail, useWorkflowStore } from "@/stores/workflowStore";
import type {
  ExecutionEvent,
  TriggerFormField,
  TriggerNode,
  WorkflowDefinition,
  WorkflowNode,
} from "@/types/api";

type FormValue = string | string[];
type FormState = Record<string, FormValue>;

export default function WorkflowRunPage() {
  const params = useParams<{ id: string }>();
  const workflowId = params.id;

  const fetchDetail = useWorkflowStore((s) => s.fetchWorkflowDetail);
  const current = useWorkflowStore((s) => s.currentWorkflow);
  const publishWorkflow = useWorkflowStore((s) => s.publishWorkflow);
  const unpublishWorkflow = useWorkflowStore((s) => s.unpublishWorkflow);
  const [togglingPublish, setTogglingPublish] = useState(false);

  const appendEvent = useExecutionStore((s) => s.appendEvent);
  const startCanvas = useExecutionStore((s) => s.startCanvas);
  const approveHitl = useExecutionStore((s) => s.approveHitl);

  const [sessionKey, setSessionKey] = useState<string | null>(null);
  const executor = useExecutionStore((s) =>
    sessionKey ? s.executions[sessionKey] : undefined,
  );

  const [formState, setFormState] = useState<FormState>({});
  const [advancedMode, setAdvancedMode] = useState(false);
  const [inputJson, setInputJson] = useState("{}");
  const [running, setRunning] = useState(false);
  const [feedback, setFeedback] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    void fetchDetail(workflowId);
    return () => {
      abortRef.current?.abort();
    };
  }, [fetchDetail, workflowId]);

  const definition = draftDefinitionFromDetail(current ?? null);
  const trigger = useMemo(() => findTrigger(definition), [definition]);

  // Initialise form state from trigger.form_fields once the definition loads.
  useEffect(() => {
    if (!trigger || trigger.trigger_type !== "form") return;
    setFormState((prev) => {
      // Don't clobber values the user has already typed.
      if (Object.keys(prev).length > 0) return prev;
      const next: FormState = {};
      for (const f of trigger.form_fields) {
        next[f.key] = f.type === "multi_choice" ? [] : "";
      }
      return next;
    });
  }, [trigger]);

  // Pre-fill the advanced JSON box with a trigger-aware sample so the agent
  // has real input to reason about (otherwise it just asks for "more details").
  useEffect(() => {
    if (!trigger) return;
    let cancelled = false;
    async function loadSample() {
      try {
        const { data } = await api.get<{ input_data: Record<string, unknown> }>(
          `/api/v1/workflows/${workflowId}/sample_input`,
        );
        if (!cancelled) {
          setInputJson(JSON.stringify(data.input_data ?? {}, null, 2));
        }
      } catch {
        if (!cancelled) {
          const stub =
            trigger?.trigger_type === "webhook"
              ? {
                  message:
                    "Hi, I'm an existing customer and I have an issue with my recent order.",
                  customer_id: "cust_demo_001",
                  email: "demo@example.com",
                }
              : trigger?.trigger_type === "chat"
                ? { message: "Hello!" }
                : {};
          setInputJson(JSON.stringify(stub, null, 2));
        }
      }
    }
    void loadSample();
    return () => {
      cancelled = true;
    };
  }, [trigger, workflowId]);

  const buildPayload = useCallback((): Record<string, unknown> | null => {
    if (advancedMode) {
      try {
        const raw = JSON.parse(inputJson.trim() || "{}");
        if (raw && typeof raw === "object" && !Array.isArray(raw)) {
          return raw as Record<string, unknown>;
        }
        return {};
      } catch {
        toast.error("Input JSON appears invalid.");
        return null;
      }
    }

    // Form trigger — or a manual trigger that declares input fields — collect
    // the typed fields (manual workflows like HR Sourcing take a JD + role).
    if (
      trigger &&
      (trigger.trigger_type === "form" ||
        (trigger.trigger_type === "manual" &&
          (trigger.form_fields?.length ?? 0) > 0))
    ) {
      for (const f of trigger.form_fields) {
        if (!f.required) continue;
        const v = formState[f.key];
        const missing =
          v === undefined ||
          (typeof v === "string" && v.trim() === "") ||
          (Array.isArray(v) && v.length === 0);
        if (missing) {
          toast.error(`"${f.label}" is required.`);
          return null;
        }
      }
      return { ...formState };
    }

    // For webhook / chat / schedule / manual triggers in basic mode, use the
    // sample payload we pre-loaded so the agent has real input to reason about.
    if (inputJson.trim()) {
      try {
        const parsed = JSON.parse(inputJson);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          return parsed as Record<string, unknown>;
        }
      } catch {
        // Fall through to empty payload.
      }
    }
    return {};
  }, [advancedMode, formState, inputJson, trigger]);

  const beginStream = useCallback(async () => {
    if (!definition) return;
    const parsed = buildPayload();
    if (parsed === null) return;

    abortRef.current?.abort();
    abortRef.current = new AbortController();

    const { sessionKey: key } = startCanvas(workflowId);
    setSessionKey(key);
    setRunning(true);

    try {
      await ssePostStream({
        path: `/api/v1/workflows/${workflowId}/execute`,
        body: {
          input_data: parsed,
          variables: {},
        },
        signal: abortRef.current.signal,
        onEvent: (evt) => appendEvent(key, unwrapExecution(evt)),
      });
    } catch (e: unknown) {
      if ((e as Error).name !== "AbortError") {
        toast.error(e instanceof Error ? e.message : "Execution interrupted");
      }
    } finally {
      setRunning(false);
    }
  }, [appendEvent, buildPayload, definition, startCanvas, workflowId]);

  const handleSubmit = async (evt: FormEvent) => {
    evt.preventDefault();
    await beginStream();
  };

  const execId = executor?.executionId ?? hitlFallback(executor?.events ?? []);
  const hitlAwaiting =
    executor?.status === "awaiting_approval" && Boolean(execId);

  async function approve(approved: boolean) {
    if (!execId) {
      toast.error("Missing execution id for checkpoint.");
      return;
    }
    try {
      await approveHitl(workflowId, execId, {
        approved,
        feedback: feedback || null,
      });
      setFeedback("");
      toast.success(
        approved
          ? "Approval delivered — streaming resumes shortly."
          : "Rejection relayed.",
      );
    } catch {
      toast.error("Could not record approval.");
    }
  }

  const summaryVisible = executor?.status === "complete";
  const wfStatus = current?.workflow.status ?? null;
  const isPublished = wfStatus === "published";

  const togglePublish = useCallback(async () => {
    setTogglingPublish(true);
    try {
      if (isPublished) {
        await unpublishWorkflow(workflowId);
        toast.success("Reverted to draft — runs now preview.");
      } else {
        await publishWorkflow(workflowId);
        toast.success("Published — actions will run for real now.");
      }
      await fetchDetail(workflowId);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Publish toggle failed");
    } finally {
      setTogglingPublish(false);
    }
  }, [
    fetchDetail,
    isPublished,
    publishWorkflow,
    unpublishWorkflow,
    workflowId,
  ]);

  return (
    <div className="mx-auto max-w-5xl space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase text-slate-500">Execute</p>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-50">
              {definition?.name ?? "Workflow"}
            </h1>
            {wfStatus ? (
              <span
                className={
                  isPublished
                    ? "rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200"
                    : "rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wide text-amber-800 dark:bg-amber-950 dark:text-amber-200"
                }
              >
                {wfStatus}
              </span>
            ) : null}
          </div>
          <p className="max-w-xl text-sm text-slate-600 dark:text-slate-400">
            {definition?.trigger ||
              definition?.description ||
              "Feed inputs to the workflow and watch checkpoints stream live."}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {wfStatus ? (
            <button
              type="button"
              onClick={() => void togglePublish()}
              disabled={togglingPublish}
              className={
                isPublished
                  ? "rounded-xl border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
                  : "rounded-xl bg-emerald-600 px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-emerald-700 disabled:opacity-50"
              }
              title={
                isPublished
                  ? "Revert to draft (runs will preview again)"
                  : "Publish so actions run for real on the next execution"
              }
            >
              {togglingPublish
                ? "Updating\u2026"
                : isPublished
                  ? "Unpublish"
                  : "Publish workflow"}
            </button>
          ) : null}
          <Link
            href={`/workflows/${workflowId}`}
            className="text-sm font-medium text-brand-700 underline"
          >
            Back to detail
          </Link>
        </div>
      </div>

      {wfStatus && !isPublished ? (
        <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100">
          <p className="font-semibold">Draft mode — actions run in preview</p>
          <p className="mt-1 text-xs opacity-90">
            Side-effecting actions (send email, create ticket, etc.) won't fire
            for real until you publish the workflow. Agents still call the LLM so
            you can validate the reasoning end-to-end.
          </p>
        </div>
      ) : null}

      <form className="space-y-5" onSubmit={handleSubmit}>
        <TriggerInput
          trigger={trigger}
          workflowId={workflowId}
          formState={formState}
          setFormState={setFormState}
          advancedMode={advancedMode}
          setAdvancedMode={setAdvancedMode}
          inputJson={inputJson}
          setInputJson={setInputJson}
        />

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={running || !definition}
            className="rounded-xl bg-brand-600 px-6 py-2 text-sm font-semibold text-white disabled:opacity-50"
          >
            {running ? "Streaming…" : executeLabel(trigger)}
          </button>
          {trigger && !advancedMode ? (
            <button
              type="button"
              onClick={() => setAdvancedMode(true)}
              className="text-xs text-slate-500 underline hover:text-slate-700 dark:hover:text-slate-300"
            >
              Advanced (raw JSON)
            </button>
          ) : null}
          {advancedMode ? (
            <button
              type="button"
              onClick={() => setAdvancedMode(false)}
              className="text-xs text-slate-500 underline hover:text-slate-700 dark:hover:text-slate-300"
            >
              Back to form
            </button>
          ) : null}
        </div>
      </form>

      {hitlAwaiting ? (
        <div className="space-y-3 rounded-3xl border border-amber-200 bg-amber-50 p-6 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-50">
          <p className="font-semibold">Human approval required</p>
          <p className="text-xs opacity-80">
            Keep this page open while the LangGraph interrupt waits for your decision.
            Approve or reject to resume the open SSE stream.
          </p>
          <textarea
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            rows={3}
            className="w-full rounded-2xl border border-amber-200 bg-white p-3 text-xs text-slate-900 dark:border-amber-800 dark:bg-slate-950 dark:text-slate-50"
            placeholder="Optional feedback for compliance / audit"
          />
          <div className="flex flex-wrap gap-3">
            <button
              type="button"
              className="rounded-xl bg-emerald-600 px-4 py-2 text-sm font-semibold text-white"
              onClick={() => void approve(true)}
            >
              Approve
            </button>
            <button
              type="button"
              className="rounded-xl bg-red-600 px-4 py-2 text-sm font-semibold text-white"
              onClick={() => void approve(false)}
            >
              Reject
            </button>
          </div>
        </div>
      ) : null}

      {executor?.steps?.length || executor?.actionSteps?.length ? (
        <StepTimeline steps={executor.steps} actionSteps={executor.actionSteps} />
      ) : (
        <p className="text-sm text-slate-500">
          Execution timeline appears as agents start via SSE.
        </p>
      )}

      {summaryVisible && executor?.summary ? (
        <SummaryCard
          status={executor.status}
          totalTools={executor.summary.toolCalls}
          agentsFinished={executor.summary.agentsCompleted}
          agentsRun={executor.summary.agentsRun}
          agentsSkipped={executor.summary.agentsSkipped}
          actionsSucceeded={executor.summary.actionsSucceeded}
          actionsDryRun={executor.summary.actionsDryRun}
          nodesSkipped={executor.summary.nodesSkipped}
        />
      ) : null}

      {executor?.status === "error" ? (
        <div className="rounded-2xl border border-red-200 bg-red-50 p-4 text-sm text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-50">
          {executor.errorMessage}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Trigger-aware input renderer
// ---------------------------------------------------------------------------

function TriggerInput({
  trigger,
  workflowId,
  formState,
  setFormState,
  advancedMode,
  setAdvancedMode,
  inputJson,
  setInputJson,
}: {
  trigger: TriggerNode | null;
  workflowId: string;
  formState: FormState;
  setFormState: (next: FormState | ((prev: FormState) => FormState)) => void;
  advancedMode: boolean;
  setAdvancedMode: (v: boolean) => void;
  inputJson: string;
  setInputJson: (s: string) => void;
}) {
  if (advancedMode) {
    return (
      <label className="block text-sm font-medium text-slate-700 dark:text-slate-200">
        Trigger payload (JSON)
        <textarea
          value={inputJson}
          onChange={(e) => setInputJson(e.target.value)}
          rows={18}
          spellCheck={false}
          className="mt-2 min-h-[360px] w-full resize-y rounded-2xl border border-slate-200 bg-slate-950 p-5 font-mono text-sm leading-6 text-slate-50 dark:border-slate-800"
        />
      </label>
    );
  }

  if (!trigger) {
    return (
      <EmptyCard
        title="No trigger configured"
        body="This workflow has no trigger node. Switch to advanced mode to send a raw payload."
        onAdvanced={() => setAdvancedMode(true)}
      />
    );
  }

  switch (trigger.trigger_type) {
    case "form":
      return (
        <FormTriggerInputs
          fields={trigger.form_fields}
          formState={formState}
          setFormState={setFormState}
        />
      );
    case "manual":
      // A manual trigger can still declare input fields (e.g. HR Sourcing's
      // Job description + Role title). Render them when present.
      return trigger.form_fields?.length ? (
        <FormTriggerInputs
          fields={trigger.form_fields}
          formState={formState}
          setFormState={setFormState}
        />
      ) : (
        <InfoCard
          title="Manual trigger"
          body="No inputs are required. Click Execute to run the workflow now."
        />
      );
    case "webhook":
      return (
        <WebhookInfo
          workflowId={workflowId}
          trigger={trigger}
          inputJson={inputJson}
          setInputJson={setInputJson}
        />
      );
    case "schedule":
      return (
        <InfoCard
          title="Scheduled trigger"
          body={
            trigger.schedule_cron
              ? `Runs automatically on the cron schedule: ${trigger.schedule_cron}. Click Execute to run it manually now.`
              : "Runs on a schedule. Click Execute to run it manually now."
          }
        />
      );
    case "chat":
      return (
        <InfoCard
          title="Chat trigger"
          body="This workflow runs from the chat interface, not this page."
        >
          <Link
            href="/chat"
            className="mt-2 inline-block rounded-xl bg-brand-600 px-4 py-2 text-sm font-semibold text-white"
          >
            Open chat
          </Link>
        </InfoCard>
      );
    default:
      return (
        <EmptyCard
          title="Unknown trigger type"
          body="Use advanced mode to send a raw JSON payload."
          onAdvanced={() => setAdvancedMode(true)}
        />
      );
  }
}

function FormTriggerInputs({
  fields,
  formState,
  setFormState,
}: {
  fields: TriggerFormField[];
  formState: FormState;
  setFormState: (next: FormState | ((prev: FormState) => FormState)) => void;
}) {
  if (fields.length === 0) {
    return (
      <InfoCard
        title="Form trigger"
        body="No form fields were defined on the trigger. Click Execute to run with an empty payload."
      />
    );
  }
  return (
    <div className="space-y-4 rounded-2xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
      {fields.map((f) => {
        const value = formState[f.key];
        const update = (v: FormValue) =>
          setFormState((prev) => ({ ...prev, [f.key]: v }));
        return (
          <div key={f.key} className="space-y-1.5">
            <label className="block text-sm font-medium text-slate-700 dark:text-slate-200">
              {f.label}
              {f.required ? <span className="ml-1 text-red-600">*</span> : null}
            </label>
            {f.type === "text" ? (
              <textarea
                value={typeof value === "string" ? value : ""}
                onChange={(e) => update(e.target.value)}
                rows={3}
                className="w-full resize-y rounded-xl border border-slate-200 bg-white p-3 text-sm text-slate-900 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
              />
            ) : f.type === "choice" ? (
              <select
                value={typeof value === "string" ? value : ""}
                onChange={(e) => update(e.target.value)}
                className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
              >
                <option value="">Select…</option>
                {(f.options ?? []).map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </select>
            ) : (
              <div className="flex flex-wrap gap-2">
                {(f.options ?? []).map((opt) => {
                  const arr = Array.isArray(value) ? value : [];
                  const selected = arr.includes(opt);
                  return (
                    <button
                      type="button"
                      key={opt}
                      onClick={() =>
                        update(
                          selected ? arr.filter((x) => x !== opt) : [...arr, opt],
                        )
                      }
                      className={
                        selected
                          ? "rounded-full bg-brand-600 px-3 py-1 text-xs font-medium text-white"
                          : "rounded-full border border-slate-300 px-3 py-1 text-xs text-slate-700 dark:border-slate-600 dark:text-slate-300"
                      }
                    >
                      {opt}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function WebhookInfo({
  workflowId,
  trigger,
  inputJson,
  setInputJson,
}: {
  workflowId: string;
  trigger: TriggerNode;
  inputJson: string;
  setInputJson: (s: string) => void;
}) {
  const apiBase =
    process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ??
    (typeof window !== "undefined" ? window.location.origin : "");
  const slug = trigger.slug || trigger.id;
  const url = apiBase
    ? `${apiBase}/api/v1/workflows/${workflowId}/webhook/${slug}`
    : "";
  const curl = url
    ? `curl -X POST '${url}' \\
  -H 'Content-Type: application/json' \\
  -d '{"message": "Hi, I have an order issue"}'`
    : "";
  return (
    <div className="space-y-3 rounded-2xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
      <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
        Webhook trigger
      </p>
      <p className="text-xs text-slate-500 dark:text-slate-400">
        External systems fire this workflow by sending an HTTP{" "}
        <code className="rounded bg-slate-100 px-1 py-0.5 text-[11px] dark:bg-slate-800">
          POST
        </code>{" "}
        to the URL below. The JSON body becomes the workflow's input. You can
        also click <strong>Run manually</strong> to fire it from here with the
        payload below.
      </p>
      <div className="flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-700 dark:bg-slate-950">
        <code className="flex-1 truncate text-xs text-slate-700 dark:text-slate-200">
          {url}
        </code>
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard.writeText(url);
            toast.success("Webhook URL copied");
          }}
          className="rounded-lg bg-brand-600 px-3 py-1 text-xs font-medium text-white"
        >
          Copy URL
        </button>
      </div>
      <div className="space-y-1.5">
        <p className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
          Sample request
        </p>
        <div className="flex items-start gap-2 rounded-xl border border-slate-200 bg-slate-950 px-3 py-2.5">
          <pre className="flex-1 overflow-x-auto whitespace-pre text-[11px] leading-relaxed text-slate-100">
{curl}
          </pre>
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard.writeText(curl);
              toast.success("curl command copied");
            }}
            className="shrink-0 rounded-lg border border-slate-700 px-2.5 py-1 text-[11px] font-medium text-slate-200 hover:bg-slate-800"
          >
            Copy
          </button>
        </div>
      </div>
      <div className="space-y-1.5">
        <label className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
          Payload for &ldquo;Run manually&rdquo;
        </label>
        <textarea
          value={inputJson}
          onChange={(e) => setInputJson(e.target.value)}
          rows={6}
          spellCheck={false}
          className="w-full resize-y rounded-xl border border-slate-200 bg-white p-3 font-mono text-xs leading-snug text-slate-800 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
        />
        <p className="text-[11px] text-slate-500 dark:text-slate-400">
          Edit this to simulate the kind of body an external system would POST.
          It becomes the workflow&rsquo;s input on the next manual run.
        </p>
      </div>
      {trigger.secret_required ? (
        <p className="text-xs text-amber-700 dark:text-amber-300">
          A signed{" "}
          <code className="rounded bg-amber-100 px-1 dark:bg-amber-900">
            X-Webhook-Secret
          </code>{" "}
          header is required for this webhook.
        </p>
      ) : null}
    </div>
  );
}

function InfoCard({
  title,
  body,
  children,
}: {
  title: string;
  body: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
      <p className="text-sm font-medium text-slate-700 dark:text-slate-200">{title}</p>
      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{body}</p>
      {children}
    </div>
  );
}

function EmptyCard({
  title,
  body,
  onAdvanced,
}: {
  title: string;
  body: string;
  onAdvanced: () => void;
}) {
  return (
    <div className="rounded-2xl border border-dashed border-slate-300 p-5 text-sm text-slate-600 dark:border-slate-700 dark:text-slate-300">
      <p className="font-medium">{title}</p>
      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{body}</p>
      <button
        type="button"
        onClick={onAdvanced}
        className="mt-2 text-xs text-brand-700 underline"
      >
        Switch to advanced JSON
      </button>
    </div>
  );
}

function findTrigger(defn: WorkflowDefinition | null): TriggerNode | null {
  if (!defn) return null;
  const nodes: WorkflowNode[] = defn.nodes ?? [];
  for (const n of nodes) {
    if (n.kind === "trigger") return n;
  }
  return null;
}

function executeLabel(trigger: TriggerNode | null): string {
  if (!trigger) return "Execute";
  switch (trigger.trigger_type) {
    case "manual":
      return "Run workflow";
    case "form":
      return "Submit & run";
    case "webhook":
      return "Run manually";
    case "schedule":
      return "Run now";
    default:
      return "Execute";
  }
}

function unwrapExecution(raw: Record<string, unknown>): ExecutionEvent {
  return raw as unknown as ExecutionEvent;
}

function hitlFallback(events: ExecutionEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i]!;
    if (e.type === "hitl_required" && e.execution_id) {
      return String(e.execution_id);
    }
  }
  return null;
}
