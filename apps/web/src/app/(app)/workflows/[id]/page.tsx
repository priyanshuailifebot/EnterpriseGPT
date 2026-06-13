"use client";

import { MessageCircle } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

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
    <div className="mx-auto w-full max-w-[1600px] space-y-6">
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
