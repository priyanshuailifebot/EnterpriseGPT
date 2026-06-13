"use client";

/**
 * Production-grade interactive workflow canvas (React Flow).
 *
 * Wraps an editor store, the read-only ``VisualEditor`` node renderers,
 * and adds:
 *
 *  - Drag from ``NodePalette`` → drop on canvas to create a node at
 *    cursor position.
 *  - Click a node → emits selection up to the store so the
 *    ``PropertyInspector`` can edit it.
 *  - Drag from any node's right handle to another's left handle to
 *    add a ``depends_on`` edge. Cycles are rejected silently.
 *  - Select an edge then press Backspace/Delete to remove it.
 *  - Press Backspace/Delete with a selected node to delete it.
 *  - Cmd/Ctrl+Z / Cmd/Ctrl+Shift+Z for undo / redo.
 *  - Save / discard buttons rendered in the canvas top bar.
 *  - AI Refine drawer that posts to ``/workflows/{id}/augment``.
 *
 * State lives in the ``EditorStore`` passed in via props; the canvas is
 * a controlled component over that store so the host page can subscribe
 * (e.g. to enable the global Save shortcut).
 */

import "@xyflow/react/dist/style.css";

import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  Background,
  type Connection,
  Controls,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from "@xyflow/react";
import { PlayCircle, Redo2, Rocket, Save, Undo2, Wand2 } from "lucide-react";
import {
  type DragEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import toast from "react-hot-toast";
import { useStore } from "zustand";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/authStore";
import { useWorkflowStore } from "@/stores/workflowStore";
import type {
  NativeConnectionResponse,
  NativeProviderCatalogResponse,
  WorkflowDefinition,
  WorkflowNode,
  WorkflowStatus,
} from "@/types/api";

import { NodeInspectDrawer } from "./NodeInspectDrawer";
import { NodePalette, PALETTE_DRAG_MIME } from "./NodePalette";
import { PropertyInspector } from "./PropertyInspector";
import { TestRunPanel } from "./TestRunPanel";
import { WorkflowChatPanel, type ChatSubmitResult } from "./WorkflowChatPanel";
import { type ExecutionRunState } from "./execution-status";
import { type EditorStore } from "./useWorkflowEditor";
import {
  type NodeDiff,
  diffClassName,
  diffDefinitions,
  diffIsEmpty,
} from "./workflow-diff";
import {
  allNodes,
  makeBlankNode,
  uniqueIdFrom,
  type NodeKind,
} from "./workflow-mutations";

// We reuse the read-only node renderers from VisualEditor so the
// interactive canvas looks identical to the preview. Importing them
// directly keeps the visual treatment in one place.
import {
  AGENT_FLOW_NODE_TYPES,
  BRANCH_EDGE_TYPES,
  flowTypeForKind,
  toFlowNodeData,
} from "./visual-editor-shared";
import { workflowToFlowGraph } from "./workflow-topology";

const NODE_COL_X = 300;
const NODE_ROW_Y = 200;

export interface AugmentInput {
  message: string;
  focusNodeId: string | null;
  definition: WorkflowDefinition;
}

export interface AugmentProposal {
  proposed: WorkflowDefinition;
  changes: string[];
}

export interface InteractiveCanvasProps {
  store: EditorStore;
  /** Workflow id when the canvas is editing a saved workflow. ``null`` for
   *  a brand-new graph that hasn't been persisted yet (Save creates it). */
  workflowId: string | null;
  onSave: (defn: WorkflowDefinition) => Promise<void> | void;
  /** Resolve an NL refine request to a proposed graph (NOT applied). The
   *  canvas previews the diff and applies it only when the user Accepts. */
  onAugment?: (input: AugmentInput) => Promise<AugmentProposal>;
  /** Disables the AI Refine panel when the parent doesn't wire augment. */
  readOnly?: boolean;
}

export function InteractiveCanvas(props: InteractiveCanvasProps) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function CanvasInner({
  store,
  workflowId,
  onSave,
  onAugment,
  readOnly,
}: InteractiveCanvasProps) {
  const definition = useStore(store, (s) => s.definition);
  const selectedId = useStore(store, (s) => s.selectedId);
  const issues = useStore(store, (s) => s.issues);
  const isDirty = useStore(store, (s) => s.isDirty);
  const past = useStore(store, (s) => s.past);
  const future = useStore(store, (s) => s.future);

  const addNodeAction = useStore(store, (s) => s.addNode);
  const removeNodeAction = useStore(store, (s) => s.removeNode);
  const connectAction = useStore(store, (s) => s.connect);
  const disconnectAction = useStore(store, (s) => s.disconnect);
  const patchNodeAction = useStore(store, (s) => s.patchNode);
  const renameAction = useStore(store, (s) => s.renameNodeId);
  const selectNode = useStore(store, (s) => s.selectNode);
  const undo = useStore(store, (s) => s.undo);
  const redo = useStore(store, (s) => s.redo);
  const markSaved = useStore(store, (s) => s.markSaved);
  const applyProposedAction = useStore(store, (s) => s.applyProposed);

  // ----------------------------------------------------------------------
  // AI refine — a proposed graph awaiting accept/reject. While set, the
  // canvas renders ``preview.proposed`` (read-only) with diff rings instead
  // of the editable store definition.
  // ----------------------------------------------------------------------
  const [chatOpen, setChatOpen] = useState(false);
  const [preview, setPreview] = useState<
    { proposed: WorkflowDefinition; diff: NodeDiff } | null
  >(null);

  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const reactFlow = useReactFlow();

  // ----------------------------------------------------------------------
  // Live execution state — pushed up from <TestRunPanel> via
  // ``onExecutionState`` so this canvas can paint per-node status rings.
  // ----------------------------------------------------------------------
  const [executionState, setExecutionState] = useState<ExecutionRunState | null>(
    null,
  );
  const [testPanelOpen, setTestPanelOpen] = useState(false);

  // Connection awareness — which providers have an active connection, so we can
  // badge action nodes that still need connecting (n8n-style). Only catalog
  // providers are connectable; others (e.g. Google Sheets) aren't badged.
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const [connectedProviders, setConnectedProviders] = useState<Set<string>>(new Set());
  const [catalogProviderIds, setCatalogProviderIds] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!workspaceId) return;
    let cancelled = false;
    (async () => {
      try {
        const [cat, conns] = await Promise.all([
          api.get<NativeProviderCatalogResponse>("/api/v1/connections/providers"),
          api.get<NativeConnectionResponse[]>(
            `/api/v1/connections?workspace_id=${workspaceId}`,
          ),
        ]);
        if (cancelled) return;
        setCatalogProviderIds(new Set(cat.data.providers.map((p) => p.id)));
        setConnectedProviders(
          new Set(conns.data.filter((c) => c.status === "active").map((c) => c.provider)),
        );
      } catch {
        /* connection badges are best-effort; ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, testPanelOpen]);
  // Node whose test-run input/output is shown in the inspect drawer. Set only
  // when a clicked node has run data; while non-null the PropertyInspector is
  // suppressed so the two right-rail panels never collide.
  const [inspectNodeId, setInspectNodeId] = useState<string | null>(null);

  // ----------------------------------------------------------------------
  // Convert WorkflowDefinition → React Flow nodes/edges.
  //
  // We keep ``positions`` as a *local* state map keyed by node id so the
  // user's hand-tuned layout survives mutations to the definition. New
  // nodes start at the cursor drop position; pre-existing ones fall back
  // to the auto-layout depth/row.
  // ----------------------------------------------------------------------
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});

  // Build React Flow nodes via the same proven topology pipeline the
  // read-only detail page uses (``workflowToFlowGraph``). This keeps the
  // two canvases visually identical and avoids divergence bugs (the
  // older hand-rolled ``allNodes + autoLayout`` path was silently
  // dropping nodes when the LLM emitted certain shapes).
  // While a refine proposal is pending, the canvas shows the PROPOSED graph
  // (read-only) so the diff rings line up; otherwise the live store graph.
  const activeDefinition = preview?.proposed ?? definition;

  const nodesView = useMemo(() => {
    const { nodes: topo } = workflowToFlowGraph(activeDefinition);
    const out: Node[] = topo.map((n) => {
      const xy = positions[n.id] ?? {
        x: 48 + n.depth * NODE_COL_X,
        y: 56 + n.orderInLevel * NODE_ROW_Y,
      };
      const raw = n.data.raw;
      // Preview mode: paint diff rings, suppress run/selection chrome.
      if (preview) {
        return {
          id: n.id,
          type: flowTypeForKind(n.data.kind),
          position: xy,
          data: toFlowNodeData(raw, activeDefinition, undefined),
          selected: false,
          className: diffClassName(n.id, preview.diff),
        };
      }
      const runStatus = executionState?.nodes[n.id];
      // Badge action nodes whose (connectable) provider has no active
      // connection — but only when we're not mid-run (run rings take over).
      const prov = (raw as { provider?: string }).provider;
      const needsConn =
        !executionState &&
        n.data.kind === "action" &&
        !!prov &&
        catalogProviderIds.has(prov) &&
        !connectedProviders.has(prov);
      return {
        id: n.id,
        type: flowTypeForKind(n.data.kind),
        position: xy,
        data: toFlowNodeData(raw, activeDefinition, runStatus),
        selected: n.id === selectedId,
        className: runStatus
          ? `egpt-run-${runStatus.status}`
          : needsConn
            ? "egpt-needs-conn"
            : undefined,
      };
    });
    return out;
  }, [
    activeDefinition,
    preview,
    positions,
    selectedId,
    executionState,
    catalogProviderIds,
    connectedProviders,
  ]);

  const edgesView = useMemo(() => {
    const { edges } = workflowToFlowGraph(activeDefinition);
    return edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      sourceHandle: "src",
      targetHandle: "tgt",
      type: "branch",
      animated: true,
      data: { branchLabel: e.branchLabel, fromForEach: e.fromForEach },
    })) as Edge[];
  }, [activeDefinition]);

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      // Editing is frozen while reviewing an AI proposal.
      if (preview) return;
      // Apply React Flow's local position changes (dragging) into our
      // positions map. Selection changes funnel back into the store.
      const next = applyNodeChanges(changes, nodesView);
      const newPositions = { ...positions };
      for (const n of next) newPositions[n.id] = n.position;
      setPositions(newPositions);
      for (const ch of changes) {
        if (ch.type === "select") {
          // If the selected node has test-run data, open the inspect drawer
          // (read-only view of its input/output) instead of the editor form.
          if (ch.selected && executionState?.nodes[ch.id]) {
            setInspectNodeId(ch.id);
          } else {
            setInspectNodeId(null);
          }
          selectNode(ch.selected ? ch.id : null);
        }
        if (ch.type === "remove") {
          removeNodeAction(ch.id);
        }
      }
    },
    [nodesView, positions, selectNode, removeNodeAction, executionState, preview],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      if (preview) return;
      for (const ch of changes) {
        if (ch.type === "remove") {
          const edge = edgesView.find((e) => e.id === ch.id);
          if (edge) disconnectAction(edge.source, edge.target);
        }
      }
      // Local React Flow visual state for animation transitions — ignored
      // here because the store is the source of truth.
      void applyEdgeChanges(changes, edgesView);
    },
    [disconnectAction, edgesView, preview],
  );

  const onConnect = useCallback(
    (conn: Connection) => {
      if (preview) return;
      if (!conn.source || !conn.target) return;
      connectAction(conn.source, conn.target);
      // ``addEdge`` is unused here — we re-derive edges from the store —
      // but invoking it would also be fine since the next render replaces
      // the local view. Keeping the call out avoids a no-op flicker.
      void addEdge;
    },
    [connectAction, preview],
  );

  const onDragOver = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }, []);

  const onDrop = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      if (preview) return;
      const kind = e.dataTransfer.getData(PALETTE_DRAG_MIME) as NodeKind | "";
      if (!kind) return;
      const bounds = wrapperRef.current?.getBoundingClientRect();
      if (!bounds) return;
      const position = reactFlow.screenToFlowPosition({
        x: e.clientX,
        y: e.clientY,
      });
      const id = uniqueIdFrom(kind, definition);
      const name = humanLabel(kind);
      const node = makeBlankNode(kind, id, name);
      addNodeAction(node as WorkflowNode);
      setPositions((prev) => ({ ...prev, [id]: position }));
    },
    [reactFlow, definition, addNodeAction, preview],
  );

  // Click on empty canvas → clear selection so the inspector stops
  // editing whatever was last selected, and close the inspect drawer.
  const onPaneClick = useCallback(() => {
    selectNode(null);
    setInspectNodeId(null);
  }, [selectNode]);

  // Keyboard shortcuts — undo/redo + delete the selected node.
  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      if (preview) return;
      const ctrl = e.metaKey || e.ctrlKey;
      if (ctrl && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
        return;
      }
      if ((e.key === "Backspace" || e.key === "Delete") && selectedId) {
        e.preventDefault();
        removeNodeAction(selectedId);
      }
    },
    [undo, redo, selectedId, removeNodeAction, preview],
  );

  // ----------------------------------------------------------------------
  // Save handler + dirty guard
  // ----------------------------------------------------------------------
  const [saving, setSaving] = useState(false);

  // ----------------------------------------------------------------------
  // AI refine flow — request a proposal (no apply), preview it, accept/reject.
  // ----------------------------------------------------------------------
  const selectedNode = useMemo(() => {
    if (!selectedId) return null;
    const n = allNodes(definition).find((x) => x.id === selectedId);
    return n ? { id: n.id, name: n.name || n.id } : null;
  }, [definition, selectedId]);

  const handleChatSubmit = useCallback(
    async (message: string, focusNodeId: string | null): Promise<ChatSubmitResult> => {
      if (!onAugment) return { ok: false, changes: [], hasPreview: false };
      try {
        const current = store.getState().definition;
        const { proposed, changes } = await onAugment({
          message,
          focusNodeId,
          definition: current,
        });
        const diff = diffDefinitions(current, proposed);
        if (diffIsEmpty(diff)) {
          return { ok: true, changes, hasPreview: false };
        }
        setPreview({ proposed, diff });
        return { ok: true, changes, hasPreview: true };
      } catch {
        // onAugment surfaces its own error toast.
        return { ok: false, changes: [], hasPreview: false };
      }
    },
    [onAugment, store],
  );

  const acceptPreview = useCallback(() => {
    if (!preview) return;
    applyProposedAction(preview.proposed);
    setPreview(null);
  }, [preview, applyProposedAction]);

  const rejectPreview = useCallback(() => {
    setPreview(null);
  }, []);

  // ----------------------------------------------------------------------
  // Publish lifecycle — status badge + Publish/Unpublish (publish-gate).
  // ----------------------------------------------------------------------
  const publishWorkflow = useWorkflowStore((s) => s.publishWorkflow);
  const unpublishWorkflow = useWorkflowStore((s) => s.unpublishWorkflow);
  const wfStatus = useWorkflowStore((s): WorkflowStatus | null => {
    if (!workflowId) return null;
    if (s.currentWorkflow?.workflow.id === workflowId)
      return s.currentWorkflow.workflow.status;
    return s.workflows.find((w) => w.id === workflowId)?.status ?? null;
  });
  const [publishing, setPublishing] = useState(false);

  const handlePublish = useCallback(async () => {
    if (!workflowId) return;
    setPublishing(true);
    try {
      await publishWorkflow(workflowId);
      toast.success("Published — live runs now perform real actions.");
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Publish failed — run a successful test first.";
      toast.error(detail);
    } finally {
      setPublishing(false);
    }
  }, [workflowId, publishWorkflow]);

  const handleUnpublish = useCallback(async () => {
    if (!workflowId) return;
    setPublishing(true);
    try {
      await unpublishWorkflow(workflowId);
      toast.success("Back to draft — runs now preview, nothing is sent.");
    } catch {
      toast.error("Could not unpublish.");
    } finally {
      setPublishing(false);
    }
  }, [workflowId, unpublishWorkflow]);

  const handleSave = useCallback(async () => {
    const errors = issues.filter((i) => i.severity === "error");
    if (errors.length > 0) {
      toast.error(
        `${errors.length} validation error${errors.length === 1 ? "" : "s"}; fix before saving.`,
      );
      return;
    }
    setSaving(true);
    try {
      await onSave(definition);
      markSaved();
      toast.success("Workflow saved");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [issues, onSave, definition, markSaved]);

  // Re-fit the viewport whenever the node set changes — this is what makes
  // freshly generated workflows actually appear on the canvas. React Flow's
  // ``fitView`` prop only fires on initial mount; without this effect, nodes
  // added after mount stay off-screen and the canvas looks blank.
  useEffect(() => {
    if (nodesView.length === 0) return;
    // Defer one frame so React Flow has the new nodes measured before fitting.
    const t = window.setTimeout(() => {
      try {
        reactFlow.fitView({ padding: 0.2, duration: 200 });
      } catch {
        // ReactFlow not ready yet; harmless.
      }
    }, 30);
    return () => window.clearTimeout(t);
  }, [nodesView.length, reactFlow]);

  // Block page navigation when there are unsaved edits.
  useEffect(() => {
    if (!isDirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isDirty]);

  const errorCount = issues.filter((i) => i.severity === "error").length;

  return (
    <div className="flex w-full gap-3" onKeyDown={onKeyDown} tabIndex={-1}>
      {!readOnly ? <NodePalette onAddNode={(k) => addAtCenter(k)} /> : null}

      <div className="flex min-h-[820px] flex-1 flex-col gap-2">
        <Toolbar
          isDirty={isDirty}
          canUndo={past.length > 0}
          canRedo={future.length > 0}
          errorCount={errorCount}
          saving={saving}
          showAugment={!!onAugment}
          canTestRun={!!workflowId}
          status={wfStatus}
          publishing={publishing}
          onPublish={() => void handlePublish()}
          onUnpublish={() => void handleUnpublish()}
          onSave={() => void handleSave()}
          onUndo={undo}
          onRedo={redo}
          onOpenRefine={() => setChatOpen((o) => !o)}
          onOpenTestRun={() => setTestPanelOpen(true)}
        />
        <div
          ref={wrapperRef}
          onDrop={onDrop}
          onDragOver={onDragOver}
          className="reactflow-themed relative h-[calc(100vh-220px)] min-h-[720px] w-full rounded-2xl border border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-950"
        >
          <ReactFlow
            nodes={nodesView}
            edges={edgesView}
            nodeTypes={AGENT_FLOW_NODE_TYPES}
            edgeTypes={BRANCH_EDGE_TYPES}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onPaneClick={onPaneClick}
            nodesDraggable={!preview}
            nodesConnectable={!preview}
            elementsSelectable={!preview}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            proOptions={{ hideAttribution: true }}
            deleteKeyCode={null /* we handle keys ourselves */}
          >
            <Background gap={28} />
            <Controls
              className="!rounded-xl !border !border-slate-200 !bg-white !shadow-lg dark:!border-slate-700 dark:!bg-slate-900"
              showInteractive={false}
            />
          </ReactFlow>
          {nodesView.length === 0 ? (
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-3 rounded-2xl">
              <div className="flex flex-col items-center gap-2 rounded-2xl bg-white/80 px-8 py-6 text-center shadow-sm dark:bg-slate-900/80">
                <span className="text-3xl">👨‍🍳</span>
                <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
                  Something&apos;s cooking up…
                </p>
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  Drag a node from the palette to get started, or switch to{" "}
                  <strong>Describe</strong> to review the generated blueprint.
                </p>
              </div>
            </div>
          ) : null}
          {preview ? (
            <DiffReviewBanner
              diff={preview.diff}
              onAccept={acceptPreview}
              onReject={rejectPreview}
            />
          ) : null}
        </div>
        {issues.length > 0 ? <IssuesBar issues={issues} /> : null}
      </div>

      {!readOnly && !inspectNodeId ? (
        <PropertyInspector
          state={{ definition, selectedId }}
          onPatchNode={patchNodeAction}
          onRenameId={renameAction}
          onRemoveNode={removeNodeAction}
          onClearSelection={() => selectNode(null)}
          workflowId={workflowId}
        />
      ) : null}

      <NodeInspectDrawer
        open={inspectNodeId !== null}
        onClose={() => {
          setInspectNodeId(null);
          selectNode(null);
        }}
        nodeId={inspectNodeId}
        runState={inspectNodeId ? executionState?.nodes[inspectNodeId] : undefined}
      />

      <WorkflowChatPanel
        open={chatOpen}
        onClose={() => setChatOpen(false)}
        disabled={!onAugment || !workflowId}
        selectedNode={selectedNode}
        onClearScope={() => selectNode(null)}
        pendingPreview={preview !== null}
        onSubmit={handleChatSubmit}
        onAccept={acceptPreview}
        onReject={rejectPreview}
      />

      <TestRunPanel
        open={testPanelOpen}
        onClose={() => setTestPanelOpen(false)}
        workflowId={workflowId}
        definition={definition}
        onExecutionState={setExecutionState}
      />
    </div>
  );

  function addAtCenter(kind: NodeKind) {
    const id = uniqueIdFrom(kind, definition);
    const name = humanLabel(kind);
    addNodeAction(makeBlankNode(kind, id, name) as WorkflowNode);
    // Drop centred relative to current viewport so the user can see it.
    const viewport = reactFlow.getViewport();
    const cx = 320 - viewport.x / viewport.zoom;
    const cy = 220 - viewport.y / viewport.zoom;
    setPositions((prev) => ({ ...prev, [id]: { x: cx, y: cy } }));
  }
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

function Toolbar({
  isDirty,
  canUndo,
  canRedo,
  errorCount,
  saving,
  showAugment,
  canTestRun,
  status,
  publishing,
  onPublish,
  onUnpublish,
  onSave,
  onUndo,
  onRedo,
  onOpenRefine,
  onOpenTestRun,
}: {
  isDirty: boolean;
  canUndo: boolean;
  canRedo: boolean;
  errorCount: number;
  saving: boolean;
  showAugment: boolean;
  canTestRun: boolean;
  status: WorkflowStatus | null;
  publishing: boolean;
  onPublish: () => void;
  onUnpublish: () => void;
  onSave: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onOpenRefine: () => void;
  onOpenTestRun: () => void;
}) {
  const isPublished = status === "published";
  return (
    <div className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-2 shadow-sm dark:border-slate-800 dark:bg-slate-950">
      <button
        type="button"
        onClick={onUndo}
        disabled={!canUndo}
        className={toolButtonClasses}
        title="Undo (⌘Z)"
      >
        <Undo2 className="h-4 w-4" />
      </button>
      <button
        type="button"
        onClick={onRedo}
        disabled={!canRedo}
        className={toolButtonClasses}
        title="Redo (⌘⇧Z)"
      >
        <Redo2 className="h-4 w-4" />
      </button>
      <div className="ml-2 flex-1">
        {errorCount > 0 ? (
          <p className="text-[11px] font-medium text-rose-600 dark:text-rose-400">
            {errorCount} validation issue{errorCount === 1 ? "" : "s"} — fix before saving.
          </p>
        ) : isDirty ? (
          <p className="text-[11px] text-amber-600 dark:text-amber-400">
            Unsaved changes
          </p>
        ) : (
          <p className="text-[11px] text-slate-500 dark:text-slate-400">All changes saved</p>
        )}
      </div>
      {status ? (
        <span
          className={cn(
            "rounded-full border px-2.5 py-0.5 text-[11px] font-semibold capitalize",
            isPublished
              ? "border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200"
              : "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200",
          )}
          title={
            isPublished
              ? "Live — production runs perform real actions"
              : "Draft — runs preview only; nothing is sent until published"
          }
        >
          {isPublished ? "Published" : "Draft"}
        </span>
      ) : null}
      <button
        type="button"
        onClick={onOpenTestRun}
        disabled={!canTestRun}
        className={cn(
          toolButtonClasses,
          "flex items-center gap-1 text-emerald-700 disabled:opacity-40 dark:text-emerald-300",
        )}
        title={canTestRun ? "Test workflow with mocked or real data" : "Save before testing"}
      >
        <PlayCircle className="h-4 w-4" />
        <span className="text-[12px] font-semibold">Test</span>
      </button>
      {showAugment ? (
        <button
          type="button"
          onClick={onOpenRefine}
          className={cn(
            toolButtonClasses,
            "flex items-center gap-1 text-brand-700 dark:text-brand-300",
          )}
        >
          <Wand2 className="h-4 w-4" />
          <span className="text-[12px] font-semibold">Refine with AI</span>
        </button>
      ) : null}
      <button
        type="button"
        onClick={onSave}
        disabled={saving || errorCount > 0}
        className="flex items-center gap-1.5 rounded-md bg-brand-600 px-3 py-1.5 text-[12px] font-semibold text-white shadow-sm hover:bg-brand-700 disabled:opacity-60"
      >
        <Save className="h-3.5 w-3.5" />
        {saving ? "Saving…" : "Save"}
      </button>
      {canTestRun ? (
        isPublished ? (
          <button
            type="button"
            onClick={onUnpublish}
            disabled={publishing}
            className={cn(
              toolButtonClasses,
              "flex items-center gap-1 text-slate-600 dark:text-slate-300",
            )}
            title="Revert to draft — runs will preview, nothing sent"
          >
            <span className="text-[12px] font-semibold">
              {publishing ? "…" : "Unpublish"}
            </span>
          </button>
        ) : (
          <button
            type="button"
            onClick={onPublish}
            disabled={publishing}
            className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-3 py-1.5 text-[12px] font-semibold text-white shadow-sm hover:bg-emerald-700 disabled:opacity-60"
            title="Go live — requires a successful test run of the current version"
          >
            <Rocket className="h-3.5 w-3.5" />
            {publishing ? "Publishing…" : "Publish"}
          </button>
        )
      ) : null}
    </div>
  );
}

const toolButtonClasses =
  "rounded-md border border-transparent px-2 py-1.5 text-slate-700 hover:bg-slate-100 disabled:opacity-40 dark:text-slate-300 dark:hover:bg-slate-800";

function IssuesBar({
  issues,
}: {
  issues: { nodeId: string | null; message: string; severity: "error" | "warning" }[];
}) {
  return (
    <ul
      role="status"
      aria-live="polite"
      className="rounded-2xl border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] text-rose-700 dark:border-rose-950 dark:bg-rose-950/40 dark:text-rose-300"
    >
      {issues.slice(0, 6).map((i, idx) => (
        <li key={`${i.nodeId ?? "_"}-${idx}`} className="leading-tight">
          <span className="font-mono">{i.nodeId ?? "graph"}</span>: {i.message}
        </li>
      ))}
      {issues.length > 6 ? (
        <li className="opacity-70">… and {issues.length - 6} more</li>
      ) : null}
    </ul>
  );
}

/** Sticky banner shown over the canvas while an AI proposal is being
 *  reviewed. Mirrors the chat panel's Accept/Reject so closing the chat
 *  never strands a pending preview. */
function DiffReviewBanner({
  diff,
  onAccept,
  onReject,
}: {
  diff: NodeDiff;
  onAccept: () => void;
  onReject: () => void;
}) {
  const parts: string[] = [];
  if (diff.added.size) parts.push(`${diff.added.size} added`);
  if (diff.modified.size) parts.push(`${diff.modified.size} edited`);
  if (diff.removed.size) parts.push(`${diff.removed.size} removed`);
  const removedNames = [...diff.removed];
  return (
    <div className="absolute left-1/2 top-3 z-10 flex -translate-x-1/2 items-center gap-3 rounded-full border border-brand-200 bg-white/95 px-4 py-2 shadow-lg backdrop-blur dark:border-brand-900 dark:bg-slate-900/95">
      <Wand2 className="h-4 w-4 text-brand-600 dark:text-brand-300" />
      <span className="text-[12px] font-medium text-slate-700 dark:text-slate-200">
        Proposed changes{parts.length ? `: ${parts.join(", ")}` : ""}
        {removedNames.length
          ? ` (removes ${removedNames.slice(0, 3).join(", ")}${removedNames.length > 3 ? "…" : ""})`
          : ""}
      </span>
      <button
        type="button"
        onClick={onAccept}
        className="rounded-md bg-emerald-600 px-3 py-1 text-[12px] font-semibold text-white hover:bg-emerald-700"
      >
        Accept
      </button>
      <button
        type="button"
        onClick={onReject}
        className="rounded-md border border-slate-300 px-3 py-1 text-[12px] font-semibold text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
      >
        Reject
      </button>
    </div>
  );
}
