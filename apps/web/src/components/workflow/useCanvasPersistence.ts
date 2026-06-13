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
 *  - ``onAugment`` → ``POST /workflows/{id}/augment`` then swaps the editor
 *                    store's definition for the proposed graph, letting the
 *                    user review and Save (or Undo).
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

import type { EditorStore } from "./useWorkflowEditor";

export function useCanvasPersistence(store: EditorStore, workflowId: string) {
  const updateWorkflow = useWorkflowStore((s) => s.updateWorkflow);

  const onSave = useCallback(
    async (definition: WorkflowDefinition): Promise<void> => {
      await updateWorkflow(workflowId, { definition });
    },
    [updateWorkflow, workflowId],
  );

  const onAugment = useCallback(
    async (message: string): Promise<void> => {
      const current = store.getState().definition;
      try {
        const { data } = await api.post<AugmentResponse>(
          `/api/v1/workflows/${workflowId}/augment`,
          { message, current_definition: current },
        );
        // Replace the working definition with the proposed graph. The user
        // previews it on the canvas and explicitly Saves to persist.
        store.getState().reset(data.proposed_definition);
        if (data.changes.length > 0) {
          toast.success(
            `Applied ${data.changes.length} change${data.changes.length === 1 ? "" : "s"}.`,
          );
        } else {
          toast("No structural changes.");
        }
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
    [store, workflowId],
  );

  return { onSave, onAugment };
}
