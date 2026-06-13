import type {
  ActionNode,
  AgentDefinition,
  AgentNode,
  ConditionNode,
  DataStoreNode,
  ForEachNode,
  IfNode,
  MemoryNode,
  MergeNode,
  OutputParserNode,
  TriggerNode,
  WaitForWebhookNode,
  WorkflowDefinition,
  WorkflowNode,
} from "@/types/api";

/**
 * Topology helper for the visual editor.
 *
 * Produces a uniform list of flow nodes regardless of whether the source
 * definition uses legacy ``agents`` or the v2 ``nodes`` discriminator.
 * Outgoing edges from condition nodes carry their branch label so the
 * editor can render them inline. For-each ``body`` membership is exposed
 * on each member node via ``parentForEachId`` so the renderer can group
 * them in a faint container.
 */

export type FlowNodeKind =
  | "agent"
  | "action"
  | "condition"
  | "if"
  | "for_each"
  | "merge"
  | "wait_for_webhook"
  | "trigger"
  | "data_store"
  | "memory"
  | "output_parser";

/** Slot label used when a satellite is rendered under its parent agent. */
export type SatelliteSlot =
  | "model"
  | "memory"
  | "tool"
  | "output_parser";

export interface SatelliteEntry {
  node: WorkflowNode;
  slot: SatelliteSlot;
  /** Order within the agent's satellite row, stable across re-renders. */
  order: number;
}

export interface FlowNodeData {
  kind: FlowNodeKind;
  /** Original node payload — narrow on ``kind`` to access kind-specific fields. */
  raw: WorkflowNode;
  checkpoint: boolean;
  parentForEachId: string | null;
  // ``@xyflow/react`` types ``Node.data`` as ``Record<string, unknown>``. The
  // index signature here lets ``FlowNodeData`` satisfy that constraint
  // without forcing casts at every usage site.
  [k: string]: unknown;
}

export interface FlowEdge {
  id: string;
  source: string;
  target: string;
  /** Branch label when ``source`` is a condition node. */
  branchLabel?: string;
  /** Edges going INTO a for_each member's body get a "loop" hint. */
  fromForEach?: boolean;
}

export interface FlowLayout {
  nodes: {
    id: string;
    depth: number;
    orderInLevel: number;
    data: FlowNodeData;
  }[];
  edges: FlowEdge[];
}

// ---------------------------------------------------------------------------
// Public helpers
// ---------------------------------------------------------------------------

export function isAgentNode(n: WorkflowNode): n is AgentNode {
  return n.kind === "agent";
}
export function isConditionNode(n: WorkflowNode): n is ConditionNode {
  return n.kind === "condition";
}
export function isForEachNode(n: WorkflowNode): n is ForEachNode {
  return n.kind === "for_each";
}
export function isMergeNode(n: WorkflowNode): n is MergeNode {
  return n.kind === "merge";
}
export function isWaitNode(n: WorkflowNode): n is WaitForWebhookNode {
  return n.kind === "wait_for_webhook";
}
export function isActionNode(n: WorkflowNode): n is ActionNode {
  return n.kind === "action";
}
export function isIfNode(n: WorkflowNode): n is IfNode {
  return n.kind === "if";
}
export function isTriggerNode(n: WorkflowNode): n is TriggerNode {
  return n.kind === "trigger";
}
export function isDataStoreNode(n: WorkflowNode): n is DataStoreNode {
  return n.kind === "data_store";
}
export function isMemoryNode(n: WorkflowNode): n is MemoryNode {
  return n.kind === "memory";
}
export function isOutputParserNode(n: WorkflowNode): n is OutputParserNode {
  return n.kind === "output_parser";
}

/** Mirror of ``schemas.workflow._SATELLITE_KINDS`` on the server. */
export const SATELLITE_KINDS = new Set<FlowNodeKind>([
  "action",
  "data_store",
  "memory",
  "output_parser",
]);

/** Kinds that hold pure config and never appear in the executor's top-level walk. */
export const NON_EXECUTABLE_KINDS = new Set<FlowNodeKind>([
  "memory",
  "output_parser",
]);

export function isSatellite(n: WorkflowNode): boolean {
  if (!SATELLITE_KINDS.has(n.kind)) return false;
  // Cast for a uniform read of the field; not every kind declares it.
  const pid = (n as unknown as { parent_agent_id?: string | null }).parent_agent_id;
  return typeof pid === "string" && pid.length > 0;
}

/** Build the agent_id → satellite-list mapping. Memory satellites get the
 *  ``memory`` slot label, OutputParser → ``output_parser``, everything else
 *  → ``tool``. A synthetic ``model`` slot is appended when the agent has an
 *  explicit ``chat_model`` set; that satellite has no underlying node — the
 *  editor renders it from the agent's own ``chat_model`` field.
 */
export function satellitesByAgent(
  def: WorkflowDefinition,
): Map<string, SatelliteEntry[]> {
  const out = new Map<string, SatelliteEntry[]>();
  for (const n of unifiedNodes(def)) {
    if (!isSatellite(n)) continue;
    const pid = (n as unknown as { parent_agent_id?: string | null }).parent_agent_id ?? "";
    if (!pid) continue;
    const slot: SatelliteSlot =
      n.kind === "memory" ? "memory"
      : n.kind === "output_parser" ? "output_parser"
      : "tool";
    const list = out.get(pid) ?? [];
    list.push({ node: n, slot, order: list.length });
    out.set(pid, list);
  }
  return out;
}

/** Promote a legacy ``AgentDefinition`` into an ``AgentNode``. */

function _promoteAgent(a: AgentDefinition): AgentNode {
  return {
    kind: "agent",
    id: a.id,
    name: a.name,
    depends_on: a.depends_on,
    activate_on: a.activate_on ?? null,
    role: a.role,
    instructions: a.instructions,
    tools: a.tools,
    is_parallel: a.is_parallel,
  };
}

/** Returns the unified node list the editor renders from. */

export function unifiedNodes(def: WorkflowDefinition): WorkflowNode[] {
  if (def.nodes && def.nodes.length > 0) return def.nodes;
  return def.agents.map(_promoteAgent);
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

export function workflowToFlowGraph(def: WorkflowDefinition): FlowLayout {
  const allNodes = unifiedNodes(def);
  // Satellites are rendered under their parent agent, not in the
  // top-level flow. They never appear in the execution order, never
  // contribute edges, never count toward depth.
  const nodes = allNodes.filter((n) => !isSatellite(n));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const checkpointSet = new Set(def.human_checkpoints ?? []);

  // ---- parentForEachId map ------------------------------------------------
  // A for_each node owns its ``body`` nodes; flag them so the renderer can
  // draw a translucent container behind that subgraph.
  const parentOf = new Map<string, string>();
  for (const n of nodes) {
    if (isForEachNode(n)) {
      for (const child of n.body) parentOf.set(child, n.id);
    }
  }

  // ---- depth ---------------------------------------------------------------
  const depthMemo = new Map<string, number>();
  function depthOf(id: string): number {
    if (depthMemo.has(id)) return depthMemo.get(id)!;
    const n = byId.get(id);
    if (!n || n.depends_on.length === 0) {
      depthMemo.set(id, 0);
      return 0;
    }
    const dd = Math.max(...n.depends_on.map((d) => depthOf(d)));
    const next = dd + 1;
    depthMemo.set(id, next);
    return next;
  }

  const levelBuckets = new Map<number, string[]>();
  for (const n of nodes) {
    const L = depthOf(n.id);
    if (!levelBuckets.has(L)) levelBuckets.set(L, []);
    levelBuckets.get(L)!.push(n.id);
  }

  const levels = [...levelBuckets.keys()].sort((a, b) => a - b);

  const flowNodes: FlowLayout["nodes"] = [];
  for (const L of levels) {
    // Stable order within a level: nodes activated by an earlier-declared
    // branch sort before others. Otherwise alphabetical by name.
    const ids = [...(levelBuckets.get(L) ?? [])].sort((a, b) => {
      const na = byId.get(a)!;
      const nb = byId.get(b)!;
      const aBranch = na.activate_on ? Object.values(na.activate_on)[0] ?? "" : "";
      const bBranch = nb.activate_on ? Object.values(nb.activate_on)[0] ?? "" : "";
      if (aBranch !== bBranch) return aBranch.localeCompare(bBranch);
      return na.name.localeCompare(nb.name);
    });
    ids.forEach((id, idx) => {
      const node = byId.get(id)!;
      flowNodes.push({
        id,
        depth: L,
        orderInLevel: idx,
        data: {
          kind: node.kind,
          raw: node,
          checkpoint: checkpointSet.has(id),
          parentForEachId: parentOf.get(id) ?? null,
        },
      });
    });
  }

  // ---- edges ---------------------------------------------------------------
  const edges: FlowEdge[] = [];
  for (const n of nodes) {
    for (const dep of n.depends_on) {
      const source = byId.get(dep);
      const edge: FlowEdge = {
        id: `${dep}->${n.id}`,
        source: dep,
        target: n.id,
      };
      // Branch label: this edge originates from a condition / if node AND
      // the target declares an ``activate_on`` value for it.
      if (
        source &&
        (isConditionNode(source) || isIfNode(source)) &&
        n.activate_on?.[source.id]
      ) {
        edge.branchLabel = n.activate_on[source.id];
      }
      if (source && isForEachNode(source)) {
        edge.fromForEach = true;
      }
      edges.push(edge);
    }
  }

  return { nodes: flowNodes, edges };
}
