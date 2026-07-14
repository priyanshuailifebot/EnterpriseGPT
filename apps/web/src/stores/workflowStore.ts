import { create } from "zustand";

import { api } from "@/lib/api";
import type {
  ClarificationQuestion,
  SelfHealConfig,
  WorkflowCreateBody,
  WorkflowDefinition,
  WorkflowDetailOut,
  WorkflowListOut,
  WorkflowSummaryOut,
  WorkflowUpdateBody,
} from "@/types/api";

export type ClarificationState = {
  sessionId: string;
  questions: ClarificationQuestion[];
  pendingAnswers: Record<string, string | string[]>;
  roundNumber: number;
  originalPrompt: string;
} | null;

type WorkflowStore = {
  clarification: ClarificationState;
  workflows: WorkflowSummaryOut[];
  currentWorkflow: WorkflowDetailOut | null;
  isLoading: boolean;
  setClarification: (c: ClarificationState) => void;
  setAnswer: (questionId: string, answer: string | string[]) => void;
  clearClarification: () => void;
  fetchWorkflows: (workspaceId: string) => Promise<void>;
  fetchWorkflowDetail: (id: string) => Promise<void>;
  createWorkflow: (body: WorkflowCreateBody) => Promise<WorkflowSummaryOut>;
  updateWorkflow: (id: string, body: WorkflowUpdateBody) => Promise<WorkflowSummaryOut>;
  renameWorkflow: (id: string, name: string) => Promise<WorkflowSummaryOut>;
  publishWorkflow: (id: string) => Promise<WorkflowSummaryOut>;
  unpublishWorkflow: (id: string) => Promise<WorkflowSummaryOut>;
  setSelfHeal: (id: string, config: SelfHealConfig) => Promise<WorkflowSummaryOut>;
  setCurrentWorkflow: (d: WorkflowDetailOut | null) => void;
};

export const useWorkflowStore = create<WorkflowStore>((set, get) => ({
  clarification: null,
  workflows: [],
  currentWorkflow: null,
  isLoading: false,

  setClarification: (c) => set({ clarification: c }),
  setAnswer: (questionId, answer) =>
    set((s) => {
      if (!s.clarification) return s;
      return {
        clarification: {
          ...s.clarification,
          pendingAnswers: {
            ...s.clarification.pendingAnswers,
            [questionId]: answer,
          },
        },
      };
    }),
  clearClarification: () => set({ clarification: null }),

  async fetchWorkflows(workspaceId) {
    set({ isLoading: true });
    try {
      const { data } = await api.get<WorkflowListOut>("/api/v1/workflows/", {
        params: { workspace_id: workspaceId, page_size: 100 },
      });
      set({ workflows: data.items });
    } finally {
      set({ isLoading: false });
    }
  },

  async fetchWorkflowDetail(id) {
    set({ isLoading: true });
    try {
      const { data } = await api.get<WorkflowDetailOut>(`/api/v1/workflows/${id}`);
      set({ currentWorkflow: data });
    } finally {
      set({ isLoading: false });
    }
  },

  async createWorkflow(body) {
    const { data } = await api.post<WorkflowSummaryOut>("/api/v1/workflows/", body);
    const list = get().workflows;
    set({ workflows: [data, ...list.filter((w) => w.id !== data.id)] });
    return data;
  },

  async updateWorkflow(id, body) {
    const { data } = await api.put<WorkflowSummaryOut>(
      `/api/v1/workflows/${id}`,
      body,
    );
    set((s) => ({
      workflows: s.workflows.map((w) => (w.id === id ? data : w)),
      currentWorkflow:
        s.currentWorkflow?.workflow.id === id ?
          {
            ...s.currentWorkflow,
            workflow: data,
            versions: s.currentWorkflow.versions,
          }
        : s.currentWorkflow,
    }));
    return data;
  },

  async renameWorkflow(id, name) {
    const { data } = await api.patch<WorkflowSummaryOut>(
      `/api/v1/workflows/${id}`,
      { name },
    );
    set((s) => {
      const cur = s.currentWorkflow;
      const sameWf = cur?.workflow.id === id;
      const latestVer = sameWf
        ? Math.max(...cur!.versions.map((v) => v.version), 0)
        : 0;
      return {
        workflows: s.workflows.map((w) => (w.id === id ? data : w)),
        currentWorkflow: sameWf
          ? {
              ...cur!,
              workflow: data,
              // keep the latest version's definition name in sync so the editor
              // re-seeds with the new name (rename creates no new version).
              versions: cur!.versions.map((v) =>
                v.version === latestVer
                  ? { ...v, definition: { ...v.definition, name } }
                  : v,
              ),
            }
          : cur,
      };
    });
    return data;
  },

  async publishWorkflow(id) {
    const { data } = await api.post<WorkflowSummaryOut>(
      `/api/v1/workflows/${id}/publish`,
    );
    set((s) => ({
      workflows: s.workflows.map((w) => (w.id === id ? data : w)),
      currentWorkflow:
        s.currentWorkflow?.workflow.id === id ?
          { ...s.currentWorkflow, workflow: data }
        : s.currentWorkflow,
    }));
    return data;
  },

  async unpublishWorkflow(id) {
    const { data } = await api.post<WorkflowSummaryOut>(
      `/api/v1/workflows/${id}/unpublish`,
    );
    set((s) => ({
      workflows: s.workflows.map((w) => (w.id === id ? data : w)),
      currentWorkflow:
        s.currentWorkflow?.workflow.id === id ?
          { ...s.currentWorkflow, workflow: data }
        : s.currentWorkflow,
    }));
    return data;
  },

  async setSelfHeal(id, config) {
    const { data } = await api.put<WorkflowSummaryOut>(
      `/api/v1/workflows/${id}/self-heal`,
      config,
    );
    set((s) => ({
      workflows: s.workflows.map((w) => (w.id === id ? data : w)),
      currentWorkflow:
        s.currentWorkflow?.workflow.id === id ?
          { ...s.currentWorkflow, workflow: data }
        : s.currentWorkflow,
    }));
    return data;
  },

  setCurrentWorkflow: (d) => set({ currentWorkflow: d }),
}));

export function draftDefinitionFromDetail(
  detail: WorkflowDetailOut | null,
): WorkflowDefinition | null {
  if (!detail?.versions?.length) return null;
  const latest = [...detail.versions].sort((a, b) => b.version - a.version)[0];
  return latest?.definition ?? null;
}
