"use client";

import axios from "axios";
import * as Tabs from "@radix-ui/react-tabs";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";

import { ClarificationPanel } from "@/components/workflow/ClarificationPanel";
import {
  InteractiveCanvas,
  type AugmentInput,
  type AugmentProposal,
} from "@/components/workflow/InteractiveCanvas";
import { NLInput } from "@/components/workflow/NLInput";
import { VisualEditor } from "@/components/workflow/VisualEditor";
import { WorkflowPreview } from "@/components/workflow/WorkflowPreview";
import { createWorkflowEditorStore } from "@/components/workflow/useWorkflowEditor";
import { api, getErrorMessage } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { useWorkflowStore } from "@/stores/workflowStore";
import type {
  AugmentResponse,
  InterpretRequestPayload,
  InterpretResponse,
  ToolsListResponse,
  WorkflowDefinition,
} from "@/types/api";
import { isNeedsClarification, isReadyResponse } from "@/types/api";

export type BuilderPhase =
  | "idle"
  | "clarifying"
  | "interpreting"
  | "preview"
  | "error";

export type WorkflowBuilderProps = {
  skipClarification?: boolean;
  onWorkflowSaved?: (id: string) => void;
};

export function WorkflowBuilder({
  skipClarification = false,
  onWorkflowSaved,
}: WorkflowBuilderProps) {
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const [tab, setTab] = useState("describe");

  const searchParams = useSearchParams();
  const seededPrompt = searchParams?.get("prompt") ?? "";

  const [phase, setPhase] = useState<BuilderPhase>("idle");
  const [prompt, setPrompt] = useState(seededPrompt);

  // When the user navigates here from the templates gallery the prompt is
  // passed as ``?prompt=...``. ``useState`` snapshots on mount but a router
  // navigation between two ``/workflows/new`` routes with different query
  // strings doesn't remount — re-seed if the param changes.
  useEffect(() => {
    if (seededPrompt && seededPrompt !== prompt) {
      setPrompt(seededPrompt);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seededPrompt]);
  const [definition, setDefinition] = useState<WorkflowDefinition | null>(
    null,
  );
  const [savedWorkflowId, setSavedWorkflowId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);

  const [toolsCatalog, setToolsCatalog] = useState<string[]>([]);

  // One editor-store instance per definition handoff. We rebuild the store
  // when the upstream definition changes (e.g. after re-interpret) so the
  // canvas always starts clean — but we DO NOT rebuild on every render,
  // otherwise undo history would reset on each keystroke.
  const editorStore = useMemo(
    () => (definition ? createWorkflowEditorStore(definition) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [definition],
  );

  const clarificationState = useWorkflowStore((s) => s.clarification);
  const setClarification = useWorkflowStore((s) => s.setClarification);
  const setAnswer = useWorkflowStore((s) => s.setAnswer);
  const clearClarification = useWorkflowStore((s) => s.clearClarification);
  const createWorkflow = useWorkflowStore((s) => s.createWorkflow);

  const reloadTools = useCallback(async () => {
    if (!workspaceId) return;
    try {
      const { data } = await api.get<ToolsListResponse>(
        "/api/v1/integrations/tools",
        {
          params: { workspace_id: workspaceId },
        },
      );
      setToolsCatalog(data.tools.map((t) => t.name));
    } catch {
      toast.error("Could not load tool catalog.");
    }
  }, [workspaceId]);

  const applyClarificationFromResponse = useCallback(
    (data: Extract<InterpretResponse, { status: "needs_clarification" }>) => {
      const pending: Record<string, string | string[]> = {};
      for (const q of data.questions) {
        if (q.type === "multi_choice") pending[q.id] = [];
        else if (q.type === "choice" && q.options?.length)
          pending[q.id] = q.options[0];
        else pending[q.id] = "";
      }
      setClarification({
        sessionId: data.session_id,
        questions: data.questions,
        pendingAnswers: pending,
        roundNumber: data.round_number,
        originalPrompt: data.original_prompt,
      });
      setPhase("clarifying");
    },
    [setClarification],
  );

  const interpret = useCallback(
    async (body: InterpretRequestPayload) => {
      if (!workspaceId) {
        toast.error("Select a workspace first.");
        return;
      }
      setBusy(true);
      setPhase("interpreting");
      try {
        const { data } = await api.post<InterpretResponse>(
          "/api/v1/workflows/interpret",
          {
            ...body,
            workspace_id: body.workspace_id ?? workspaceId,
          },
        );
        if (isNeedsClarification(data)) {
          applyClarificationFromResponse(data);
          return;
        }
        if (isReadyResponse(data)) {
          clearClarification();
          setDefinition(data.definition);
          setPhase("preview");
          setTab("describe");
          void reloadTools();
        }
      } catch (e: unknown) {
        // Keep the user in the clarifying phase if they were already
        // answering questions, so a flaky LLM round-trip doesn't wipe
        // out their typed answers. Otherwise fall back to the error
        // state which re-shows the NL input.
        setPhase((prev) => (prev === "clarifying" ? "clarifying" : "error"));

        const message =
          axios.isAxiosError(e) ?
            e.code === "ECONNABORTED" || /timeout/i.test(e.message) ?
              "The LLM took too long to respond. Please try again."
            : !e.response ?
              "Could not reach the API (network error). Please try again."
            : getErrorMessage(e)
          : "Could not interpret workflow.";
        toast.error(message);
      } finally {
        setBusy(false);
      }
    },
    [
      applyClarificationFromResponse,
      clearClarification,
      reloadTools,
      workspaceId,
    ],
  );

  const onGenerate = useCallback(() => {
    const text = prompt.trim();
    if (!text) {
      toast.error("Enter a workflow description.");
      return;
    }
    void interpret({
      text,
      workspace_id: workspaceId ?? "",
      skip_clarification: skipClarification,
      answers: [],
    });
  }, [interpret, prompt, skipClarification, workspaceId]);

  const buildAnswersPayload = useMemo(() => {
    if (!clarificationState) return [];
    return clarificationState.questions.map((q) => ({
      question_id: q.id,
      answer: clarificationState.pendingAnswers[q.id] ?? "",
    }));
  }, [clarificationState]);

  const submitClarification = useCallback(() => {
    if (!clarificationState) return;
    void interpret({
      session_id: clarificationState.sessionId,
      answers: buildAnswersPayload,
    });
  }, [buildAnswersPayload, clarificationState, interpret]);

  const onForceProceed = useCallback(() => {
    if (!clarificationState) return;
    void interpret({
      session_id: clarificationState.sessionId,
      answers: buildAnswersPayload,
      force_proceed: true,
    });
  }, [buildAnswersPayload, clarificationState, interpret]);

  const onUseDefaults = useCallback(() => {
    if (!clarificationState) return;
    for (const q of clarificationState.questions) {
      if (q.type === "multi_choice") {
        setAnswer(q.id, q.options?.slice(0, 1) ?? []);
      } else if (q.type === "choice") {
        setAnswer(q.id, q.options?.[0] ?? "");
      } else {
        setAnswer(q.id, "Not specified");
      }
    }
  }, [clarificationState, setAnswer]);

  const resetBuilder = useCallback(() => {
    clearClarification();
    setDefinition(null);
    setPhase("idle");
    setPrompt("");
  }, [clearClarification]);

  const saveWorkflow = useCallback(async (): Promise<void> => {
    if (!workspaceId || !definition) throw new Error("Missing workspace/graph");
    setSaving(true);
    try {
      const row = await createWorkflow({
        workspace_id: workspaceId,
        definition,
      });
      toast.success("Workflow saved");
      onWorkflowSaved?.(row.id);
    } finally {
      setSaving(false);
    }
  }, [createWorkflow, definition, onWorkflowSaved, workspaceId]);

  /**
   * Persist edits made in the interactive canvas. First save → POST
   * /workflows; subsequent saves on the same canvas session → PUT
   * /workflows/{id} (creating a new version).
   */
  const persistCanvasDefinition = useCallback(
    async (next: WorkflowDefinition): Promise<void> => {
      if (!workspaceId) throw new Error("Missing workspace");
      if (savedWorkflowId) {
        await api.put(`/api/v1/workflows/${savedWorkflowId}`, {
          definition: next,
        });
        setDefinition(next);
        return;
      }
      const row = await createWorkflow({
        workspace_id: workspaceId,
        definition: next,
      });
      setSavedWorkflowId(row.id);
      setDefinition(next);
      onWorkflowSaved?.(row.id);
    },
    [createWorkflow, onWorkflowSaved, savedWorkflowId, workspaceId],
  );

  /**
   * Call POST /workflows/{id}/augment with the current definition + an NL
   * instruction and return the proposed graph + change list. The canvas
   * previews the diff and applies it only when the user Accepts.
   */
  const refineWithAI = useCallback(
    async (input: AugmentInput): Promise<AugmentProposal> => {
      if (!savedWorkflowId) {
        toast.error("Save the workflow at least once before refining.");
        throw new Error("not saved");
      }
      try {
        const { data } = await api.post<AugmentResponse>(
          `/api/v1/workflows/${savedWorkflowId}/augment`,
          {
            message: input.message,
            current_definition: input.definition,
            focus_node_id: input.focusNodeId,
          },
        );
        return { proposed: data.proposed_definition, changes: data.changes };
      } catch (e) {
        const msg =
          axios.isAxiosError(e)
            ? getErrorMessage(e)
            : e instanceof Error
              ? e.message
              : "Refine failed";
        toast.error(msg);
        throw new Error(msg);
      }
    },
    [savedWorkflowId],
  );

  if (!workspaceId) {
    return (
      <div className="rounded-2xl border border-dashed border-slate-300 p-10 text-center text-sm text-slate-600 dark:border-slate-700">
        Pick a workspace in the header to design workflows for that tenant scope.
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-[1600px] space-y-10">
      {phase !== "idle" ?
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={resetBuilder}
            className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs dark:border-slate-700"
          >
            Start over
          </button>
        </div>
      : null}

      {phase === "interpreting" ? (
        <div className="animate-pulse text-sm text-slate-600 dark:text-slate-400">
          Interpreting your workflow with Dynamiq + LangGraph…
        </div>
      ) : null}

      {phase === "clarifying" && clarificationState ?
        <>
          <ClarificationPanel
            questions={clarificationState.questions}
            roundNumber={clarificationState.roundNumber}
            originalPrompt={clarificationState.originalPrompt}
            pendingAnswers={clarificationState.pendingAnswers}
            onAnswerChange={setAnswer}
            onSubmit={() => submitClarification()}
            onUseDefaults={onUseDefaults}
            isSubmitting={busy}
          />
          <button
            type="button"
            onClick={onForceProceed}
            className="text-xs text-slate-500 underline hover:text-slate-700 dark:hover:text-slate-300"
          >
            Skip remaining clarification and proceed
          </button>
        </>
      : null}

      {phase !== "preview" || !definition ? (
        <>
          {phase !== "clarifying" ? (
            <NLInput
              value={prompt}
              onChange={setPrompt}
              onGenerate={() => void onGenerate()}
              loading={busy}
            />
          ) : null}
        </>
      ) : (
        <Tabs.Root value={tab} onValueChange={setTab}>
          <Tabs.List className="mb-6 flex gap-2 border-b border-slate-200 pb-2 dark:border-slate-800">
            <Tabs.Trigger
              value="describe"
              className="rounded-lg px-4 py-2 text-sm font-medium text-slate-600 data-[state=active]:bg-brand-600 data-[state=active]:text-white dark:text-slate-300"
            >
              Describe
            </Tabs.Trigger>
            <Tabs.Trigger
              value="visual"
              className="rounded-lg px-4 py-2 text-sm font-medium text-slate-600 data-[state=active]:bg-brand-600 data-[state=active]:text-white dark:text-slate-300"
            >
              Visual
            </Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="describe">
            <WorkflowPreview
              definition={definition}
              toolsCatalog={toolsCatalog}
              saving={saving}
              onDefinitionChange={setDefinition}
              onSaved={saveWorkflow}
            />
          </Tabs.Content>
          <Tabs.Content value="visual" className="space-y-4">
            <p className="text-sm text-slate-600 dark:text-slate-400">
              Drag nodes from the palette, click to edit properties, drag handles
              to connect. Save the workflow to persist; once saved, “Refine with AI”
              applies natural-language edits to the graph.
            </p>
            {editorStore ? (
              <InteractiveCanvas
                store={editorStore}
                workflowId={savedWorkflowId}
                onSave={persistCanvasDefinition}
                onAugment={savedWorkflowId ? refineWithAI : undefined}
              />
            ) : null}
            <div className="flex gap-3">
              <button
                type="button"
                className="rounded-xl border px-5 py-2 text-sm dark:border-slate-700"
                onClick={() => void reloadTools()}
              >
                Reload tools catalog
              </button>
            </div>
          </Tabs.Content>
        </Tabs.Root>
      )}
    </div>
  );
}
