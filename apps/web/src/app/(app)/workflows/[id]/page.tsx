"use client";

import { MessageCircle, Plus, Save, Undo2 } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";
import { useStore } from "zustand";

import { ChatPanel } from "@/components/chat/ChatPanel";
import { VisualEditor } from "@/components/workflow/VisualEditor";
import {
  createWorkflowEditorStore,
  type EditorStore,
} from "@/components/workflow/useWorkflowEditor";
import {
  makeBlankNode,
  NODE_KIND_CATALOG,
  uniqueIdFrom,
  type NodeKind,
} from "@/components/workflow/workflow-mutations";
import { findNode } from "@/components/workflow/workflow-mutations";
import { draftDefinitionFromDetail, useWorkflowStore } from "@/stores/workflowStore";
import type { TriggerNode, WorkflowNode } from "@/types/api";

export default function WorkflowDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const current = useWorkflowStore((s) => s.currentWorkflow);
  const load = useWorkflowStore((s) => s.fetchWorkflowDetail);

  useEffect(() => {
    void load(id);
  }, [id, load]);

  const serverDefinition = useMemo(
    () => draftDefinitionFromDetail(current),
    [current],
  );

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
    <div className="mx-auto w-full max-w-[1400px] space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">{current.workflow.name}</h1>
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
        <GraphSummary store={store} workflowId={id} />
      ) : (
        <p className="rounded-3xl border border-dashed border-slate-300 p-10 text-center text-sm text-slate-600 dark:border-slate-700 dark:text-slate-400">
          This workflow has no saved versions yet.
        </p>
      )}
    </div>
  );
}

function GraphSummary({
  store,
  workflowId,
}: {
  store: EditorStore;
  workflowId: string;
}) {
  const definition = useStore(store, (s) => s.definition);
  const selectedId = useStore(store, (s) => s.selectedId);
  const isDirty = useStore(store, (s) => s.isDirty);
  const past = useStore(store, (s) => s.past);
  const addNode = useStore(store, (s) => s.addNode);
  const undo = useStore(store, (s) => s.undo);
  const selectNode = useStore(store, (s) => s.selectNode);
  const markSaved = useStore(store, (s) => s.markSaved);

  const updateWorkflow = useWorkflowStore((s) => s.updateWorkflow);
  const [menuOpen, setMenuOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  const handleAdd = useCallback(
    (kind: NodeKind) => {
      const id = uniqueIdFrom(kind, definition);
      const node = makeBlankNode(kind, id, humanLabel(kind));
      addNode(node as WorkflowNode);
      setMenuOpen(false);
    },
    [definition, addNode],
  );

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await updateWorkflow(workflowId, { definition });
      markSaved();
      toast.success("Workflow saved");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [updateWorkflow, workflowId, definition, markSaved]);

  const selectedNode = useMemo(
    () => (selectedId ? findNode(definition, selectedId) : null),
    [definition, selectedId],
  );

  return (
    <div className="rounded-3xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-900">
      <h2 className="text-lg font-semibold">Graph summary</h2>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <div className="relative">
          <button
            type="button"
            onClick={() => setMenuOpen((o) => !o)}
            className="inline-flex items-center gap-2 rounded-xl border px-4 py-2 text-sm font-medium dark:border-slate-700"
          >
            <Plus className="h-4 w-4" />
            Add node
          </button>
          {menuOpen ? (
            <div className="absolute z-10 mt-2 w-72 rounded-2xl border border-slate-200 bg-white p-2 shadow-xl dark:border-slate-700 dark:bg-slate-900">
              {NODE_KIND_CATALOG.map((entry) => (
                <button
                  key={entry.kind}
                  type="button"
                  onClick={() => handleAdd(entry.kind)}
                  className="block w-full rounded-xl px-3 py-2 text-left hover:bg-slate-100 dark:hover:bg-slate-800"
                >
                  <span className="text-sm font-medium">{entry.label}</span>
                  <span className="block text-xs text-slate-500 dark:text-slate-400">
                    {entry.description}
                  </span>
                </button>
              ))}
            </div>
          ) : null}
        </div>

        <button
          type="button"
          onClick={undo}
          disabled={past.length === 0}
          className="inline-flex items-center gap-2 rounded-xl border px-4 py-2 text-sm font-medium disabled:opacity-40 dark:border-slate-700"
        >
          <Undo2 className="h-4 w-4" />
          Undo
        </button>

        {isDirty ? (
          <button
            type="button"
            onClick={() => void handleSave()}
            disabled={saving}
            className="inline-flex items-center gap-2 rounded-xl bg-brand-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-60"
          >
            <Save className="h-4 w-4" />
            {saving ? "Saving…" : "Save"}
          </button>
        ) : null}
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[1fr_320px]">
        <VisualEditor definition={definition} onNodeSelect={selectNode} />

        <div className="rounded-3xl border border-slate-200 bg-slate-50 p-5 dark:border-slate-800 dark:bg-slate-950">
          <h3 className="text-sm font-semibold">Node inspector</h3>
          {selectedNode ? (
            <div className="mt-3 space-y-2 text-sm">
              <div>
                <span className="text-xs uppercase tracking-wide text-slate-400">
                  Type
                </span>
                <p className="font-medium">{selectedNode.kind}</p>
              </div>
              <div>
                <span className="text-xs uppercase tracking-wide text-slate-400">
                  Id
                </span>
                <p className="font-mono text-xs">{selectedNode.id}</p>
              </div>
              <div>
                <span className="text-xs uppercase tracking-wide text-slate-400">
                  Name
                </span>
                <p className="font-medium">
                  {(selectedNode as { name?: string }).name ?? "—"}
                </p>
              </div>
              <pre className="mt-3 max-h-72 overflow-auto rounded-xl bg-slate-900 p-3 text-[11px] leading-relaxed text-slate-100">
                {JSON.stringify(selectedNode, null, 2)}
              </pre>
            </div>
          ) : (
            <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
              Click any node to see its configuration (prompts, tools, branches,
              etc.).
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function humanLabel(kind: NodeKind): string {
  switch (kind) {
    case "for_each":
      return "For Each";
    case "wait_for_webhook":
      return "Wait for Webhook";
    case "data_store":
      return "Data Store";
    case "output_parser":
      return "Output Parser";
    default:
      return kind[0].toUpperCase() + kind.slice(1);
  }
}
