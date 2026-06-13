/**
 * Pure mutation helpers for ``WorkflowDefinition``.
 *
 * Every helper here is **pure** — it accepts the current definition,
 * returns a new one, and never mutates input. The interactive canvas
 * and the property inspector both consume these so all canvas state
 * changes flow through one well-tested code path. Validation (cycle
 * detection, satellite legality, etc.) is enforced by the backend
 * Pydantic schema on save — these helpers maintain *structural* sanity
 * (id uniqueness, dangling-ref cleanup) so the in-progress graph stays
 * presentable while the user is editing.
 */

import { produce } from "immer";

import type {
  ActionNode,
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

// ---------------------------------------------------------------------------
// Constants — node-kind catalog the palette consumes
// ---------------------------------------------------------------------------

export type NodeKind = WorkflowNode["kind"];

export interface NodeKindCatalogEntry {
  kind: NodeKind;
  category: "trigger" | "logic" | "agent" | "tool" | "data";
  label: string;
  description: string;
}

/**
 * Ordered catalog driving the node palette. The order here is intentional —
 * matches the left-to-right flow users expect (trigger first, control flow,
 * agents, then tools/data sinks).
 */
export const NODE_KIND_CATALOG: NodeKindCatalogEntry[] = [
  {
    kind: "trigger",
    category: "trigger",
    label: "Trigger",
    description: "Starts the workflow — manual, webhook, form, chat, or schedule.",
  },
  {
    kind: "agent",
    category: "agent",
    label: "Agent",
    description: "LLM agent with optional tools, memory, and output parser.",
  },
  {
    kind: "action",
    category: "tool",
    label: "Action",
    description: "Atomic integration call (Gmail, Slack, HTTP, etc.).",
  },
  {
    kind: "condition",
    category: "logic",
    label: "Condition",
    description: "LLM-routed branch with named labels.",
  },
  {
    kind: "if",
    category: "logic",
    label: "If",
    description: "Deterministic boolean branch over JSON values.",
  },
  {
    kind: "for_each",
    category: "logic",
    label: "For Each",
    description: "Fan out over a list produced upstream.",
  },
  {
    kind: "merge",
    category: "logic",
    label: "Merge",
    description: "Join point — outputs a dict keyed by upstream id.",
  },
  {
    kind: "wait_for_webhook",
    category: "logic",
    label: "Wait for Webhook",
    description: "Pause until an external HTTP POST resumes the workflow.",
  },
  {
    kind: "data_store",
    category: "data",
    label: "Data Store",
    description: "Read/write a workspace JSONB table.",
  },
  {
    kind: "memory",
    category: "tool",
    label: "Memory",
    description: "Conversation memory satellite for an agent.",
  },
  {
    kind: "output_parser",
    category: "tool",
    label: "Output Parser",
    description: "JSON-schema enforcer attached to an agent.",
  },
];

// ---------------------------------------------------------------------------
// ID generator
// ---------------------------------------------------------------------------

/**
 * Generate a snake_case id derived from a label, guaranteed unique within
 * the given definition. Falls back to a numeric suffix on collision.
 */
export function uniqueIdFrom(label: string, defn: WorkflowDefinition): string {
  const existing = new Set<string>();
  for (const n of allNodes(defn)) existing.add(n.id);
  const base =
    (label || "node")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "") || "node";
  if (!existing.has(base)) return base;
  for (let i = 2; i < 1000; i += 1) {
    const cand = `${base}_${i}`;
    if (!existing.has(cand)) return cand;
  }
  // Pragmatic fallback — should never trigger.
  return `${base}_${Date.now()}`;
}

// ---------------------------------------------------------------------------
// View helpers
// ---------------------------------------------------------------------------

/**
 * Return the unified node list — promoting legacy ``agents`` to ``AgentNode``
 * shape when the v2 ``nodes`` array is empty. Mirrors ``iter_nodes`` on the
 * backend so the editor always sees one consistent shape.
 */
export function allNodes(defn: WorkflowDefinition): WorkflowNode[] {
  if (defn.nodes && defn.nodes.length > 0) return defn.nodes;
  return (defn.agents ?? []).map(
    (a): AgentNode => ({
      kind: "agent",
      id: a.id,
      name: a.name,
      depends_on: [...a.depends_on],
      activate_on: a.activate_on ?? null,
      role: a.role,
      instructions: a.instructions,
      tools: [...a.tools],
      is_parallel: a.is_parallel,
      memory_ref: "",
      output_parser_ref: "",
      chat_model: null,
    }),
  );
}

export function findNode(
  defn: WorkflowDefinition,
  id: string,
): WorkflowNode | null {
  for (const n of allNodes(defn)) if (n.id === id) return n;
  return null;
}

// ---------------------------------------------------------------------------
// Factories — defaults per node kind
// ---------------------------------------------------------------------------

export function makeBlankNode(kind: NodeKind, id: string, name: string): WorkflowNode {
  switch (kind) {
    case "trigger":
      return {
        kind: "trigger",
        id,
        name,
        depends_on: [],
        activate_on: null,
        trigger_type: "manual",
        slug: id,
        form_fields: [],
        schedule_cron: "",
        secret_required: false,
        chat_welcome_message: "",
        chat_memory_ref: "",
      } satisfies TriggerNode;
    case "agent":
      return {
        kind: "agent",
        id,
        name,
        depends_on: [],
        activate_on: null,
        role: "",
        instructions: "",
        tools: [],
        is_parallel: false,
        memory_ref: "",
        output_parser_ref: "",
        chat_model: null,
      } satisfies AgentNode;
    case "action":
      return {
        kind: "action",
        id,
        name,
        depends_on: [],
        activate_on: null,
        provider: "http_bearer",
        action_slug: "http_get",
        params: {},
        allow_dry_run: true,
        parent_agent_id: null,
        tool_description: "",
      } satisfies ActionNode;
    case "condition":
      return {
        kind: "condition",
        id,
        name,
        depends_on: [],
        activate_on: null,
        expression: "Describe the rubric here.",
        branches: ["true", "false"],
      } satisfies ConditionNode;
    case "if":
      return {
        kind: "if",
        id,
        name,
        depends_on: [],
        activate_on: null,
        expression: "$.upstream.value > 0",
      } satisfies IfNode;
    case "for_each":
      return {
        kind: "for_each",
        id,
        name,
        depends_on: [],
        activate_on: null,
        items_from: "",
        items_path: "$",
        item_var: "item",
        body: [],
        max_concurrency: 4,
      } satisfies ForEachNode;
    case "merge":
      return {
        kind: "merge",
        id,
        name,
        depends_on: [],
        activate_on: null,
      } satisfies MergeNode;
    case "wait_for_webhook":
      return {
        kind: "wait_for_webhook",
        id,
        name,
        depends_on: [],
        activate_on: null,
        description: "",
        timeout_seconds: 86400,
        response_schema: null,
      } satisfies WaitForWebhookNode;
    case "data_store":
      return {
        kind: "data_store",
        id,
        name,
        depends_on: [],
        activate_on: null,
        op: "write",
        table: "default_table",
        key: "",
        payload: {},
        filter: {},
        parent_agent_id: null,
        tool_description: "",
      } satisfies DataStoreNode;
    case "memory":
      return {
        kind: "memory",
        id,
        name,
        depends_on: [],
        activate_on: null,
        scope: "session",
        store: "redis",
        ttl_seconds: 3600,
        max_turns: 24,
        parent_agent_id: null,
      } satisfies MemoryNode;
    case "output_parser":
      return {
        kind: "output_parser",
        id,
        name,
        depends_on: [],
        activate_on: null,
        json_schema: {},
        max_retries: 2,
        parent_agent_id: null,
      } satisfies OutputParserNode;
    default: {
      const _exhaustive: never = kind;
      throw new Error(`Unknown node kind: ${String(_exhaustive)}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

/** Insert a new node. The caller chooses id/name; pass ``uniqueIdFrom`` results. */
export function addNode(
  defn: WorkflowDefinition,
  node: WorkflowNode,
): WorkflowDefinition {
  return produce(defn, (draft) => {
    if (!draft.nodes) draft.nodes = [];
    // Migrate legacy agents → nodes once on first mutation so the editor
    // always works against the v2 shape.
    if (draft.nodes.length === 0 && draft.agents && draft.agents.length > 0) {
      draft.nodes = allNodes(defn);
      draft.agents = [];
    }
    draft.nodes.push(node);
  });
}

/**
 * Remove a node and prune dangling references in other nodes.
 *
 * Cleans up:
 *   - ``depends_on`` arrays that reference the removed id
 *   - ``activate_on`` keys that reference the removed id
 *   - ``for_each.body`` entries
 *   - ``for_each.items_from`` pointers (cleared if matched)
 *   - ``agent.memory_ref`` / ``agent.output_parser_ref``
 *   - ``trigger.chat_memory_ref``
 *   - satellite ``parent_agent_id`` (when removing an agent — orphans the
 *     satellite by clearing the parent reference, which is still valid
 *     since satellites without parents become standalone nodes)
 *   - ``human_checkpoints`` references
 */
export function removeNode(
  defn: WorkflowDefinition,
  id: string,
): WorkflowDefinition {
  return produce(defn, (draft) => {
    if (!draft.nodes) draft.nodes = allNodes(defn);
    draft.nodes = draft.nodes.filter((n) => n.id !== id);
    draft.agents = (draft.agents ?? []).filter((a) => a.id !== id);

    for (const n of draft.nodes) {
      n.depends_on = n.depends_on.filter((d) => d !== id);
      if (n.activate_on) {
        const clean: Record<string, string> = {};
        for (const [k, v] of Object.entries(n.activate_on)) {
          if (k !== id) clean[k] = v;
        }
        n.activate_on = Object.keys(clean).length > 0 ? clean : null;
      }
      if (n.kind === "for_each") {
        n.body = n.body.filter((b) => b !== id);
        if (n.items_from === id) n.items_from = "";
      }
      if (n.kind === "agent") {
        if (n.memory_ref === id) n.memory_ref = "";
        if (n.output_parser_ref === id) n.output_parser_ref = "";
      }
      if (n.kind === "trigger" && n.chat_memory_ref === id) {
        n.chat_memory_ref = "";
      }
      if (
        (n.kind === "action" ||
          n.kind === "data_store" ||
          n.kind === "memory" ||
          n.kind === "output_parser") &&
        n.parent_agent_id === id
      ) {
        n.parent_agent_id = null;
      }
    }
    draft.human_checkpoints = (draft.human_checkpoints ?? []).filter(
      (c) => c !== id,
    );
  });
}

/**
 * Connect ``source`` → ``target`` by adding ``source`` to ``target.depends_on``.
 *
 * Refuses obvious cycles (target already reachable from source) and
 * silently no-ops if the edge already exists. Self-loops are rejected.
 */
export function connectNodes(
  defn: WorkflowDefinition,
  source: string,
  target: string,
): WorkflowDefinition {
  if (source === target) return defn;
  if (wouldCreateCycle(defn, source, target)) return defn;
  return produce(defn, (draft) => {
    if (!draft.nodes) draft.nodes = allNodes(defn);
    const tgt = draft.nodes.find((n) => n.id === target);
    if (!tgt) return;
    if (!tgt.depends_on.includes(source)) {
      tgt.depends_on.push(source);
    }
  });
}

/** Remove the dependency edge ``source → target``. */
export function disconnectNodes(
  defn: WorkflowDefinition,
  source: string,
  target: string,
): WorkflowDefinition {
  return produce(defn, (draft) => {
    if (!draft.nodes) draft.nodes = allNodes(defn);
    const tgt = draft.nodes.find((n) => n.id === target);
    if (!tgt) return;
    tgt.depends_on = tgt.depends_on.filter((d) => d !== source);
  });
}

/**
 * Patch an existing node's fields. The patch is shallow-merged into the
 * existing payload (deep keys like ``params`` are replaced wholesale —
 * the inspector controls them as a single JSON blob).
 */
export function patchNode<T extends WorkflowNode>(
  defn: WorkflowDefinition,
  id: string,
  patch: Partial<T>,
): WorkflowDefinition {
  return produce(defn, (draft) => {
    if (!draft.nodes) draft.nodes = allNodes(defn);
    const target = draft.nodes.find((n) => n.id === id);
    if (!target) return;
    Object.assign(target, patch);
  });
}

/** Replace a node's id everywhere it's referenced. Useful when the user
 *  renames a node via the inspector — keeps the graph consistent in one shot. */
export function renameNodeId(
  defn: WorkflowDefinition,
  oldId: string,
  newId: string,
): WorkflowDefinition {
  if (oldId === newId || !/^[a-zA-Z0-9_-]+$/.test(newId)) return defn;
  if (findNode(defn, newId)) return defn; // collision; ignore
  return produce(defn, (draft) => {
    if (!draft.nodes) draft.nodes = allNodes(defn);
    for (const n of draft.nodes) {
      if (n.id === oldId) n.id = newId;
      n.depends_on = n.depends_on.map((d) => (d === oldId ? newId : d));
      if (n.activate_on) {
        const remapped: Record<string, string> = {};
        for (const [k, v] of Object.entries(n.activate_on)) {
          remapped[k === oldId ? newId : k] = v;
        }
        n.activate_on = remapped;
      }
      if (n.kind === "for_each") {
        if (n.items_from === oldId) n.items_from = newId;
        n.body = n.body.map((b) => (b === oldId ? newId : b));
      }
      if (n.kind === "agent") {
        if (n.memory_ref === oldId) n.memory_ref = newId;
        if (n.output_parser_ref === oldId) n.output_parser_ref = newId;
      }
      if (n.kind === "trigger" && n.chat_memory_ref === oldId) {
        n.chat_memory_ref = newId;
      }
      if (
        (n.kind === "action" ||
          n.kind === "data_store" ||
          n.kind === "memory" ||
          n.kind === "output_parser") &&
        n.parent_agent_id === oldId
      ) {
        n.parent_agent_id = newId;
      }
    }
    draft.human_checkpoints = (draft.human_checkpoints ?? []).map((c) =>
      c === oldId ? newId : c,
    );
  });
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

export interface ValidationIssue {
  nodeId: string | null;
  message: string;
  severity: "error" | "warning";
}

/**
 * Lightweight client-side validation that mirrors the most-likely-to-trip
 * Pydantic rules — duplicate ids, dangling refs, cycles, satellites with
 * depends_on. The backend still has the final word on save; this gives
 * the user inline feedback before they POST.
 */
export function validateDefinition(defn: WorkflowDefinition): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  const nodes = allNodes(defn);
  if (nodes.length === 0) {
    issues.push({
      nodeId: null,
      message: "Workflow needs at least one node.",
      severity: "error",
    });
    return issues;
  }

  const idCount = new Map<string, number>();
  for (const n of nodes) idCount.set(n.id, (idCount.get(n.id) ?? 0) + 1);
  for (const [id, c] of idCount) {
    if (c > 1) {
      issues.push({
        nodeId: id,
        message: `Duplicate node id: ${id}`,
        severity: "error",
      });
    }
  }

  const ids = new Set(nodes.map((n) => n.id));
  const satelliteIds = new Set(
    nodes
      .filter((n) =>
        (n.kind === "action" ||
          n.kind === "data_store" ||
          n.kind === "memory" ||
          n.kind === "output_parser") &&
        n.parent_agent_id,
      )
      .map((n) => n.id),
  );

  for (const n of nodes) {
    for (const d of n.depends_on) {
      if (!ids.has(d)) {
        issues.push({
          nodeId: n.id,
          message: `${n.id} depends on missing node "${d}"`,
          severity: "error",
        });
      }
      if (satelliteIds.has(d)) {
        issues.push({
          nodeId: n.id,
          message: `${n.id} depends on satellite "${d}" — not allowed`,
          severity: "error",
        });
      }
    }
    if (
      (n.kind === "action" ||
        n.kind === "data_store" ||
        n.kind === "memory" ||
        n.kind === "output_parser") &&
      n.parent_agent_id
    ) {
      if (n.depends_on.length > 0) {
        issues.push({
          nodeId: n.id,
          message: `Satellite "${n.id}" cannot declare depends_on`,
          severity: "error",
        });
      }
      const parent = nodes.find((m) => m.id === n.parent_agent_id);
      if (!parent || parent.kind !== "agent") {
        issues.push({
          nodeId: n.id,
          message: `parent_agent_id "${n.parent_agent_id}" must reference an Agent`,
          severity: "error",
        });
      }
    }
    if (n.kind === "agent" && n.memory_ref) {
      const t = nodes.find((m) => m.id === n.memory_ref);
      if (!t || t.kind !== "memory") {
        issues.push({
          nodeId: n.id,
          message: `agent.memory_ref must reference a Memory node`,
          severity: "error",
        });
      }
    }
    if (n.kind === "agent" && n.output_parser_ref) {
      const t = nodes.find((m) => m.id === n.output_parser_ref);
      if (!t || t.kind !== "output_parser") {
        issues.push({
          nodeId: n.id,
          message: `agent.output_parser_ref must reference an Output Parser node`,
          severity: "error",
        });
      }
    }
    if (n.kind === "for_each") {
      if (n.items_from && !ids.has(n.items_from)) {
        issues.push({
          nodeId: n.id,
          message: `for_each.items_from "${n.items_from}" not found`,
          severity: "error",
        });
      }
      for (const b of n.body) {
        if (!ids.has(b)) {
          issues.push({
            nodeId: n.id,
            message: `for_each.body references missing node "${b}"`,
            severity: "error",
          });
        }
      }
    }
  }

  if (hasCycle(defn)) {
    issues.push({
      nodeId: null,
      message: "Graph contains a cycle.",
      severity: "error",
    });
  }
  return issues;
}

// ---------------------------------------------------------------------------
// Graph algorithms
// ---------------------------------------------------------------------------

function buildAdjacency(defn: WorkflowDefinition): Map<string, Set<string>> {
  // outgoing[a] = set of nodes b such that b.depends_on includes a
  const out = new Map<string, Set<string>>();
  const nodes = allNodes(defn);
  for (const n of nodes) out.set(n.id, new Set());
  for (const n of nodes) {
    for (const d of n.depends_on) {
      const set = out.get(d);
      if (set) set.add(n.id);
    }
  }
  return out;
}

function hasCycle(defn: WorkflowDefinition): boolean {
  const adj = buildAdjacency(defn);
  const visiting = new Set<string>();
  const visited = new Set<string>();
  function dfs(node: string): boolean {
    if (visiting.has(node)) return true;
    if (visited.has(node)) return false;
    visiting.add(node);
    for (const next of adj.get(node) ?? []) {
      if (dfs(next)) return true;
    }
    visiting.delete(node);
    visited.add(node);
    return false;
  }
  for (const id of adj.keys()) {
    if (dfs(id)) return true;
  }
  return false;
}

function wouldCreateCycle(
  defn: WorkflowDefinition,
  source: string,
  target: string,
): boolean {
  // Adding source → target creates a cycle iff source is reachable FROM target
  // following outgoing edges (because target → ... → source already exists).
  const adj = buildAdjacency(defn);
  const stack = [target];
  const seen = new Set<string>();
  while (stack.length > 0) {
    const cur = stack.pop()!;
    if (cur === source) return true;
    if (seen.has(cur)) continue;
    seen.add(cur);
    for (const next of adj.get(cur) ?? []) stack.push(next);
  }
  return false;
}

// ---------------------------------------------------------------------------
// Layout — simple longest-path tier assignment used for auto-layout.
// ---------------------------------------------------------------------------

export interface NodeLayout {
  id: string;
  depth: number;
  orderInLevel: number;
}

/** Compute a left-to-right layered layout. Satellites are NOT included
 *  — they're positioned visually beneath their parent agent by the canvas. */
export function autoLayout(defn: WorkflowDefinition): Map<string, NodeLayout> {
  const nodes = allNodes(defn).filter((n) => {
    if (
      (n.kind === "action" ||
        n.kind === "data_store" ||
        n.kind === "memory" ||
        n.kind === "output_parser") &&
      n.parent_agent_id
    ) {
      return false;
    }
    return true;
  });
  const ids = new Set(nodes.map((n) => n.id));
  const depth = new Map<string, number>();
  // Iterate until stable — guaranteed to converge for acyclic graphs.
  for (const n of nodes) depth.set(n.id, 0);
  let changed = true;
  while (changed) {
    changed = false;
    for (const n of nodes) {
      let d = 0;
      for (const dep of n.depends_on) {
        if (!ids.has(dep)) continue;
        d = Math.max(d, (depth.get(dep) ?? 0) + 1);
      }
      if (d !== depth.get(n.id)) {
        depth.set(n.id, d);
        changed = true;
      }
    }
  }
  // Assign order within each depth tier.
  const tiers = new Map<number, string[]>();
  for (const [id, d] of depth) {
    if (!tiers.has(d)) tiers.set(d, []);
    tiers.get(d)!.push(id);
  }
  const out = new Map<string, NodeLayout>();
  for (const [d, ids] of tiers) {
    ids.sort();
    ids.forEach((id, i) => out.set(id, { id, depth: d, orderInLevel: i }));
  }
  return out;
}
