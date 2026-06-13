import { create } from "zustand";

export type ChatRole = "user" | "assistant" | "system";

export interface CitationChip {
  index: number;
  documentTitle: string;
  pageNumber: number;
  documentId: string;
}

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: number;
  /** Model response metadata */
  citations?: CitationChip[];
  /** While assistant message is streaming/revealing */
  partial?: boolean;
}

export interface ChatSession {
  id: string;
  title: string;
  workspaceId: string;
  messages: ChatMessage[];
  updatedAt: number;
}

type ChatState = {
  sessions: ChatSession[];
  currentSessionId: string | null;
  createSession: (workspaceId: string, title?: string) => string;
  setCurrentSession: (id: string | null) => void;
  addMessage: (
    sessionId: string,
    message: Omit<ChatMessage, "id" | "createdAt">,
  ) => string;
  patchMessage: (
    sessionId: string,
    messageId: string,
    patch: Partial<ChatMessage>,
  ) => void;
};

export const useChatStore = create<ChatState>((set, get) => ({
  sessions: [],
  currentSessionId: null,

  createSession(workspaceId, title = "New conversation") {
    const id = crypto.randomUUID();
    const session: ChatSession = {
      id,
      title,
      workspaceId,
      messages: [],
      updatedAt: Date.now(),
    };
    set({ sessions: [session, ...get().sessions], currentSessionId: id });
    return id;
  },

  setCurrentSession(id) {
    set({ currentSessionId: id });
  },

  addMessage(sessionId, msg) {
    const mid = crypto.randomUUID();
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id !== sessionId ?
          s
        : {
            ...s,
            messages: [
              ...s.messages,
              {
                ...msg,
                id: mid,
                createdAt: Date.now(),
              },
            ],
            updatedAt: Date.now(),
            title:
              s.messages.length === 0 && msg.role === "user" ?
                msg.content.slice(0, 80)
              : s.title,
          },
      ),
    }));
    return mid;
  },

  patchMessage(sessionId, messageId, patch) {
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id !== sessionId ?
          s
        : {
            ...s,
            messages: s.messages.map((m) =>
              m.id === messageId ? { ...m, ...patch } : m,
            ),
            updatedAt: Date.now(),
          },
      ),
    }));
  },
}));
