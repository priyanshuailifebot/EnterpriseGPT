/**
 * Pure reducer mapping ``ExecutionEvent`` streams to per-node UI status.
 *
 * The canvas overlay subscribes to this so each node renderer can apply
 * a status ring (idle / running / done / error / skipped). The reducer
 * is pure so the unit tests can replay a recorded event stream and
 * assert the final state without touching React.
 *
 * Events the reducer cares about:
 *   - ``trigger_fired``                 → trigger node DONE
 *   - ``agent_start``                   → agent node RUNNING
 *   - ``agent_complete``                → agent node DONE
 *   - ``action_invoked``                → action node RUNNING (top-level)
 *   - ``action_result|action_dry_run``  → action node DONE
 *   - ``data_store_op``                 → data_store node DONE
 *   - ``condition_decided|if_decided``  → router node DONE + branch label
 *   - ``for_each_started``              → for_each node RUNNING
 *   - ``for_each_complete``             → for_each node DONE
 *   - ``wait_for_webhook``              → wait node RUNNING
 *   - ``webhook_resumed``               → wait node DONE
 *   - ``node_skipped``                  → addressed node SKIPPED
 *   - ``tool_call|tool_result``         → satellite node RUNNING / DONE
 *   - ``error``                         → either targeted node or graph-level
 *   - ``workflow_complete``             → overall status COMPLETE
 */

import type { ExecutionEvent } from "@/types/api";

export type NodeRunStatus =
  | "idle"
  | "running"
  | "done"
  | "error"
  | "skipped"
  | "waiting";

export type GraphRunStatus =
  | "idle"
  | "running"
  | "complete"
  | "error"
  | "awaiting_human";

export interface NodeRunState {
  status: NodeRunStatus;
  /** Mini human-friendly label rendered under the status ring (e.g.
   *  the branch a Condition picked). */
  label?: string;
  /** ISO timestamp of the last event affecting this node. */
  lastEventAt?: string;
  /** Set on error; otherwise undefined. */
  errorMessage?: string;
  /** Per-node inspection data, populated by ``node_complete``. */
  nodeKind?: string;
  inputSnapshot?: unknown;
  outputSnapshot?: unknown;
  durationMs?: number;
  dryRun?: boolean;
}

export interface ExecutionRunState {
  /** Overall workflow status. */
  graphStatus: GraphRunStatus;
  /** Per-node runtime state, keyed by node id. */
  nodes: Record<string, NodeRunState>;
  /** Synthetic execution id (from ``workflow_start``) or the real one. */
  executionId: string | null;
  /** Number of events received — handy for debugging. */
  eventCount: number;
  /** Graph-level error message (when ``graphStatus === "error"``). */
  errorMessage?: string;
}

export const INITIAL_EXECUTION_STATE: ExecutionRunState = {
  graphStatus: "idle",
  nodes: {},
  executionId: null,
  eventCount: 0,
};

/**
 * Apply a single event and return the next state. Pure — the caller
 * (zustand store or React reducer) decides how to persist it.
 */
export function applyExecutionEvent(
  state: ExecutionRunState,
  event: ExecutionEvent,
): ExecutionRunState {
  const next: ExecutionRunState = {
    ...state,
    nodes: { ...state.nodes },
    eventCount: state.eventCount + 1,
  };
  const ts = new Date().toISOString();

  // Heartbeats only update the keep-alive counter.
  if (event.type === "heartbeat") return next;

  if (event.type === "workflow_start") {
    next.graphStatus = "running";
    next.executionId = event.execution_id ?? null;
    return next;
  }

  if (event.type === "workflow_complete") {
    next.graphStatus = "complete";
    return next;
  }

  if (event.type === "error") {
    if (event.agent_id) {
      next.nodes[event.agent_id] = {
        ...next.nodes[event.agent_id],
        status: "error",
        errorMessage: event.message ?? undefined,
        lastEventAt: ts,
      };
    } else {
      next.graphStatus = "error";
      next.errorMessage = event.message ?? undefined;
    }
    return next;
  }

  if (event.type === "hitl_required") {
    next.graphStatus = "awaiting_human";
    if (event.agent_id) {
      next.nodes[event.agent_id] = {
        ...next.nodes[event.agent_id],
        status: "waiting",
        lastEventAt: ts,
      };
    }
    return next;
  }

  // node_complete carries per-node input/output snapshots. Handled before the
  // ``agent_id`` guard below so a node_id-only event is never dropped. Both
  // executors set ``agent_id === node_id`` too, but we key off node_id first.
  if (event.type === "node_complete") {
    const ncId = event.node_id ?? event.agent_id;
    if (!ncId) return next;
    const prev = next.nodes[ncId];
    const mapped: NodeRunStatus =
      event.status === "failed"
        ? "error"
        : event.status === "skipped"
          ? "skipped"
          : "done";
    // Never downgrade an already-errored node back to done (late event).
    const status =
      prev?.status === "error" && mapped === "done" ? "error" : mapped;
    next.nodes[ncId] = {
      ...prev,
      status,
      nodeKind: event.node_kind ?? prev?.nodeKind,
      inputSnapshot: event.input_snapshot,
      outputSnapshot: event.output_snapshot,
      durationMs: event.duration_ms ?? prev?.durationMs,
      dryRun: event.dry_run ?? prev?.dryRun,
      label: event.dry_run ? "dry-run" : prev?.label,
      lastEventAt: ts,
    };
    return next;
  }

  const nodeId = event.agent_id;
  if (!nodeId) return next;

  switch (event.type) {
    case "trigger_fired":
      next.nodes[nodeId] = { status: "done", lastEventAt: ts };
      return next;

    case "agent_start":
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "running",
        lastEventAt: ts,
      };
      return next;
    case "agent_thinking":
      // Already running — keep state, just bump the timestamp.
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "running",
        lastEventAt: ts,
      };
      return next;
    case "agent_complete":
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "done",
        lastEventAt: ts,
      };
      return next;

    case "action_invoked":
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "running",
        lastEventAt: ts,
      };
      return next;
    case "action_result":
    case "action_dry_run":
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "done",
        label: event.type === "action_dry_run" ? "dry-run" : undefined,
        lastEventAt: ts,
      };
      return next;

    case "data_store_op":
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "done",
        label: (event.data?.op as string | undefined) ?? undefined,
        lastEventAt: ts,
      };
      return next;

    case "condition_decided":
    case "if_decided":
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "done",
        label: (event.data?.branch as string | undefined) ?? undefined,
        lastEventAt: ts,
      };
      return next;

    case "for_each_started":
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "running",
        label: `0 / ${event.data?.item_count ?? "?"}`,
        lastEventAt: ts,
      };
      return next;
    case "for_each_item": {
      const idx = (event.data?.index as number | undefined) ?? 0;
      const total =
        (event.data?.item_count as number | undefined) ??
        Number(((next.nodes[nodeId]?.label ?? "0 / ?").split(" / ")[1]) || "?");
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "running",
        label: `${idx + 1} / ${total}`,
        lastEventAt: ts,
      };
      return next;
    }
    case "for_each_complete":
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "done",
        lastEventAt: ts,
      };
      return next;

    case "wait_for_webhook":
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "waiting",
        lastEventAt: ts,
      };
      return next;
    case "webhook_resumed":
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "done",
        lastEventAt: ts,
      };
      return next;

    case "node_skipped":
      next.nodes[nodeId] = {
        ...next.nodes[nodeId],
        status: "skipped",
        lastEventAt: ts,
      };
      return next;

    case "tool_call": {
      // Satellite nodes carry their id under ``data.node_id``.
      const satelliteId = (event.data?.node_id as string | undefined) ?? null;
      if (satelliteId) {
        next.nodes[satelliteId] = {
          ...next.nodes[satelliteId],
          status: "running",
          lastEventAt: ts,
        };
      }
      return next;
    }
    case "tool_result": {
      const satelliteId = (event.data?.node_id as string | undefined) ?? null;
      if (satelliteId) {
        next.nodes[satelliteId] = {
          ...next.nodes[satelliteId],
          status: "done",
          lastEventAt: ts,
        };
      }
      return next;
    }

    default:
      return next;
  }
}

/**
 * Replay a list of events from scratch. Used by tests + when restoring
 * a session mid-stream after a reconnect.
 */
export function replayExecution(events: ExecutionEvent[]): ExecutionRunState {
  let state = INITIAL_EXECUTION_STATE;
  for (const ev of events) state = applyExecutionEvent(state, ev);
  return state;
}
