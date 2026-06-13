/**
 * Node-level diff between two workflow definitions, used to highlight an
 * AI-proposed graph on the canvas before the user accepts it.
 *
 * The backend's ``augment`` response already ships a human-readable
 * ``changes`` list; this computes the id-level sets the canvas needs to paint
 * added / removed / modified rings.
 */

import type { WorkflowDefinition, WorkflowNode } from "@/types/api";

import { allNodes } from "./workflow-mutations";

export interface NodeDiff {
  added: Set<string>;
  removed: Set<string>;
  modified: Set<string>;
}

export const EMPTY_DIFF: NodeDiff = {
  added: new Set(),
  removed: new Set(),
  modified: new Set(),
};

function byId(def: WorkflowDefinition): Map<string, WorkflowNode> {
  const m = new Map<string, WorkflowNode>();
  for (const n of allNodes(def)) m.set(n.id, n);
  return m;
}

/** Stable-ish stringify: JSON key order is insertion order, which is stable
 *  for the interpreter's output, so a plain compare is good enough to flag a
 *  node as "modified". False positives only cost an extra amber ring. */
function sameNode(a: WorkflowNode, b: WorkflowNode): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function diffDefinitions(
  before: WorkflowDefinition,
  after: WorkflowDefinition,
): NodeDiff {
  const b = byId(before);
  const a = byId(after);
  const added = new Set<string>();
  const removed = new Set<string>();
  const modified = new Set<string>();

  for (const id of a.keys()) {
    if (!b.has(id)) added.add(id);
    else if (!sameNode(b.get(id)!, a.get(id)!)) modified.add(id);
  }
  for (const id of b.keys()) {
    if (!a.has(id)) removed.add(id);
  }
  return { added, removed, modified };
}

export function diffClassName(id: string, diff: NodeDiff | null): string | undefined {
  if (!diff) return undefined;
  if (diff.added.has(id)) return "egpt-diff-added";
  if (diff.removed.has(id)) return "egpt-diff-removed";
  if (diff.modified.has(id)) return "egpt-diff-modified";
  return undefined;
}

export function diffIsEmpty(diff: NodeDiff): boolean {
  return diff.added.size === 0 && diff.removed.size === 0 && diff.modified.size === 0;
}
