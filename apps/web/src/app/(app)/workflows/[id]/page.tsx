"use client";

import { Check, MessageCircle, Pencil, X } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";
import { useStore } from "zustand";

import { ChatPanel } from "@/components/chat/ChatPanel";
import { InteractiveCanvas } from "@/components/workflow/InteractiveCanvas";
import {
  createWorkflowEditorStore,
  type EditorStore,
} from "@/components/workflow/useWorkflowEditor";
import { useCanvasPersistence } from "@/components/workflow/useCanvasPersistence";
import { draftDefinitionFromDetail, useWorkflowStore } from "@/stores/workflowStore";
import type { TriggerNode } from "@/types/api";

export default function WorkflowDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const current = useWorkflowStore((s) => s.currentWorkflow);
  const load = useWorkflowStore((s) => s.fetchWorkflowDetail);

  useEffect(() => {
    void load(id);
  }, [id, load]);

  // Only derive a definition once the LOADED workflow actually matches this
  // route's id. Otherwise a stale ``currentWorkflow`` (left over from a
  // different workflow opened moments earlier) would seed this editor with the
  // wrong graph — and a Save would then overwrite this workflow with it.
  const serverDefinition = useMemo(() => {
    if (!current || String(current.workflow.id) !== String(id)) return null;
    return draftDefinitionFromDetail(current);
  }, [current, id]);

  // One editor store per workflow id, created the first time the definition
  // loads and kept across server refreshes so in-progress edits survive.
  const [store, setStore] = useState<EditorStore | null>(null);
  const initializedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!serverDefinition) return;
    if (initializedFor.current === id && store) return;
    setStore(createWorkflowEditorStore(serverDefinition));
    initializedFor.current = id;
  }, [serverDefinition, id, store]);

  const chatTrigger = useMemo<TriggerNode | null>(() => {
    if (!serverDefinition) return null;
    for (const n of serverDefinition.nodes ?? []) {
      if (n.kind === "trigger" && (n as TriggerNode).trigger_type === "chat") {
        return n as TriggerNode;
      }
    }
    return null;
  }, [serverDefinition]);

  const [chatOpen, setChatOpen] = useState(false);

  if (!current || String(current.workflow.id) !== String(id)) {
    return (
      <div className="flex justify-center py-20 text-sm text-slate-500">
        Loading workflow…
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-[1600px] space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          {store ? (
            <EditableTitle store={store} workflowId={id} />
          ) : (
            <h1 className="text-2xl font-semibold">{current.workflow.name}</h1>
          )}
          <p className="text-sm text-slate-600 dark:text-slate-400">
            Latest version #{current.workflow.current_version}
          </p>
        </div>
        <div className="flex gap-3">
          {chatTrigger ? (
            <button
              type="button"
              onClick={() => setChatOpen(true)}
              className="inline-flex items-center gap-2 rounded-xl border border-brand-600 px-4 py-2 text-sm font-semibold text-brand-700 hover:bg-brand-50 dark:border-brand-400 dark:text-brand-300 dark:hover:bg-brand-950"
            >
              <MessageCircle className="h-4 w-4" />
              Test
            </button>
          ) : null}
          <Link
            href={`/workflows/${id}/run`}
            className="rounded-xl bg-brand-600 px-4 py-2 text-sm font-semibold text-white"
          >
            Run workflow
          </Link>
          <Link
            href="/workflows"
            className="rounded-xl border px-4 py-2 text-sm dark:border-slate-700"
          >
            Library
          </Link>
        </div>
      </div>

      {chatOpen && chatTrigger ? (
        <div
          className="fixed inset-0 z-40 bg-slate-900/40 backdrop-blur-sm"
          onClick={(e) => {
            if (e.target === e.currentTarget) setChatOpen(false);
          }}
        >
          <div className="fixed right-4 top-4 z-50 w-[480px] max-w-[95vw]">
            <ChatPanel
              workspaceId={String(current.workflow.workspace_id)}
              workflowId={id}
              triggerSlug={chatTrigger.slug}
              onClose={() => setChatOpen(false)}
            />
          </div>
        </div>
      ) : null}

      {store ? (
        <SavedWorkflowCanvas store={store} workflowId={id} />
      ) : (
        <p className="rounded-3xl border border-dashed border-slate-300 p-10 text-center text-sm text-slate-600 dark:border-slate-700 dark:text-slate-400">
          This workflow has no saved versions yet.
        </p>
      )}
    </div>
  );
}

/**
 * Inline-editable workflow title. Renaming hits the lightweight
 * ``PATCH /workflows/{id}`` (name only) — it does NOT create a new version or
 * change publish state — then syncs the editor store's ``definition.name`` in
 * place (without marking the canvas dirty).
 */
function EditableTitle({
  store,
  workflowId,
}: {
  store: EditorStore;
  workflowId: string;
}) {
  const name = useStore(store, (s) => s.definition.name);
  const renameInPlace = useStore(store, (s) => s.renameInPlace);
  const renameWorkflow = useWorkflowStore((s) => s.renameWorkflow);

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(name);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraft(name);
  }, [name]);

  const commit = useCallback(async () => {
    const next = draft.trim();
    if (!next || next === name) {
      setDraft(name);
      setEditing(false);
      return;
    }
    setEditing(false);
    setSaving(true);
    try {
      await renameWorkflow(workflowId, next);
      renameInPlace(next); // keep the editor store in sync (no dirty flag)
      toast.success("Workflow renamed");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Rename failed");
      setDraft(name);
    } finally {
      setSaving(false);
    }
  }, [draft, name, renameInPlace, renameWorkflow, workflowId]);

  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => {
          setDraft(name);
          setEditing(true);
        }}
        className="group inline-flex items-center gap-2 rounded-lg text-left"
        title="Rename workflow"
      >
        <h1 className="text-2xl font-semibold">{name}</h1>
        {saving ? (
          <span className="text-xs text-slate-400">saving…</span>
        ) : (
          <Pencil className="h-4 w-4 text-slate-400 opacity-0 transition group-hover:opacity-100" />
        )}
      </button>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            void commit();
          } else if (e.key === "Escape") {
            setDraft(name);
            setEditing(false);
          }
        }}
        onBlur={() => void commit()}
        maxLength={255}
        className="w-[28rem] max-w-[70vw] rounded-md border border-brand-400 bg-white px-2 py-1 text-2xl font-semibold text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 dark:bg-slate-900 dark:text-slate-100"
      />
      <button
        type="button"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => void commit()}
        className="rounded-md p-1.5 text-emerald-600 hover:bg-emerald-50 dark:hover:bg-emerald-950"
        title="Save name"
      >
        <Check className="h-4 w-4" />
      </button>
      <button
        type="button"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => {
          setDraft(name);
          setEditing(false);
        }}
        className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
        title="Cancel"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

/**
 * Mounts the full interactive editor against an existing workflow. Split into
 * its own component so the persistence hook runs only once ``store`` exists
 * (and is therefore non-null), keeping hook order stable.
 */
function SavedWorkflowCanvas({
  store,
  workflowId,
}: {
  store: EditorStore;
  workflowId: string;
}) {
  const { onSave, onAugment } = useCanvasPersistence(workflowId);
  return (
    <InteractiveCanvas
      store={store}
      workflowId={workflowId}
      onSave={onSave}
      onAugment={onAugment}
    />
  );
}
