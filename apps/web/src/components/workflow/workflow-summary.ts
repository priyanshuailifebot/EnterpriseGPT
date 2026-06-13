/**
 * Deterministic, zero-cost plain-English summaries for a workflow node.
 *
 * Used as the instant fallback in the property inspector before (or instead
 * of) the LLM-generated detailed summary. Built purely from the node's own
 * fields plus the names of nodes it references, so it never hits the network
 * and always reflects the user's latest edit.
 */

import type { WorkflowDefinition, WorkflowNode } from "@/types/api";

import { allNodes } from "./workflow-mutations";

function nameMap(definition: WorkflowDefinition): Map<string, string> {
  const m = new Map<string, string>();
  for (const n of allNodes(definition)) m.set(n.id, n.name || n.id);
  return m;
}

function labelList(ids: string[], names: Map<string, string>): string {
  const labels = ids.map((id) => names.get(id) ?? id);
  if (labels.length === 0) return "";
  if (labels.length === 1) return labels[0];
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`;
  return `${labels.slice(0, -1).join(", ")}, and ${labels[labels.length - 1]}`;
}

function dependsClause(node: WorkflowNode, names: Map<string, string>): string {
  if (node.depends_on.length === 0) return "";
  return ` It runs after ${labelList(node.depends_on, names)}.`;
}

function activateClause(node: WorkflowNode, names: Map<string, string>): string {
  const act = node.activate_on;
  if (!act) return "";
  const parts = Object.entries(act).map(
    ([upstream, branch]) => `${names.get(upstream) ?? upstream} = "${branch}"`,
  );
  if (parts.length === 0) return "";
  return ` Only active when ${parts.join(" and ")}.`;
}

/**
 * Return a 1–3 sentence plain-English description of ``node`` in the context
 * of ``definition``. Never throws — unknown kinds fall back to a generic line.
 */
export function summarizeNode(
  node: WorkflowNode,
  definition: WorkflowDefinition,
): string {
  const names = nameMap(definition);
  const after = dependsClause(node, names);
  const gate = activateClause(node, names);

  switch (node.kind) {
    case "trigger": {
      const t = node.trigger_type;
      const how =
        t === "chat"
          ? "when a user starts a chat conversation"
          : t === "webhook"
            ? "when an external system sends a webhook"
            : t === "form"
              ? "when someone submits the public form"
              : t === "schedule"
                ? `on a schedule${node.schedule_cron ? ` (${node.schedule_cron})` : ""}`
                : "when run manually";
      return `Starts the workflow ${how}. This is the entry point — everything else runs after it.`;
    }

    case "agent": {
      const toolNote =
        node.tools && node.tools.length > 0
          ? ` It can use ${node.tools.length} tool${node.tools.length === 1 ? "" : "s"} (${labelList(node.tools, names) || node.tools.join(", ")}).`
          : " It answers directly without calling external tools.";
      const role = node.role ? ` acting as “${node.role.trim()}”` : "";
      const parallel = node.is_parallel ? " Runs in parallel with its siblings." : "";
      return `An AI agent${role} that reasons over the incoming data and produces a result.${toolNote}${after}${gate}${parallel}`;
    }

    case "action": {
      const what = node.action_slug
        ? `the “${node.action_slug}” action`
        : "an action";
      const prov = node.provider ? ` on ${node.provider}` : "";
      const dry = node.allow_dry_run
        ? " If no connection is configured it runs in dry-run (echo) mode."
        : "";
      return `Calls ${what}${prov} — a direct integration call, no AI.${after}${gate}${dry}`;
    }

    case "condition": {
      const branches =
        node.branches.length > 0
          ? ` It picks one of: ${node.branches.join(", ")}.`
          : "";
      const expr = node.expression ? ` Decision rule: ${node.expression.trim()}` : "";
      return `An AI-evaluated router that sends the flow down a branch.${branches}${expr ? `${expr}` : ""}${after}`;
    }

    case "if": {
      const expr = node.expression ? ` Condition: ${node.expression.trim()}.` : "";
      return `A deterministic true/false branch.${expr} Downstream nodes gate on the “true” or “false” outcome.${after}`;
    }

    case "for_each": {
      const src = node.items_from
        ? `each item from ${names.get(node.items_from) ?? node.items_from}`
        : "each item in a list";
      const body =
        node.body.length > 0
          ? ` For every item it runs ${labelList(node.body, names)}.`
          : "";
      const conc = node.max_concurrency
        ? ` Up to ${node.max_concurrency} run at once.`
        : "";
      return `Loops over ${src}.${body}${conc}${after}`;
    }

    case "merge":
      return `A join point — waits for ${labelList(node.depends_on, names) || "its upstream branches"} to finish, then passes their combined output to the next step.`;

    case "wait_for_webhook": {
      const desc = node.description ? ` ${node.description.trim()}` : "";
      const to = node.timeout_seconds
        ? ` Times out after ${node.timeout_seconds} seconds.`
        : "";
      return `Pauses the workflow until an external HTTP callback arrives.${desc}${to}${after}`;
    }

    case "data_store": {
      const verb =
        node.op === "write"
          ? "Writes to"
          : node.op === "read"
            ? "Reads from"
            : "Queries";
      const tbl = node.table ? ` the “${node.table}” table` : " a workspace table";
      return `${verb}${tbl} (no database setup needed).${after}${gate}`;
    }

    case "memory":
      return `Stores conversation/state memory (${node.scope} scope, kept in ${node.store}). Attached to an agent so it remembers across turns; it is not a step in the run.`;

    case "output_parser":
      return `Validates an agent's output against a JSON schema and retries up to ${node.max_retries} time${node.max_retries === 1 ? "" : "s"} if it doesn't conform. Attached to an agent, not a run step.`;

    default:
      return `A ${(node as WorkflowNode).kind} node.${after}${gate}`;
  }
}
