"use client";

/**
 * Slide-out panel that runs the workflow as a demo (or for real) and
 * streams live SSE results back into the canvas overlay.
 *
 * Auto-detects the trigger kind and renders an appropriate input form:
 *   - chat       → single ``message`` textarea
 *   - form       → key/value fields built from ``form_fields``
 *   - webhook    → JSON textarea pre-filled with a sample envelope
 *   - schedule   → no-input notice; runs with empty payload
 *   - manual     → free-form JSON textarea
 *
 * The "Demo mode" toggle defaults to ``true``. When on, the backend
 * uses the mocked executor (no LLM, no integrations). When off, the
 * real Dynamiq executor runs and requires configured credentials.
 */

import * as Dialog from "@radix-ui/react-dialog";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  PlayCircle,
  StopCircle,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  ExecutionEvent,
  SampleInputResponse,
  TriggerNode,
  WorkflowDefinition,
} from "@/types/api";

import { useExecutionStream } from "./useExecutionStream";
import { allNodes } from "./workflow-mutations";
import type { ExecutionRunState } from "./execution-status";

export interface TestRunPanelProps {
  open: boolean;
  onClose: () => void;
  workflowId: string | null;
  definition: WorkflowDefinition;
  /** Called every time the execution state changes so the canvas can
   *  overlay per-node status badges. */
  onExecutionState?: (state: ExecutionRunState) => void;
}

export function TestRunPanel({
  open,
  onClose,
  workflowId,
  definition,
  onExecutionState,
}: TestRunPanelProps) {
  const trigger = useMemo(
    () =>
      allNodes(definition).find(
        (n) => n.kind === "trigger",
      ) as TriggerNode | undefined,
    [definition],
  );

  // How the test run should collect input:
  //   chat   → message textarea
  //   form   → field-by-field form
  //   manual → friendly free-text "what starts the run" field
  //   auto   → no prompt; the workflow pulls its own data (a sheet/db read
  //            right after the trigger, or a schedule/webhook trigger)
  const inputMode = useMemo(
    () => computeInputMode(definition, trigger),
    [definition, trigger],
  );

  const [demo, setDemo] = useState(true);
  const [useRealLlm, setUseRealLlm] = useState(false);
  const [inputData, setInputData] = useState<Record<string, unknown>>({});
  const [rawJson, setRawJson] = useState("{}");
  const [rawJsonError, setRawJsonError] = useState<string | null>(null);
  const [branchOverrides, setBranchOverrides] = useState<Record<string, string>>({});

  // Condition / if nodes the tester can force down a chosen branch to exercise
  // a specific path (e.g. the "new customer" route).
  const branchNodes = useMemo(
    () =>
      allNodes(definition)
        .filter((n) => n.kind === "condition" || n.kind === "if")
        .map((n) => ({
          id: (n as { id: string }).id,
          name: (n as { name?: string }).name ?? (n as { id: string }).id,
          branches:
            n.kind === "if"
              ? ["true", "false"]
              : ((n as { branches?: string[] }).branches ?? []),
        })),
    [definition],
  );

  // Initialise inputData from the backend sample_input endpoint when the
  // panel opens. Falls back to a synthesized stub if the workflow isn't
  // saved yet (no id).
  useEffect(() => {
    if (!open) return;
    let cancelled = false;

    async function load() {
      if (workflowId) {
        try {
          const { data } = await api.get<SampleInputResponse>(
            `/api/v1/workflows/${workflowId}/sample_input`,
          );
          if (!cancelled) {
            setInputData(data.input_data ?? {});
            setRawJson(JSON.stringify(data.input_data ?? {}, null, 2));
            setRawJsonError(null);
          }
          return;
        } catch {
          // fall through to local stub
        }
      }
      const stub = localStub(trigger);
      if (!cancelled) {
        setInputData(stub);
        setRawJson(JSON.stringify(stub, null, 2));
        setRawJsonError(null);
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [open, workflowId, trigger]);

  const stream = useExecutionStream({ workflowId: workflowId ?? "" });

  // Conversational transcript (chat/manual triggers): the typed message becomes
  // a user bubble and the workflow's reply comes back as an agent bubble, while
  // the canvas behind the panel lights up node-by-node.
  const conversational = inputMode === "chat" || inputMode === "manual";
  const [transcript, setTranscript] = useState<
    { role: "user" | "agent"; text: string }[]
  >([]);
  const pendingReplyRef = useRef(false);

  // Propagate execution state up so the canvas can update overlays.
  useEffect(() => {
    onExecutionState?.(stream.state);
  }, [stream.state, onExecutionState]);

  // When a run finishes, surface the workflow's reply as an agent bubble.
  useEffect(() => {
    if (!pendingReplyRef.current) return;
    if (stream.state.graphStatus === "complete") {
      pendingReplyRef.current = false;
      const reply = extractReply(stream.events);
      setTranscript((t) => [
        ...t,
        {
          role: "agent",
          text:
            reply ??
            "(Run finished — this workflow has no customer-facing reply node.)",
        },
      ]);
    } else if (stream.state.graphStatus === "error") {
      pendingReplyRef.current = false;
      setTranscript((t) => [
        ...t,
        { role: "agent", text: `⚠️ ${stream.state.errorMessage ?? "run failed"}` },
      ]);
    }
  }, [stream.state.graphStatus, stream.events, stream.state.errorMessage]);

  // Reset state whenever the panel reopens.
  useEffect(() => {
    if (open) {
      stream.reset();
      setTranscript([]);
      pendingReplyRef.current = false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  function handleRun() {
    if (!workflowId) return;
    const userMsg = conversational
      ? String(
          (inputMode === "chat" ? inputData.message : inputData.input) ?? "",
        ).trim()
      : "";
    if (conversational && userMsg) {
      setTranscript((t) => [...t, { role: "user", text: userMsg }]);
      pendingReplyRef.current = true;
    }
    void stream.start({ inputData, demo, useRealLlm, branchOverrides });
    // Clear the compose box so the next message can be typed (multi-turn demo).
    if (conversational) {
      setInputData((d) => ({ ...d, message: "", input: "" }));
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30 backdrop-blur-sm" />
        <Dialog.Content
          className={cn(
            "fixed right-0 top-0 z-50 flex h-full w-[480px] flex-col gap-3 border-l border-slate-200 bg-white p-5 shadow-2xl",
            "dark:border-slate-800 dark:bg-slate-950",
          )}
        >
          <header className="flex items-start justify-between gap-2">
            <div>
              <Dialog.Title className="flex items-center gap-2 text-sm font-semibold text-slate-900 dark:text-slate-100">
                <PlayCircle className="h-4 w-4 text-brand-600" />
                Test workflow
              </Dialog.Title>
              <Dialog.Description className="text-[11px] text-slate-500 dark:text-slate-400">
                {workflowId
                  ? "Stream a run through the saved graph. Demo mode bypasses LLM + integration calls."
                  : "Save the workflow at least once before running a test."}
              </Dialog.Description>
            </div>
            <Dialog.Close className="rounded-md p-1 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800">
              <X className="h-4 w-4" />
            </Dialog.Close>
          </header>

          {/* Demo toggle */}
          <label className="flex items-center gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-800 dark:bg-slate-900">
            <input
              type="checkbox"
              checked={demo}
              onChange={(e) => setDemo(e.target.checked)}
              className="h-3.5 w-3.5"
            />
            <div className="flex-1">
              <p className="text-[12px] font-semibold text-slate-900 dark:text-slate-100">
                Demo mode
              </p>
              <p className="text-[10px] leading-tight text-slate-500 dark:text-slate-400">
                Bypass external integrations; every action returns a dry-run stub.
                {demo ? null : " Off → production run (real LLM + real integrations + DB row)."}
              </p>
            </div>
          </label>
          {/* Real-LLM toggle — only meaningful when demo=true */}
          <label
            className={cn(
              "flex items-center gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-800 dark:bg-slate-900",
              !demo && "opacity-50",
            )}
            title={!demo ? "Production runs always use the real LLM." : ""}
          >
            <input
              type="checkbox"
              checked={demo ? useRealLlm : true}
              disabled={!demo}
              onChange={(e) => setUseRealLlm(e.target.checked)}
              className="h-3.5 w-3.5"
            />
            <div className="flex-1">
              <p className="text-[12px] font-semibold text-slate-900 dark:text-slate-100">
                Use real LLM for agents
              </p>
              <p className="text-[10px] leading-tight text-slate-500 dark:text-slate-400">
                {demo
                  ? "Call Azure OpenAI for agent reasoning instead of returning a stub. Integrations stay mocked."
                  : "Always on for production runs."}
              </p>
            </div>
          </label>

          {/* Conversation transcript — chat/manual triggers play like a chat:
              your message, then the workflow's reply. */}
          {conversational && transcript.length > 0 ? (
            <div className="flex max-h-64 flex-col gap-2 overflow-y-auto rounded-md border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900">
              {transcript.map((m, i) => (
                <div
                  key={i}
                  className={cn(
                    "flex",
                    m.role === "user" ? "justify-end" : "justify-start",
                  )}
                >
                  <div
                    className={cn(
                      "max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-[12px] leading-relaxed",
                      m.role === "user"
                        ? "bg-brand-600 text-white"
                        : "bg-white text-slate-800 shadow-sm dark:bg-slate-800 dark:text-slate-100",
                    )}
                  >
                    {m.text}
                  </div>
                </div>
              ))}
              {stream.isRunning && pendingReplyRef.current ? (
                <div className="flex justify-start">
                  <div className="rounded-2xl bg-white px-3 py-2 text-[11px] text-slate-400 shadow-sm dark:bg-slate-800">
                    agent is working…
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}

          {/* Trigger-aware input form */}
          <section className="flex flex-1 flex-col gap-2 overflow-y-auto">
            <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
              {inputMode === "auto"
                ? "Input source"
                : conversational
                  ? "Your message"
                  : "Trigger input"}
              <span className="ml-1 normal-case opacity-60">
                ({trigger?.trigger_type ?? "manual"})
              </span>
            </p>

            {inputMode === "chat" ? (
              <ChatInput
                value={(inputData.message as string) ?? ""}
                onChange={(v) => setInputData({ message: v })}
              />
            ) : inputMode === "form" ? (
              <FormInput
                fields={trigger?.form_fields ?? []}
                value={inputData}
                onChange={setInputData}
              />
            ) : inputMode === "manual" ? (
              <ManualInput
                value={(inputData.input as string) ?? ""}
                onChange={(v) => setInputData({ ...inputData, input: v })}
              />
            ) : (
              <AutoInput trigger={trigger} />
            )}

            {/* Power-user escape hatch: edit the raw payload. Hidden by
                default so the common case is a clean frontend form. */}
            {inputMode === "manual" || inputMode === "auto" ? (
              <details className="rounded-md border border-slate-200 dark:border-slate-800">
                <summary className="cursor-pointer px-3 py-1.5 text-[11px] font-medium text-slate-500 dark:text-slate-400">
                  Advanced — edit raw JSON payload
                </summary>
                <div className="px-3 pb-3 pt-1">
                  <JsonInput
                    value={rawJson}
                    onChange={(v) => {
                      setRawJson(v);
                      try {
                        const parsed = JSON.parse(v || "{}");
                        setInputData(
                          parsed && typeof parsed === "object" && !Array.isArray(parsed)
                            ? (parsed as Record<string, unknown>)
                            : {},
                        );
                        setRawJsonError(null);
                      } catch (e) {
                        setRawJsonError(
                          e instanceof Error ? e.message : "Invalid JSON",
                        );
                      }
                    }}
                    error={rawJsonError}
                  />
                </div>
              </details>
            ) : null}

            {branchNodes.length > 0 ? (
              <div className="mt-1 flex flex-col gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-800 dark:bg-slate-900">
                <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                  Test path — force branch decisions
                </p>
                <p className="text-[10px] leading-tight text-slate-500 dark:text-slate-400">
                  Pick a branch to drive the run down a specific route (e.g. the
                  new-customer path). &quot;Auto&quot; lets the LLM decide from your input.
                </p>
                {branchNodes.map((b) => (
                  <label key={b.id} className="flex items-center gap-2">
                    <span className="flex-1 truncate text-[11px] text-slate-700 dark:text-slate-200">
                      {b.name}
                    </span>
                    <select
                      value={branchOverrides[b.id] ?? ""}
                      onChange={(e) =>
                        setBranchOverrides((prev) => {
                          const next = { ...prev };
                          if (e.target.value) next[b.id] = e.target.value;
                          else delete next[b.id];
                          return next;
                        })
                      }
                      className="rounded-md border border-slate-300 bg-white px-2 py-1 text-[11px] dark:border-slate-700 dark:bg-slate-900"
                    >
                      <option value="">Auto</option>
                      {b.branches.map((br) => (
                        <option key={br} value={br}>
                          {br}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
            ) : null}
          </section>

          <RunStatusBlock state={stream.state} isRunning={stream.isRunning} />

          <EventLog events={stream.events} />

          <footer className="flex items-center justify-end gap-2">
            {stream.isRunning ? (
              <button
                type="button"
                onClick={stream.stop}
                className="flex items-center gap-1 rounded-md border border-slate-300 px-3 py-1.5 text-[12px] font-semibold text-slate-700 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
              >
                <StopCircle className="h-3.5 w-3.5" /> Stop
              </button>
            ) : null}
            <button
              type="button"
              disabled={!workflowId || stream.isRunning || !!rawJsonError}
              onClick={handleRun}
              className="flex items-center gap-1 rounded-md bg-brand-600 px-3 py-1.5 text-[12px] font-semibold text-white shadow-sm hover:bg-brand-700 disabled:opacity-60"
            >
              {stream.isRunning ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <PlayCircle className="h-3.5 w-3.5" />
              )}
              {stream.isRunning
                ? "Running…"
                : conversational
                  ? transcript.length > 0
                    ? "Send"
                    : "Send message"
                  : "Run test"}
            </button>
          </footer>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

// ---------------------------------------------------------------------------
// Input sub-components
// ---------------------------------------------------------------------------

function ChatInput({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
        Message
      </span>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={4}
        placeholder="Type a sample chat message…"
        className="w-full resize-y rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[12px] text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
      />
    </label>
  );
}

function FormInput({
  fields,
  value,
  onChange,
}: {
  fields: TriggerNode["form_fields"];
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      {fields.map((f, i) => {
        const key = String(f.key ?? `field_${i}`);
        const label = String(f.label ?? key);
        const type = (f.type as string | undefined) ?? "text";
        const options = (f.options as string[] | undefined) ?? [];
        return (
          <label key={key} className="flex flex-col gap-1">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
              {label}
              {f.required ? " *" : ""}
            </span>
            {type === "choice" && options.length > 0 ? (
              <select
                value={String(value[key] ?? options[0])}
                onChange={(e) => onChange({ ...value, [key]: e.target.value })}
                className="w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[12px] dark:border-slate-700 dark:bg-slate-900"
              >
                {options.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            ) : type === "multi_choice" && options.length > 0 ? (
              <div className="flex flex-wrap gap-1">
                {options.map((o) => {
                  const selected = Array.isArray(value[key])
                    ? (value[key] as string[]).includes(o)
                    : false;
                  return (
                    <button
                      key={o}
                      type="button"
                      onClick={() => {
                        const prev = Array.isArray(value[key])
                          ? (value[key] as string[])
                          : [];
                        const next = selected
                          ? prev.filter((x) => x !== o)
                          : [...prev, o];
                        onChange({ ...value, [key]: next });
                      }}
                      className={cn(
                        "rounded-full border px-2 py-0.5 text-[11px]",
                        selected
                          ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-950 dark:text-brand-300"
                          : "border-slate-300 text-slate-600 dark:border-slate-700 dark:text-slate-300",
                      )}
                    >
                      {o}
                    </button>
                  );
                })}
              </div>
            ) : (
              <input
                type="text"
                value={String(value[key] ?? "")}
                onChange={(e) => onChange({ ...value, [key]: e.target.value })}
                className="w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[12px] dark:border-slate-700 dark:bg-slate-900"
              />
            )}
          </label>
        );
      })}
      {fields.length === 0 ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700 dark:border-amber-950 dark:bg-amber-950/40 dark:text-amber-200">
          The form trigger has no fields configured. Edit the trigger node to
          add form fields, or switch to manual mode for a JSON payload.
        </p>
      ) : null}
    </div>
  );
}

function ManualInput({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
        Message / input
      </span>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={4}
        placeholder="Type what starts this run — e.g. a customer message: &quot;Hi, my order #4521 never arrived.&quot;"
        className="w-full resize-y rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[12px] text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
      />
      <span className="text-[10px] text-slate-500 dark:text-slate-400">
        This is the input that kicks off the run — exactly what a real
        inbound message would be.
      </span>
    </label>
  );
}

function AutoInput({ trigger }: { trigger: TriggerNode | undefined }) {
  const tt = trigger?.trigger_type;
  const reason =
    tt === "schedule"
      ? "runs automatically on its schedule"
      : tt === "webhook"
        ? "is triggered by an incoming webhook event"
        : "pulls its data automatically from a connected source (e.g. a Google Sheet)";
  return (
    <div className="rounded-md border border-blue-200 bg-blue-50 px-3 py-3 text-[11px] text-blue-800 dark:border-blue-900 dark:bg-blue-950/40 dark:text-blue-200">
      <p className="font-semibold">No input needed</p>
      <p className="mt-1 leading-relaxed">
        This workflow {reason}. Just press <strong>Run test</strong> — the
        run will fetch its own data and you&apos;ll see each node light up
        with the results.
      </p>
    </div>
  );
}

function JsonInput({
  value,
  onChange,
  error,
}: {
  value: string;
  onChange: (v: string) => void;
  error: string | null;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
        JSON payload
      </span>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        spellCheck={false}
        rows={8}
        className={cn(
          "w-full resize-y rounded-md border border-slate-300 bg-white px-2 py-1.5 font-mono text-[12px] text-slate-900 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100",
          error && "border-rose-500",
        )}
      />
      {error ? (
        <span className="text-[10px] text-rose-600 dark:text-rose-400">
          {error}
        </span>
      ) : null}
    </label>
  );
}

/**
 * Pull the customer-facing reply out of a finished run's event stream.
 *
 * Prefers an explicit respond/reply node's output; falls back to the last
 * agent's text (the resolve/understand agent's response). Returns null when
 * there's nothing customer-facing to show.
 */
export function extractReply(events: ExecutionEvent[]): string | null {
  // A demo dry-run stub ("[demo] gmail.send_email would fire here…") is NOT a
  // customer reply — never surface it as one.
  const isNotice = (s: string) =>
    /would fire here|^\[demo\]|response composed for the customer/i.test(s.trim());

  // 1) The composed reply is the last agent's text (the resolve/respond agent).
  //    A send action just transmits it, so the agent output is the real reply.
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (
      e.type === "agent_complete" &&
      typeof e.content === "string" &&
      e.content.trim() &&
      !isNotice(e.content)
    ) {
      return e.content.trim();
    }
  }

  // 2) Fall back to a respond/reply action that carries a GENUINE body
  //    (not a dry-run notice / generic placeholder).
  const replyish = /respond|reply|send_response|send_message|notify|send_email/;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    const slug = String(e.action_slug ?? "").toLowerCase();
    const nid = String(e.node_id ?? e.agent_id ?? "").toLowerCase();
    const isAction =
      e.type === "action_dry_run" ||
      e.type === "action_result" ||
      e.type === "node_complete";
    if (isAction && (replyish.test(slug) || replyish.test(nid))) {
      const out = (e.output_snapshot ??
        (e.result as { data?: unknown } | undefined)?.data ??
        e.result) as Record<string, unknown> | undefined;
      const text =
        (out?.body as string | undefined) ??
        (out?.message as string | undefined) ??
        (out?.summary as string | undefined) ??
        (out?.note as string | undefined);
      if (typeof text === "string" && text.trim() && !isNotice(text)) {
        return text.trim();
      }
    }
  }
  return null;
}

type InputMode = "chat" | "form" | "manual" | "auto";

const _READ_VERBS = ["read", "fetch", "get", "list", "query", "search", "load"];

/** A node that pulls data on its own (a sheet/db read, a read-ish action). */
function isAutoDataSource(node: { kind?: string; action_slug?: string; op?: string }): boolean {
  if (node.kind === "action") {
    const slug = String(node.action_slug ?? "").toLowerCase();
    return _READ_VERBS.some((v) => slug.includes(v));
  }
  if (node.kind === "data_store") {
    return ["read", "query", "get", "list"].includes(String(node.op ?? "").toLowerCase());
  }
  return false;
}

/**
 * Decide how the test run should collect input.
 *
 * chat/form keep their dedicated forms. A schedule/webhook trigger, or a
 * manual trigger whose first step is an automatic data-source read (the ICICI
 * Google-Sheet pattern), needs no manual input → "auto". Everything else is a
 * manual trigger that genuinely needs the user to provide the starting input.
 */
export function computeInputMode(
  definition: WorkflowDefinition,
  trigger: TriggerNode | undefined,
): InputMode {
  const tt = trigger?.trigger_type;
  if (tt === "chat") return "chat";
  if (tt === "form") return "form";
  if (tt === "schedule" || tt === "webhook") return "auto";
  // manual: auto only if the trigger immediately feeds a data-source read.
  const triggerId = trigger?.id;
  if (triggerId) {
    const downstream = allNodes(definition).filter(
      (n) =>
        Array.isArray((n as { depends_on?: string[] }).depends_on) &&
        (n as { depends_on: string[] }).depends_on.includes(triggerId),
    );
    if (downstream.some((n) => isAutoDataSource(n as never))) return "auto";
  }
  return "manual";
}

function localStub(trigger: TriggerNode | undefined): Record<string, unknown> {
  if (!trigger) return {};
  if (trigger.trigger_type === "chat")
    return { message: "Hello from the demo run." };
  if (trigger.trigger_type === "webhook")
    return { event: "demo.webhook", payload: { id: "demo-1", ok: true } };
  if (trigger.trigger_type === "schedule")
    return { scheduled_at: new Date().toISOString() };
  if (trigger.trigger_type === "form") {
    const out: Record<string, unknown> = {};
    for (const f of trigger.form_fields) {
      const key = String(f.key ?? "field");
      const opts = (f.options as string[] | undefined) ?? [];
      out[key] =
        f.type === "choice" && opts.length > 0
          ? opts[0]
          : f.type === "multi_choice" && opts.length > 0
            ? [opts[0]]
            : "Sample value";
    }
    return out;
  }
  return { input: "Sample input for demo run." };
}

// ---------------------------------------------------------------------------
// Status block + event log
// ---------------------------------------------------------------------------

function RunStatusBlock({
  state,
  isRunning,
}: {
  state: ExecutionRunState;
  isRunning: boolean;
}) {
  if (state.graphStatus === "idle" && !isRunning) {
    return (
      <p className="rounded-md border border-dashed border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
        Click <span className="font-semibold">Run test</span> to stream a run.
      </p>
    );
  }
  const icon =
    state.graphStatus === "running"
      ? <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-600" />
      : state.graphStatus === "complete"
        ? <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
        : state.graphStatus === "error"
          ? <AlertCircle className="h-3.5 w-3.5 text-rose-600" />
          : <Loader2 className="h-3.5 w-3.5 text-amber-600" />;
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-md border px-3 py-2 text-[11px]",
        state.graphStatus === "complete"
          ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200"
          : state.graphStatus === "error"
            ? "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200"
            : "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900 dark:bg-blue-950/40 dark:text-blue-200",
      )}
    >
      {icon}
      <span className="font-semibold capitalize">{state.graphStatus.replace(/_/g, " ")}</span>
      <span className="opacity-70">· {state.eventCount} events</span>
      {state.errorMessage ? (
        <span className="ml-auto truncate font-mono opacity-80">
          {state.errorMessage}
        </span>
      ) : null}
    </div>
  );
}

function EventLog({ events }: { events: ExecutionEvent[] }) {
  if (events.length === 0) return null;
  return (
    <details className="rounded-md border border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900">
      <summary className="cursor-pointer px-3 py-1.5 text-[11px] font-semibold text-slate-700 dark:text-slate-200">
        Event log ({events.length})
      </summary>
      <ol className="max-h-44 overflow-y-auto px-3 pb-2 text-[10px] font-mono text-slate-600 dark:text-slate-300">
        {events.slice(-100).map((e, idx) => (
          <li key={idx} className="leading-snug">
            <span className="text-slate-400">{e.type}</span>{" "}
            {e.agent_id ? <span>· {e.agent_id}</span> : null}
            {e.content ? (
              <span className="opacity-70"> — {String(e.content).slice(0, 80)}</span>
            ) : null}
          </li>
        ))}
      </ol>
    </details>
  );
}
