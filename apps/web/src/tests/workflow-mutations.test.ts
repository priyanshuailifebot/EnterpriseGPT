/**
 * Unit tests for the pure mutation helpers backing the interactive canvas.
 *
 * These cover the contract every canvas action goes through:
 *   - id uniqueness
 *   - reference cleanup on remove
 *   - cycle rejection on connect
 *   - id rename propagation
 *   - client-side validation matching Pydantic rules
 */

import { describe, expect, it } from "vitest";

import {
  addNode,
  allNodes,
  autoLayout,
  connectNodes,
  disconnectNodes,
  makeBlankNode,
  patchNode,
  removeNode,
  renameNodeId,
  uniqueIdFrom,
  validateDefinition,
} from "@/components/workflow/workflow-mutations";
import type {
  ActionNode,
  AgentNode,
  ForEachNode,
  MemoryNode,
  TriggerNode,
  WorkflowDefinition,
} from "@/types/api";

function emptyDefinition(): WorkflowDefinition {
  return {
    name: "Test",
    description: "",
    trigger: "",
    agents: [],
    nodes: [],
    human_checkpoints: [],
    output_format: "text",
  };
}

function seedTriggerAgent(): WorkflowDefinition {
  const trigger = makeBlankNode("trigger", "trigger", "Trigger") as TriggerNode;
  const agent = makeBlankNode("agent", "agent", "Agent") as AgentNode;
  agent.depends_on = ["trigger"];
  let d = emptyDefinition();
  d = addNode(d, trigger);
  d = addNode(d, agent);
  return d;
}

// ---------------------------------------------------------------------------
// uniqueIdFrom
// ---------------------------------------------------------------------------

describe("uniqueIdFrom", () => {
  it("snake_cases the label", () => {
    expect(uniqueIdFrom("Send Slack Message", emptyDefinition())).toBe(
      "send_slack_message",
    );
  });

  it("appends a numeric suffix on collision", () => {
    const d = seedTriggerAgent();
    expect(uniqueIdFrom("agent", d)).toBe("agent_2");
  });

  it("falls back to 'node' for empty labels", () => {
    expect(uniqueIdFrom("", emptyDefinition())).toBe("node");
    expect(uniqueIdFrom("!!!", emptyDefinition())).toBe("node");
  });
});

// ---------------------------------------------------------------------------
// addNode + removeNode
// ---------------------------------------------------------------------------

describe("addNode / removeNode", () => {
  it("adds a node and exposes it via allNodes", () => {
    const d = seedTriggerAgent();
    expect(allNodes(d)).toHaveLength(2);
  });

  it("migrates legacy agents on first mutation", () => {
    const legacy: WorkflowDefinition = {
      name: "Legacy",
      description: "",
      trigger: "",
      agents: [
        {
          id: "old",
          name: "Old Agent",
          role: "",
          instructions: "",
          tools: [],
          depends_on: [],
          is_parallel: false,
        },
      ],
      nodes: [],
      human_checkpoints: [],
      output_format: "text",
    };
    const newNode = makeBlankNode("memory", "mem", "Memory");
    const next = addNode(legacy, newNode);
    expect(next.nodes).toHaveLength(2);
    expect(next.agents).toHaveLength(0);
  });

  it("removes a node and prunes depends_on references", () => {
    const d = seedTriggerAgent();
    const next = removeNode(d, "trigger");
    expect(allNodes(next)).toHaveLength(1);
    const agent = allNodes(next).find((n) => n.id === "agent")!;
    expect(agent.depends_on).toEqual([]);
  });

  it("prunes for_each.body / items_from when an inner node is removed", () => {
    let d = emptyDefinition();
    d = addNode(d, makeBlankNode("trigger", "trigger", "Trigger"));
    d = addNode(d, makeBlankNode("agent", "agent", "Agent"));
    const fe = makeBlankNode("for_each", "loop", "Loop") as ForEachNode;
    fe.items_from = "agent";
    fe.body = ["agent"];
    d = addNode(d, fe);
    d = removeNode(d, "agent");
    const updated = allNodes(d).find((n) => n.id === "loop") as ForEachNode;
    expect(updated.items_from).toBe("");
    expect(updated.body).toEqual([]);
  });

  it("clears agent.memory_ref when the memory node is removed", () => {
    let d = seedTriggerAgent();
    d = addNode(d, makeBlankNode("memory", "mem", "Memory"));
    d = patchNode<AgentNode>(d, "agent", { memory_ref: "mem" });
    d = removeNode(d, "mem");
    const agent = allNodes(d).find((n) => n.id === "agent") as AgentNode;
    expect(agent.memory_ref).toBe("");
  });

  it("clears satellite.parent_agent_id when the parent agent is removed", () => {
    let d = seedTriggerAgent();
    const action = makeBlankNode("action", "send_slack", "Slack") as ActionNode;
    action.parent_agent_id = "agent";
    d = addNode(d, action);
    d = removeNode(d, "agent");
    const sat = allNodes(d).find((n) => n.id === "send_slack") as ActionNode;
    expect(sat.parent_agent_id).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// connectNodes / disconnectNodes
// ---------------------------------------------------------------------------

describe("connectNodes / disconnectNodes", () => {
  it("adds source to target.depends_on", () => {
    let d = emptyDefinition();
    d = addNode(d, makeBlankNode("trigger", "trigger", "Trigger"));
    d = addNode(d, makeBlankNode("agent", "agent", "Agent"));
    d = connectNodes(d, "trigger", "agent");
    const agent = allNodes(d).find((n) => n.id === "agent")!;
    expect(agent.depends_on).toEqual(["trigger"]);
  });

  it("rejects self-loops", () => {
    let d = emptyDefinition();
    d = addNode(d, makeBlankNode("agent", "a", "A"));
    const next = connectNodes(d, "a", "a");
    const a = allNodes(next).find((n) => n.id === "a")!;
    expect(a.depends_on).toEqual([]);
  });

  it("rejects cycles", () => {
    let d = emptyDefinition();
    d = addNode(d, makeBlankNode("agent", "a", "A"));
    d = addNode(d, makeBlankNode("agent", "b", "B"));
    d = connectNodes(d, "a", "b"); // a → b
    const before = allNodes(d).find((n) => n.id === "a")!.depends_on.slice();
    d = connectNodes(d, "b", "a"); // would create cycle
    const a = allNodes(d).find((n) => n.id === "a")!;
    expect(a.depends_on).toEqual(before); // unchanged
  });

  it("disconnect removes the edge", () => {
    const d0 = seedTriggerAgent();
    const d = disconnectNodes(d0, "trigger", "agent");
    const agent = allNodes(d).find((n) => n.id === "agent")!;
    expect(agent.depends_on).toEqual([]);
  });

  it("is idempotent on duplicate connect", () => {
    let d = seedTriggerAgent();
    d = connectNodes(d, "trigger", "agent"); // already connected
    const agent = allNodes(d).find((n) => n.id === "agent")!;
    expect(agent.depends_on).toEqual(["trigger"]);
  });
});

// ---------------------------------------------------------------------------
// renameNodeId
// ---------------------------------------------------------------------------

describe("renameNodeId", () => {
  it("renames everywhere", () => {
    let d = seedTriggerAgent();
    d = addNode(d, makeBlankNode("memory", "mem", "Memory"));
    d = patchNode<AgentNode>(d, "agent", { memory_ref: "mem" });
    d = renameNodeId(d, "mem", "session_memory");
    const agent = allNodes(d).find((n) => n.id === "agent") as AgentNode;
    expect(agent.memory_ref).toBe("session_memory");
    expect(allNodes(d).find((n) => n.id === "mem")).toBeUndefined();
    expect(allNodes(d).find((n) => n.id === "session_memory")).toBeDefined();
  });

  it("refuses collisions", () => {
    let d = seedTriggerAgent();
    d = renameNodeId(d, "agent", "trigger");
    expect(allNodes(d).find((n) => n.id === "agent")).toBeDefined();
    expect(allNodes(d).filter((n) => n.id === "trigger")).toHaveLength(1);
  });

  it("refuses invalid id patterns", () => {
    let d = seedTriggerAgent();
    d = renameNodeId(d, "agent", "has spaces!");
    expect(allNodes(d).find((n) => n.id === "agent")).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// validateDefinition
// ---------------------------------------------------------------------------

describe("validateDefinition", () => {
  it("requires at least one node", () => {
    const d = emptyDefinition();
    const issues = validateDefinition(d);
    expect(issues.some((i) => i.severity === "error")).toBe(true);
  });

  it("flags duplicate ids", () => {
    // Force a duplicate by bypassing the mutation helpers — the helpers
    // would auto-rename via uniqueIdFrom, so we construct the broken
    // definition manually for the validator test.
    const trig = makeBlankNode("trigger", "trigger", "Trigger");
    const agent = makeBlankNode("agent", "agent", "Agent");
    const dup = makeBlankNode("agent", "agent", "Dup");
    const d: WorkflowDefinition = {
      ...emptyDefinition(),
      nodes: [trig, agent, dup],
    };
    const issues = validateDefinition(d);
    expect(issues.some((i) => i.message.includes("Duplicate"))).toBe(true);
  });

  it("flags missing memory_ref target", () => {
    let d = seedTriggerAgent();
    d = patchNode<AgentNode>(d, "agent", { memory_ref: "no_such_node" });
    const issues = validateDefinition(d);
    expect(
      issues.some((i) => i.message.includes("memory_ref")),
    ).toBe(true);
  });

  it("flags satellite with depends_on", () => {
    let d = seedTriggerAgent();
    const action = makeBlankNode("action", "sat", "Sat") as ActionNode;
    action.parent_agent_id = "agent";
    action.depends_on = ["agent"];
    d = addNode(d, action);
    const issues = validateDefinition(d);
    expect(issues.some((i) => i.message.includes("Satellite"))).toBe(true);
  });

  it("returns empty for a healthy graph", () => {
    const d = seedTriggerAgent();
    const issues = validateDefinition(d);
    expect(issues.filter((i) => i.severity === "error")).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// autoLayout
// ---------------------------------------------------------------------------

describe("autoLayout", () => {
  it("assigns depth 0 to the trigger and 1 to the agent", () => {
    const d = seedTriggerAgent();
    const layout = autoLayout(d);
    expect(layout.get("trigger")?.depth).toBe(0);
    expect(layout.get("agent")?.depth).toBe(1);
  });

  it("skips satellites", () => {
    let d = seedTriggerAgent();
    const memNode = makeBlankNode("memory", "mem", "Memory") as MemoryNode;
    memNode.parent_agent_id = "agent";
    d = addNode(d, memNode);
    const layout = autoLayout(d);
    expect(layout.has("mem")).toBe(false);
  });
});
