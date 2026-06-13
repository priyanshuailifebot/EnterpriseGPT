"use client";

/**
 * Conversational "Refine with AI" panel — n8n-style assistant docked on the
 * right of the canvas.
 *
 * It is intentionally NON-MODAL: the canvas stays clickable while it's open so
 * the user can select a node to scope an instruction ("make THIS agent
 * stricter"). Each instruction posts to ``/workflows/{id}/augment`` (via the
 * ``onSubmit`` callback) and the host canvas shows the proposed graph as a
 * highlighted diff. The user then Accepts (applies it as an undoable, dirty
 * edit) or Rejects (discards) — surfaced here as per-message buttons.
 */

import { ArrowUp, Check, Sparkles, Target, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

export interface ChatSubmitResult {
  ok: boolean;
  changes: string[];
  /** True when the proposal actually changed the graph (a preview is now
   *  pending). False means "no structural changes" — nothing to accept. */
  hasPreview: boolean;
}

interface WorkflowChatPanelProps {
  open: boolean;
  onClose: () => void;
  /** No saved workflow id yet — refine is unavailable. */
  disabled: boolean;
  selectedNode: { id: string; name: string } | null;
  onClearScope: () => void;
  /** A proposal is awaiting accept/reject on the canvas. */
  pendingPreview: boolean;
  onSubmit: (message: string, focusNodeId: string | null) => Promise<ChatSubmitResult>;
  onAccept: () => void;
  onReject: () => void;
}

type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  changes?: string[];
  /** Set on the assistant message tied to the currently-pending preview. */
  proposal?: boolean;
};

let _msgSeq = 0;
function nextId(): string {
  _msgSeq += 1;
  return `m${_msgSeq}`;
}

export function WorkflowChatPanel({
  open,
  onClose,
  disabled,
  selectedNode,
  onClearScope,
  pendingPreview,
  onSubmit,
  onAccept,
  onReject,
}: WorkflowChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  // Id of the assistant message whose proposal is awaiting a decision.
  const [awaitingId, setAwaitingId] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, busy]);

  // If the preview was resolved elsewhere (e.g. the canvas banner, or an
  // Undo), stop showing Accept/Reject on the stale message.
  useEffect(() => {
    if (!pendingPreview && awaitingId) setAwaitingId(null);
  }, [pendingPreview, awaitingId]);

  const push = useCallback((m: Omit<ChatMessage, "id">) => {
    const id = nextId();
    setMessages((prev) => [...prev, { ...m, id }]);
    return id;
  }, []);

  const send = useCallback(async () => {
    const text = draft.trim();
    if (!text || busy || disabled || pendingPreview) return;
    const focusId = selectedNode?.id ?? null;
    const scopeNote = selectedNode ? ` (scoped to “${selectedNode.name}”)` : "";
    push({ role: "user", text: `${text}${scopeNote}` });
    setDraft("");
    setBusy(true);
    try {
      const res = await onSubmit(text, focusId);
      if (!res.ok) {
        push({
          role: "assistant",
          text: "I couldn't apply that — see the error toast and try rephrasing.",
        });
      } else if (!res.hasPreview) {
        push({
          role: "assistant",
          text: "No structural changes were needed for that request.",
        });
      } else {
        const id = push({
          role: "assistant",
          text:
            res.changes.length > 0
              ? "Here's what I'd change — review it on the canvas, then Accept or Reject:"
              : "I've proposed an update — review it on the canvas, then Accept or Reject:",
          changes: res.changes,
          proposal: true,
        });
        setAwaitingId(id);
      }
    } finally {
      setBusy(false);
    }
  }, [draft, busy, disabled, pendingPreview, selectedNode, push, onSubmit]);

  const accept = useCallback(() => {
    onAccept();
    setAwaitingId(null);
    push({ role: "system", text: "✓ Applied to the canvas. Review and Save to persist." });
  }, [onAccept, push]);

  const reject = useCallback(() => {
    onReject();
    setAwaitingId(null);
    push({ role: "system", text: "Discarded the proposed changes." });
  }, [onReject, push]);

  if (!open) return null;

  return (
    <div className="fixed right-0 top-0 z-30 flex h-full w-[380px] max-w-[92vw] flex-col border-l border-slate-200 bg-white shadow-2xl dark:border-slate-800 dark:bg-slate-950">
      <header className="flex items-center justify-between gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
        <div className="flex items-center gap-2">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand-100 text-brand-700 dark:bg-brand-950 dark:text-brand-300">
            <Sparkles className="h-4 w-4" />
          </span>
          <div>
            <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">
              Refine with AI
            </p>
            <p className="text-[10px] text-slate-500 dark:text-slate-400">
              Describe a change, or select a node to scope it.
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md p-1 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
          aria-label="Close refine panel"
        >
          <X className="h-4 w-4" />
        </button>
      </header>

      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {messages.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-200 p-4 text-center text-[11px] leading-relaxed text-slate-500 dark:border-slate-800 dark:text-slate-400">
            Try: <em>“Add a Slack notification after the ticket is created.”</em>
            <br />
            or select a node and say <em>“make this stricter about refunds.”</em>
          </div>
        ) : null}

        {messages.map((m) => (
          <ChatBubble
            key={m.id}
            message={m}
            showDecision={m.proposal === true && m.id === awaitingId && pendingPreview}
            onAccept={accept}
            onReject={reject}
          />
        ))}

        {busy ? (
          <div className="flex items-center gap-2 text-[11px] text-slate-500 dark:text-slate-400">
            <Sparkles className="h-3 w-3 animate-pulse" />
            Thinking through the change…
          </div>
        ) : null}
      </div>

      <div className="border-t border-slate-200 p-3 dark:border-slate-800">
        {selectedNode ? (
          <div className="mb-2 inline-flex items-center gap-1.5 rounded-full bg-brand-50 px-2.5 py-1 text-[11px] font-medium text-brand-800 dark:bg-brand-950 dark:text-brand-200">
            <Target className="h-3 w-3" />
            Editing: {selectedNode.name}
            <button
              type="button"
              onClick={onClearScope}
              className="ml-0.5 rounded-full p-0.5 hover:bg-brand-100 dark:hover:bg-brand-900"
              aria-label="Clear node scope"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        ) : null}

        {disabled ? (
          <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
            Save the workflow once before refining with AI.
          </p>
        ) : (
          <div className="flex items-end gap-2">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              rows={2}
              disabled={busy || pendingPreview}
              placeholder={
                pendingPreview
                  ? "Accept or reject the current proposal first…"
                  : selectedNode
                    ? `Change “${selectedNode.name}”…`
                    : "Describe a change to the workflow…"
              }
              className="max-h-32 flex-1 resize-y rounded-lg border border-slate-300 bg-white px-3 py-2 text-[12px] text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-60 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
            />
            <button
              type="button"
              onClick={() => void send()}
              disabled={busy || pendingPreview || !draft.trim()}
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand-600 text-white shadow-sm hover:bg-brand-700 disabled:opacity-50"
              aria-label="Send"
            >
              <ArrowUp className="h-4 w-4" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function ChatBubble({
  message,
  showDecision,
  onAccept,
  onReject,
}: {
  message: ChatMessage;
  showDecision: boolean;
  onAccept: () => void;
  onReject: () => void;
}) {
  if (message.role === "system") {
    return (
      <p className="text-center text-[10px] font-medium uppercase tracking-wide text-slate-400 dark:text-slate-500">
        {message.text}
      </p>
    );
  }
  const isUser = message.role === "user";
  return (
    <div className={cn("flex", isUser ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[88%] rounded-2xl px-3 py-2 text-[12px] leading-relaxed",
          isUser
            ? "bg-brand-600 text-white"
            : "border border-slate-200 bg-slate-50 text-slate-800 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-100",
        )}
      >
        <p className="whitespace-pre-wrap">{message.text}</p>
        {message.changes && message.changes.length > 0 ? (
          <ul className="mt-2 space-y-0.5 border-t border-slate-200/60 pt-2 text-[11px] dark:border-slate-700/60">
            {message.changes.map((c, i) => (
              <li key={i} className="flex gap-1.5">
                <span className="opacity-50">•</span>
                <span>{c}</span>
              </li>
            ))}
          </ul>
        ) : null}
        {showDecision ? (
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={onAccept}
              className="inline-flex items-center gap-1 rounded-md bg-emerald-600 px-2.5 py-1 text-[11px] font-semibold text-white hover:bg-emerald-700"
            >
              <Check className="h-3 w-3" />
              Accept
            </button>
            <button
              type="button"
              onClick={onReject}
              className="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2.5 py-1 text-[11px] font-semibold text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              <X className="h-3 w-3" />
              Reject
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
