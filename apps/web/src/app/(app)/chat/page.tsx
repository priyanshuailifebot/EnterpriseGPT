"use client";

import { Mic } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import Markdown from "react-markdown";

import remarkGfm from "remark-gfm";

import { useVoice } from "@/hooks/useVoice";
import { api } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { useChatStore } from "@/stores/chatStore";
function extractAssistantText(payload: Record<string, unknown>): string {
  const tail = payload.messages_tail;
  if (Array.isArray(tail)) {
    for (let i = tail.length - 1; i >= 0; i--) {
      const m = tail[i];
      if (typeof m !== "object" || !m) continue;
      const msg = m as Record<string, unknown>;
      const data =
        typeof msg.data === "object" && msg.data ?
          (msg.data as Record<string, unknown>)
        : null;
      const type = String(msg.type ?? msg.role ?? data?.type ?? "").toLowerCase();
      const role = String(msg.role ?? data?.role ?? "").toLowerCase();
      const isAssistant =
        type.includes("ai") ||
        type.includes("assistant") ||
        type === "aichatresponse" ||
        role.includes("assistant");
      if (!isAssistant) continue;
      const c = data?.content ?? msg.content;
      if (typeof c === "string" && c.trim()) return c;
      if (Array.isArray(c)) {
        const joined = c
          .map((chunk) => {
            if (typeof chunk === "string") return chunk;
            if (chunk && typeof chunk === "object" && "text" in chunk) {
              const t = (chunk as Record<string, unknown>).text;
              return typeof t === "string" ? t : "";
            }
            return "";
          })
          .join("");
        if (joined.trim()) return joined;
      }
    }
    try {
      return JSON.stringify(tail.slice(-4), null, 2);
    } catch {
      /* fallthrough */
    }
  }
  return JSON.stringify(payload, null, 2).slice(0, 4000);
}

export default function ChatPage() {
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const router = useRouter();
  const search = useSearchParams();
  const sessions = useChatStore((s) => s.sessions);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const createSession = useChatStore((s) => s.createSession);
  const setSession = useChatStore((s) => s.setCurrentSession);
  const addMessage = useChatStore((s) => s.addMessage);
  const patchMessage = useChatStore((s) => s.patchMessage);

  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const voiceHandlers = useMemo(
    () => ({
      onTranscript(text: string) {
        if (text) {
          setDraft((prev) => `${prev ? `${prev} ` : ""}${text}`.trim());
        }
      },
    }),
    [],
  );

  const voice = useVoice(voiceHandlers);

  useEffect(() => {
    const qid = search.get("session");
    if (qid) setSession(qid);
  }, [search, setSession]);

  useEffect(() => {
    if (!workspaceId) return;
    if (!currentSessionId && sessions.length === 0) {
      const id = createSession(workspaceId);
      router.replace(`/chat?session=${id}`);
      return;
    }
    if (currentSessionId && !search.get("session")) {
      router.replace(`/chat?session=${currentSessionId}`);
    }
  }, [
    createSession,
    currentSessionId,
    router,
    search,
    sessions.length,
    workspaceId,
  ]);

  async function send() {
    if (!workspaceId || !currentSessionId) return;
    const content = draft.trim();
    if (!content) return;
    setDraft("");
    addMessage(currentSessionId, { role: "user", content });
    const assistantId = addMessage(currentSessionId, {
      role: "assistant",
      content: "",
      partial: true,
    });
    setBusy(true);
    try {
      const { data } = await api.post<Record<string, unknown>>(
        `/api/v1/dialog/sessions/${currentSessionId}/turn`,
        { message: content, workspace_id: workspaceId },
      );
      const body = extractAssistantText(data);
      patchMessage(currentSessionId, assistantId, {
        content: body,
        partial: false,
      });
    } catch {
      patchMessage(currentSessionId, assistantId, {
        content:
          "Dialog turn failed. Verify API availability and LANGGRAPH checkpoints.",
        partial: false,
      });
    } finally {
      setBusy(false);
    }
  }

  const session = sessions.find((s) => s.id === currentSessionId);

  if (!workspaceId) {
    return (
      <p className="text-sm text-slate-600 dark:text-slate-400">
        Select a workspace in the shell before chatting.
      </p>
    );
  }

  return (
    <div className="mx-auto grid max-h-[calc(100vh-140px)] min-h-[480px] max-w-6xl gap-4 lg:grid-cols-[260px,minmax(0,1fr)]">
      <aside className="rounded-3xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="border-b px-4 py-3 dark:border-slate-800">
          <button
            type="button"
            className="w-full rounded-xl bg-brand-600 px-3 py-2 text-sm font-semibold text-white"
            onClick={() => {
              const id = createSession(workspaceId);
              router.replace(`/chat?session=${id}`);
            }}
          >
            New session
          </button>
        </div>
        <ul className="max-h-[calc(100vh-220px)] space-y-1 overflow-y-auto p-2">
          {sessions.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                className={`w-full rounded-xl px-3 py-2 text-left text-xs ${
                  currentSessionId === s.id ?
                    "bg-brand-50 font-semibold text-brand-800 dark:bg-brand-950 dark:text-brand-100"
                  : "hover:bg-slate-50 dark:hover:bg-slate-800"
                }`}
                onClick={() => router.replace(`/chat?session=${s.id}`)}
              >
                {s.title || "Untitled"}
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="flex flex-col rounded-3xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="flex-1 space-y-4 overflow-y-auto p-4 md:p-6">
          {!session?.messages?.length ?
            <p className="text-sm text-slate-500 dark:text-slate-400">
              Ask orchestration questions, route multi-agent tasks, or pair with MCP later.
              The UI reveals the eventual assistant turn after the LangGraph pass completes{" "}
              (token streaming lands in Phase 7).
            </p>
          : null}

          {session?.messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
            >
              <div
                className={`max-w-[min(760px,calc(100%-32px))] rounded-3xl px-4 py-3 text-sm shadow-sm ${
                  msg.role === "user" ?
                    "bg-brand-600 text-white"
                  : "border border-slate-200 bg-slate-50 text-slate-900 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-100"
                }`}
              >
                {msg.role === "assistant" ?
                  <div className="text-sm leading-relaxed [&_a]:text-brand-700 [&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:bg-slate-900 [&_pre]:p-2 [&_pre]:text-[11px] [&_pre]:text-slate-100 dark:[&_a]:text-brand-300">
                    <Markdown remarkPlugins={[remarkGfm]}>
                      {msg.content || (msg.partial ? "…" : "")}
                    </Markdown>
                  </div>
                : msg.content}
              </div>
            </div>
          ))}
        </div>

        <div className="border-t border-slate-200 px-4 py-3 dark:border-slate-800 md:px-6">
          <div className="flex flex-wrap gap-2">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={2}
              className="min-h-[76px] flex-1 resize-none rounded-2xl border border-slate-200 bg-transparent px-3 py-2 text-sm dark:border-slate-700"
              placeholder="Type a directive…"
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  void send();
                }
              }}
            />
            <div className="flex flex-col gap-2">
              <button
                type="button"
                className="inline-flex rounded-2xl border border-slate-200 p-2 dark:border-slate-700"
                aria-label={voice.isListening ? "Stop listening" : "Start microphone"}
                onClick={() =>
                  voice.isListening ? voice.stopListening() : voice.startListening()
                }
              >
                <Mic
                  className={`h-5 w-5 ${voice.isListening ? "animate-pulse text-red-600" : ""}`}
                />
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void send()}
                className="rounded-2xl bg-brand-600 px-6 py-2 text-sm font-semibold text-white disabled:opacity-50"
              >
                Send
              </button>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
