/**
 * Reducer tests for the SSE → per-node status mapping.
 *
 * Covers every event type that paints a status on the canvas, plus the
 * end-to-end replay over a representative demo run.
 */

import { describe, expect, it } from "vitest";

import {
  applyExecutionEvent,
  INITIAL_EXECUTION_STATE,
  replayExecution,
} from "@/components/workflow/execution-status";
import type { ExecutionEvent } from "@/types/api";

function ev(partial: Partial<ExecutionEvent> & { type: ExecutionEvent["type"] }): ExecutionEvent {
  return partial as ExecutionEvent;
}

describe("applyExecutionEvent", () => {
  it("flips graphStatus to running on workflow_start", () => {
    const s = applyExecutionEvent(INITIAL_EXECUTION_STATE, ev({ type: "workflow_start", execution_id: "exec-1" }));
    expect(s.graphStatus).toBe("running");
    expect(s.executionId).toBe("exec-1");
  });

  it("flips graphStatus to complete on workflow_complete", () => {
    const s = replayExecution([
      ev({ type: "workflow_start", execution_id: "x" }),
      ev({ type: "workflow_complete" }),
    ]);
    expect(s.graphStatus).toBe("complete");
  });

  it("paints the trigger node done on trigger_fired", () => {
    const s = applyExecutionEvent(INITIAL_EXECUTION_STATE, ev({ type: "trigger_fired", agent_id: "trig" }));
    expect(s.nodes.trig.status).toBe("done");
  });

  it("transitions an agent through running → done", () => {
    const s = replayExecution([
      ev({ type: "agent_start", agent_id: "a" }),
    ]);
    expect(s.nodes.a.status).toBe("running");
    const s2 = applyExecutionEvent(s, ev({ type: "agent_complete", agent_id: "a" }));
    expect(s2.nodes.a.status).toBe("done");
  });

  it("labels condition + if results with the picked branch", () => {
    const s = replayExecution([
      ev({
        type: "condition_decided",
        agent_id: "route",
        data: { branch: "important", branches_available: ["important", "trivial"] },
      }),
      ev({
        type: "if_decided",
        agent_id: "check",
        data: { branch: "true" },
      }),
    ]);
    expect(s.nodes.route.label).toBe("important");
    expect(s.nodes.check.label).toBe("true");
  });

  it("for_each tracks current iteration count via label", () => {
    const s = replayExecution([
      ev({ type: "for_each_started", agent_id: "loop", data: { item_count: 2 } }),
      ev({ type: "for_each_item", agent_id: "loop", data: { index: 0, item_count: 2 } }),
      ev({ type: "for_each_item", agent_id: "loop", data: { index: 1, item_count: 2 } }),
      ev({ type: "for_each_complete", agent_id: "loop" }),
    ]);
    expect(s.nodes.loop.status).toBe("done");
  });

  it("wait_for_webhook marks node waiting until webhook_resumed", () => {
    const s1 = applyExecutionEvent(INITIAL_EXECUTION_STATE, ev({ type: "wait_for_webhook", agent_id: "wait" }));
    expect(s1.nodes.wait.status).toBe("waiting");
    const s2 = applyExecutionEvent(s1, ev({ type: "webhook_resumed", agent_id: "wait" }));
    expect(s2.nodes.wait.status).toBe("done");
  });

  it("action_dry_run paints node done and labels it dry-run", () => {
    const s = applyExecutionEvent(INITIAL_EXECUTION_STATE, ev({
      type: "action_dry_run",
      agent_id: "notify",
      data: { result: { __dry_run__: true } },
    }));
    expect(s.nodes.notify.status).toBe("done");
    expect(s.nodes.notify.label).toBe("dry-run");
  });

  it("node-level error event paints just that node", () => {
    const s = applyExecutionEvent(INITIAL_EXECUTION_STATE, ev({
      type: "error",
      agent_id: "a",
      message: "boom",
    }));
    expect(s.nodes.a.status).toBe("error");
    expect(s.nodes.a.errorMessage).toBe("boom");
    expect(s.graphStatus).toBe("idle"); // graph-level still idle
  });

  it("graph-level error event escalates to graphStatus", () => {
    const s = applyExecutionEvent(INITIAL_EXECUTION_STATE, ev({
      type: "error",
      message: "stream broken",
    }));
    expect(s.graphStatus).toBe("error");
    expect(s.errorMessage).toBe("stream broken");
  });

  it("hitl_required flips graph to awaiting_human", () => {
    const s = applyExecutionEvent(INITIAL_EXECUTION_STATE, ev({
      type: "hitl_required",
      agent_id: "approve",
    }));
    expect(s.graphStatus).toBe("awaiting_human");
    expect(s.nodes.approve.status).toBe("waiting");
  });

  it("tool_call + tool_result paint the satellite node, not the agent", () => {
    const s = replayExecution([
      ev({ type: "tool_call", agent_id: "agent", data: { node_id: "lookup" } }),
      ev({ type: "tool_result", agent_id: "agent", data: { node_id: "lookup" } }),
    ]);
    expect(s.nodes.lookup.status).toBe("done");
    expect(s.nodes.agent).toBeUndefined();
  });

  it("heartbeat events do not affect status but bump event count", () => {
    const s = applyExecutionEvent(INITIAL_EXECUTION_STATE, ev({ type: "heartbeat" }));
    expect(s.eventCount).toBe(1);
    expect(s.graphStatus).toBe("idle");
  });

  it("node_complete paints node done and stores input/output snapshots", () => {
    const s = applyExecutionEvent(
      INITIAL_EXECUTION_STATE,
      ev({
        type: "node_complete",
        node_id: "agent",
        agent_id: "agent",
        node_kind: "agent",
        status: "completed",
        duration_ms: 42,
        input_snapshot: { upstream: { trig: { ok: true } } },
        output_snapshot: { value: "hello" },
      }),
    );
    expect(s.nodes.agent.status).toBe("done");
    expect(s.nodes.agent.nodeKind).toBe("agent");
    expect(s.nodes.agent.durationMs).toBe(42);
    expect(s.nodes.agent.inputSnapshot).toEqual({ upstream: { trig: { ok: true } } });
    expect(s.nodes.agent.outputSnapshot).toEqual({ value: "hello" });
    // It is a state-changing event — eventCount must increment.
    expect(s.eventCount).toBe(1);
    // It must not touch graph-level status.
    expect(s.graphStatus).toBe("idle");
  });

  it("node_complete with dry_run labels the node and flags dryRun", () => {
    const s = applyExecutionEvent(
      INITIAL_EXECUTION_STATE,
      ev({
        type: "node_complete",
        node_id: "notify",
        agent_id: "notify",
        node_kind: "action",
        status: "completed",
        dry_run: true,
        input_snapshot: {},
        output_snapshot: { __dry_run__: true },
      }),
    );
    expect(s.nodes.notify.status).toBe("done");
    expect(s.nodes.notify.dryRun).toBe(true);
    expect(s.nodes.notify.label).toBe("dry-run");
  });

  it("node_complete with status failed paints the node error", () => {
    const s = applyExecutionEvent(
      INITIAL_EXECUTION_STATE,
      ev({ type: "node_complete", node_id: "wait", agent_id: "wait", status: "failed" }),
    );
    expect(s.nodes.wait.status).toBe("error");
  });

  it("a late node_complete does not downgrade an already-errored node", () => {
    const s = replayExecution([
      ev({ type: "error", agent_id: "a", message: "boom" }),
      ev({ type: "node_complete", node_id: "a", agent_id: "a", status: "completed" }),
    ]);
    expect(s.nodes.a.status).toBe("error");
  });
});

describe("end-to-end demo run replay", () => {
  it("matches the canonical demo event sequence", () => {
    // This mirrors what the backend DemoExecutor emits for a
    // trigger → agent → action graph.
    const final = replayExecution([
      ev({ type: "workflow_start", execution_id: "demo-1", data: { demo: true } }),
      ev({ type: "trigger_fired", agent_id: "trig" }),
      ev({ type: "agent_start", agent_id: "agent" }),
      ev({ type: "agent_thinking", agent_id: "agent", content: "thinking…" }),
      ev({ type: "tool_call", agent_id: "agent", data: { node_id: "sat" } }),
      ev({ type: "tool_result", agent_id: "agent", data: { node_id: "sat" } }),
      ev({ type: "agent_complete", agent_id: "agent" }),
      ev({ type: "action_invoked", agent_id: "notify" }),
      ev({ type: "action_dry_run", agent_id: "notify", data: { result: { __dry_run__: true } } }),
      ev({ type: "workflow_complete", success: true }),
    ]);

    expect(final.graphStatus).toBe("complete");
    expect(final.nodes.trig.status).toBe("done");
    expect(final.nodes.agent.status).toBe("done");
    expect(final.nodes.sat.status).toBe("done");
    expect(final.nodes.notify.status).toBe("done");
    expect(final.nodes.notify.label).toBe("dry-run");
  });
});
