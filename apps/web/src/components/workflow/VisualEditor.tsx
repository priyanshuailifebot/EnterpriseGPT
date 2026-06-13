"use client";

/**
 * Read-only visual editor — preview pane for an existing definition.
 *
 * Node + edge renderers are shared with the interactive canvas via
 * ``visual-editor-renderers``. This component just owns the layout
 * pipeline: positions, satellite slot placement, and the minimap.
 */

import "@xyflow/react/dist/style.css";

import {
  Background,
  Controls,
  type Edge,
  MiniMap,
  type Node,
  ReactFlow,
  useEdgesState,
  useNodesState,
} from "@xyflow/react";
import { useEffect, useMemo } from "react";

import {
  type FlowEdge,
  type FlowNodeData,
  type SatelliteEntry,
  satellitesByAgent,
  workflowToFlowGraph,
} from "@/components/workflow/workflow-topology";
import {
  AGENT_FLOW_NODE_TYPES,
  BRANCH_EDGE_TYPES,
  flowTypeForKind,
  type SatelliteFlowData,
} from "@/components/workflow/visual-editor-renderers";
import type { AgentNode, WorkflowDefinition } from "@/types/api";

// Spacing constants for the top-level grid and the satellite row beneath
// each parent agent.
const _COL_X = 300;
const _ROW_Y = 200;
const _SATELLITE_GAP = 130;
const _SATELLITE_DROP_Y = 200;

export function VisualEditor({
  definition,
  onNodeSelect,
}: {
  definition: WorkflowDefinition;
  onNodeSelect?: (nodeId: string | null) => void;
}) {
  const { nodes: topo, edges: topoEdges } = useMemo(
    () => workflowToFlowGraph(definition),
    [definition],
  );

  const satellites = useMemo(() => satellitesByAgent(definition), [definition]);

  const initialNodes = useMemo((): Node[] => {
    const out: Node[] = [];
    for (const n of topo) {
      let satelliteCount:
        | { tools: number; memory: boolean; parser: boolean; model: boolean }
        | undefined;
      if (n.data.kind === "agent") {
        const ag = n.data.raw as AgentNode;
        const entries = satellites.get(n.id) ?? [];
        satelliteCount = {
          tools: entries.filter((e: SatelliteEntry) => e.slot === "tool").length,
          memory:
            entries.some((e: SatelliteEntry) => e.slot === "memory") ||
            !!ag.memory_ref,
          parser:
            entries.some((e: SatelliteEntry) => e.slot === "output_parser") ||
            !!ag.output_parser_ref,
          model: !!ag.chat_model,
        };
      }
      out.push({
        id: n.id,
        type: flowTypeForKind(n.data.kind),
        position: { x: 48 + n.depth * _COL_X, y: 56 + n.orderInLevel * _ROW_Y },
        data:
          satelliteCount ? { ...(n.data as object), satelliteCount } : n.data,
      });

      if (n.data.kind !== "agent") continue;
      const ag = n.data.raw as AgentNode;
      const entries = satellites.get(n.id) ?? [];

      const modelEntry: { slot: "model"; node: { kind: "model"; label: string; provider: string } } | null =
        ag.chat_model
          ? {
              slot: "model",
              node: {
                kind: "model",
                label: ag.chat_model.model || "default",
                provider: ag.chat_model.provider || "openai",
              },
            }
          : null;

      type RenderedSat = {
        id: string;
        slot: SatelliteEntry["slot"];
        node: SatelliteFlowData["node"];
      };
      const rendered: RenderedSat[] = [];
      if (modelEntry) {
        rendered.push({
          id: `${n.id}__satellite__model`,
          slot: "model",
          node: modelEntry.node,
        });
      }
      for (const e of entries) {
        rendered.push({
          id: e.node.id,
          slot: e.slot,
          node: e.node,
        });
      }

      const totalWidth = rendered.length * _SATELLITE_GAP;
      const baseX = 48 + n.depth * _COL_X - (totalWidth - _SATELLITE_GAP) / 2 + 60;
      const baseY = 56 + n.orderInLevel * _ROW_Y + _SATELLITE_DROP_Y;
      rendered.forEach((sat, i) => {
        out.push({
          id: sat.id,
          type: "satelliteFlow",
          position: { x: baseX + i * _SATELLITE_GAP, y: baseY },
          draggable: false,
          data: {
            slot: sat.slot,
            node: sat.node,
            agentId: n.id,
          } satisfies SatelliteFlowData as unknown as Record<string, unknown>,
        });
      });
    }
    return out;
  }, [topo, satellites]);

  const initialEdges = useMemo((): Edge[] => {
    const out: Edge[] = topoEdges.map((e: FlowEdge) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      sourceHandle: "src",
      targetHandle: "tgt",
      type: "branch",
      animated: true,
      data: {
        branchLabel: e.branchLabel,
        fromForEach: e.fromForEach,
      },
    }));

    for (const [agentId, entries] of satellites.entries()) {
      const agentNode = topo.find((t) => t.id === agentId);
      const ag = agentNode?.data.raw as AgentNode | undefined;
      if (ag?.chat_model) {
        out.push({
          id: `${agentId}->satellite_model`,
          source: agentId,
          target: `${agentId}__satellite__model`,
          sourceHandle: "satellites",
          targetHandle: "tgt",
          type: "default",
          animated: false,
          style: { strokeDasharray: "4 4", stroke: "#94a3b8", strokeWidth: 1.2 },
        });
      }
      for (const e of entries) {
        out.push({
          id: `${agentId}->satellite_${e.node.id}`,
          source: agentId,
          target: e.node.id,
          sourceHandle: "satellites",
          targetHandle: "tgt",
          type: "default",
          animated: false,
          style: { strokeDasharray: "4 4", stroke: "#94a3b8", strokeWidth: 1.2 },
        });
      }
    }
    return out;
  }, [topo, topoEdges, satellites]);

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  useEffect(() => {
    setNodes(initialNodes);
    setEdges(initialEdges);
  }, [initialEdges, initialNodes, setEdges, setNodes]);

  return (
    <div className="reactflow-themed h-[560px] w-full rounded-3xl border border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-950">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        nodeTypes={AGENT_FLOW_NODE_TYPES}
        edgeTypes={BRANCH_EDGE_TYPES}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(_, n) => onNodeSelect?.(n.id)}
        onPaneClick={() => onNodeSelect?.(null)}
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable={!!onNodeSelect}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={28} />
        <Controls
          className="!rounded-xl !border !border-slate-200 !bg-white !shadow-lg dark:!border-slate-700 dark:!bg-slate-900"
          showInteractive={false}
        />
        <MiniMap
          className="!rounded-xl !border !border-slate-200 !bg-white dark:!border-slate-700 dark:!bg-slate-900"
          pannable={false}
          maskColor="rgba(15, 23, 42, 0.65)"
          nodeColor={(n) => {
            const data = n.data as unknown as FlowNodeData;
            switch (data?.kind) {
              case "trigger":
                return "#10b981";
              case "action":
                return "#0f172a";
              case "condition":
                return "#6366f1";
              case "if":
                return "#06b6d4";
              case "for_each":
                return "#059669";
              case "wait_for_webhook":
                return "#d97706";
              case "merge":
                return "#64748b";
              case "data_store":
                return "#475569";
              default:
                return data?.checkpoint ? "#f59e0b" : "#6366f1";
            }
          }}
          nodeStrokeColor="transparent"
          nodeBorderRadius={6}
        />
      </ReactFlow>
    </div>
  );
}
