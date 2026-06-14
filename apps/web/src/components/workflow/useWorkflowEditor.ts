/**
 * Zustand store backing the interactive workflow editor.
 *
 * Holds the in-progress definition, selection, undo/redo history, and
 * derived validation issues. All mutations route through the pure
 * helpers in ``workflow-mutations`` so there's exactly one place that
 * knows how to shape a ``WorkflowDefinition``.
 *
 * The store is intentionally NOT a global — every canvas mount creates
 * its own instance via the ``createWorkflowEditorStore`` factory and
 * passes it down via a React context (see ``EditorContext.tsx``). That
 * keeps two editors on the same page from clobbering each other.
 */

import { createStore } from "zustand";

import type { WorkflowDefinition, WorkflowNode } from "@/types/api";

import {
  addNode,
  connectNodes,
  disconnectNodes,
  patchNode,
  removeNode,
  renameNodeId,
  validateDefinition,
  type ValidationIssue,
} from "./workflow-mutations";

const MAX_HISTORY = 50;

export interface EditorState {
  /** Current definition. Always validated by Pydantic on save. */
  definition: WorkflowDefinition;
  /** Selected node id (single-select). */
  selectedId: string | null;
  /** Stack of prior definitions for undo. Caps at ``MAX_HISTORY`` entries. */
  past: WorkflowDefinition[];
  /** Stack of definitions ahead of the current one for redo. */
  future: WorkflowDefinition[];
  /** ``true`` after any mutation; cleared after a successful save. */
  isDirty: boolean;
  /** Cached validation issues recomputed on every mutation. */
  issues: ValidationIssue[];
}

export interface EditorActions {
  /** Replace the definition wholesale (used on initial load). Clears history
   *  and the dirty flag. */
  reset: (defn: WorkflowDefinition) => void;
  /** Apply an externally-proposed definition (an accepted AI refinement) as a
   *  single undoable, dirty-marking edit — unlike ``reset`` it keeps history
   *  and forces a Save. */
  applyProposed: (defn: WorkflowDefinition) => void;
  /** Mark the current state as saved. Clears ``isDirty`` without touching history. */
  markSaved: () => void;
  selectNode: (id: string | null) => void;
  addNode: (node: WorkflowNode) => void;
  removeNode: (id: string) => void;
  connect: (source: string, target: string) => void;
  disconnect: (source: string, target: string) => void;
  patchNode: (id: string, patch: Partial<WorkflowNode>) => void;
  renameNodeId: (oldId: string, newId: string) => void;
  patchMeta: (patch: Partial<Pick<
    WorkflowDefinition,
    "name" | "description" | "trigger" | "output_format" | "human_checkpoints"
  >>) => void;
  /** Update the definition's name WITHOUT marking dirty or pushing history —
   *  used after a name-only rename that's already been persisted server-side. */
  renameInPlace: (name: string) => void;
  undo: () => void;
  redo: () => void;
}

export type EditorStore = ReturnType<typeof createWorkflowEditorStore>;

export function createWorkflowEditorStore(initial: WorkflowDefinition) {
  return createStore<EditorState & EditorActions>((set, get) => {
    function mutate(
      transform: (defn: WorkflowDefinition) => WorkflowDefinition,
    ): void {
      const current = get().definition;
      const next = transform(current);
      if (next === current) return; // no-op mutation
      const past = [...get().past, current].slice(-MAX_HISTORY);
      set({
        definition: next,
        past,
        future: [],
        isDirty: true,
        issues: validateDefinition(next),
      });
    }

    return {
      definition: initial,
      selectedId: null,
      past: [],
      future: [],
      isDirty: false,
      issues: validateDefinition(initial),

      reset: (defn) =>
        set({
          definition: defn,
          past: [],
          future: [],
          selectedId: null,
          isDirty: false,
          issues: validateDefinition(defn),
        }),

      applyProposed: (defn) => {
        const current = get().definition;
        if (defn === current) return;
        const past = [...get().past, current].slice(-MAX_HISTORY);
        set({
          definition: defn,
          past,
          future: [],
          selectedId: null,
          isDirty: true,
          issues: validateDefinition(defn),
        });
      },

      markSaved: () => set({ isDirty: false }),

      selectNode: (id) => set({ selectedId: id }),

      addNode: (node) => {
        mutate((d) => addNode(d, node));
        set({ selectedId: node.id });
      },

      removeNode: (id) => {
        mutate((d) => removeNode(d, id));
        if (get().selectedId === id) set({ selectedId: null });
      },

      connect: (source, target) => mutate((d) => connectNodes(d, source, target)),

      disconnect: (source, target) =>
        mutate((d) => disconnectNodes(d, source, target)),

      patchNode: (id, patch) =>
        mutate((d) => patchNode(d, id, patch as Partial<WorkflowNode>)),

      renameNodeId: (oldId, newId) => {
        mutate((d) => renameNodeId(d, oldId, newId));
        if (get().selectedId === oldId) set({ selectedId: newId });
      },

      patchMeta: (patch) =>
        mutate((d) => ({ ...d, ...patch })),

      renameInPlace: (name) =>
        set((s) => ({ definition: { ...s.definition, name } })),

      undo: () => {
        const { past, future, definition } = get();
        if (past.length === 0) return;
        const prev = past[past.length - 1];
        set({
          definition: prev,
          past: past.slice(0, -1),
          future: [definition, ...future].slice(0, MAX_HISTORY),
          isDirty: true,
          issues: validateDefinition(prev),
        });
      },

      redo: () => {
        const { past, future, definition } = get();
        if (future.length === 0) return;
        const next = future[0];
        set({
          definition: next,
          past: [...past, definition].slice(-MAX_HISTORY),
          future: future.slice(1),
          isDirty: true,
          issues: validateDefinition(next),
        });
      },
    };
  });
}
