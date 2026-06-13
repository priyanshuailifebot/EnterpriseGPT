import { create } from "zustand";

import type {
  CitedAnswerOut,
  DocumentStatusOut,
  DocumentSummaryOut,
} from "@/types/api";

export type UploadJob = {
  file: File;
  documentId: string | null;
  status: string;
  progress: number;
  error?: string;
};

type DocumentState = {
  items: DocumentSummaryOut[];
  uploads: UploadJob[];
  lastQuery: CitedAnswerOut | null;
  isLoadingList: boolean;
  setItems: (items: DocumentSummaryOut[]) => void;
  setLoadingList: (v: boolean) => void;
  addUpload: (job: UploadJob) => void;
  updateUpload: (idx: number, patch: Partial<UploadJob>) => void;
  removeUpload: (idx: number) => void;
  setLastQuery: (a: CitedAnswerOut | null) => void;
  patchDocumentStatus: (docId: string, status: DocumentStatusOut) => void;
};

export const useDocumentStore = create<DocumentState>((set) => ({
  items: [],
  uploads: [],
  lastQuery: null,
  isLoadingList: false,

  setItems(items) {
    set({ items });
  },
  setLoadingList(v) {
    set({ isLoadingList: v });
  },
  addUpload(job) {
    set((s) => ({ uploads: [...s.uploads, job] }));
  },
  updateUpload(idx, patch) {
    set((s) => ({
      uploads: s.uploads.map((u, i) => (i === idx ? { ...u, ...patch } : u)),
    }));
  },
  removeUpload(idx) {
    set((s) => ({ uploads: s.uploads.filter((_, i) => i !== idx) }));
  },
  setLastQuery(a) {
    set({ lastQuery: a });
  },
  patchDocumentStatus(docId, st) {
    set((state) => ({
      items: state.items.map((d) =>
        d.id !== docId ?
          d
        : {
            ...d,
            status: st.status,
            chunk_count: st.chunk_count,
          },
      ),
    }));
  },
}));
