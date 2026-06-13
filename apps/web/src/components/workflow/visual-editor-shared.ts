/**
 * Shared node-renderer + edge-renderer maps consumed by both the
 * read-only ``VisualEditor`` and the interactive ``InteractiveCanvas``.
 *
 * Re-exports the React Flow ``nodeTypes`` / ``edgeTypes`` so the
 * interactive canvas inherits the exact same visual treatment without
 * duplicating component definitions. The read-only editor still owns
 * its layout pipeline; we only share the per-node renderers.
 */

import type { Edge, EdgeProps, Node, NodeProps } from "@xyflow/react";

import { satellitesByAgent, type FlowNodeData } from "./workflow-topology";
import type { AgentNode, WorkflowDefinition, WorkflowNode } from "@/types/api";

// We import the renderers from the existing VisualEditor file via a
// re-export shim. The renderers are stable read-only components — the
// interactive canvas just wants to render the same shapes, not change
// them.
//
// To avoid a circular import (VisualEditor imports from this module
// for the *types* it exposes back), we factor the renderer map import
// to a relative lazy module path.
export {
  AGENT_FLOW_NODE_TYPES,
  BRANCH_EDGE_TYPES,
  flowTypeForKind,
} from "./visual-editor-renderers";

export interface SharedFlowNode extends Node {
  data: FlowNodeData & Record<string, unknown>;
}

export interface SharedFlowEdge extends Edge {
  data?: { branchLabel?: string; fromForEach?: boolean };
}

import type { NodeRunState } from "./execution-status";

/**
 * Compute the ``FlowNodeData`` blob each node renderer expects.
 *
 * For agent nodes we additionally compute the satellite badge counts
 * (memory/tools/parser/model) so the card can render its pills the same
 * way the read-only editor does. When ``runStatus`` is provided the
 * renderer paints a status ring (running blue / done green / error red
 * / waiting amber / skipped grey).
 */
export function toFlowNodeData(
  node: WorkflowNode,
  defn: WorkflowDefinition,
  runStatus?: NodeRunState,
): FlowNodeData & Record<string, unknown> {
  const base: FlowNodeData & Record<string, unknown> = {
    kind: node.kind,
    raw: node,
    checkpoint: (defn.human_checkpoints ?? []).includes(node.id),
    parentForEachId: null,
  };
  if (runStatus) base.runStatus = runStatus;
  if (node.kind !== "agent") return base;
  const sats = satellitesByAgent(defn).get(node.id) ?? [];
  const ag = node as AgentNode;
  const satelliteCount = {
    tools: sats.filter((s) => s.slot === "tool").length,
    memory: sats.some((s) => s.slot === "memory") || !!ag.memory_ref,
    parser:
      sats.some((s) => s.slot === "output_parser") || !!ag.output_parser_ref,
    model: !!ag.chat_model,
  };
  return { ...base, satelliteCount };
}

export type { NodeProps, EdgeProps };
