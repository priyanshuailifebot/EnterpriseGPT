"use client";

/**
 * Persistence wiring for an ``InteractiveCanvas`` that edits an
 * ALREADY-SAVED workflow (the ``/workflows/[id]`` detail page).
 *
 * Returns the two callbacks the canvas expects:
 *
 *  - ``onSave``    → ``PUT /workflows/{id}`` (creates a new version) via the
 *                    workflow store, so the library + detail caches stay in
 *                    sync.
 *  - ``onAugment`` → ``POST /workflows/{id}/augment`` and returns the proposed
 *                    graph + change list. It does NOT apply anything — the
 *                    canvas previews the diff and the user Accepts to apply.
 *
 * ``WorkflowBuilder`` (``/workflows/new``) intentionally does NOT use this
 * hook: its first Save must *create* the workflow (POST) before subsequent
 * PUTs — a superset flow this hook deliberately omits to stay simple.
 */

import axios from "axios";
import { useCallback } from "react";
import toast from "react-hot-toast";

import { api, getErrorMessage } from "@/lib/api";
import { useWorkflowStore } from "@/stores/workflowStore";
import type { AugmentResponse, WorkflowDefinition } from "@/types/api";

import type { AugmentInput, AugmentProposal } from "./InteractiveCanvas";

export function useCanvasPersistence(workflowId: string) {
  const updateWorkflow = useWorkflowStore((s) => s.updateWorkflow);

  const onSave = useCallback(
    async (definition: WorkflowDefinition): Promise<void> => {
      await updateWorkflow(workflowId, { definition });
    },
    [updateWorkflow, workflowId],
  );

  const onAugment = useCallback(
    async (input: AugmentInput): Promise<AugmentProposal> => {
      try {
        const { data } = await api.post<AugmentResponse>(
          `/api/v1/workflows/${workflowId}/augment`,
          {
            message: input.message,
            current_definition: input.definition,
            focus_node_id: input.focusNodeId,
          },
        );
        return { proposed: data.proposed_definition, changes: data.changes };
      } catch (e) {
        const msg = axios.isAxiosError(e)
          ? getErrorMessage(e)
          : e instanceof Error
            ? e.message
            : "Refine failed";
        toast.error(msg);
        throw new Error(msg);
      }
    },
    [workflowId],
  );

  return { onSave, onAugment };
}
