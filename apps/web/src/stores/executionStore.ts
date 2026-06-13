import { immer } from "zustand/middleware/immer";
import { create } from "zustand";

import { api } from "@/lib/api";
import type { ExecutionEvent, HITLApprovalBody } from "@/types/api";

export type ExecRuntimeStatus =
  | "idle"
  | "running"
  | "complete"
  | "error"
  | "awaiting_approval";

export interface AgentStepView {
  agentId: string;
  agentName: string;
  status: ExecRuntimeStatus;
  output?: string;
  tools: {
    tool: string;
    input?: Record<string, unknown>;
    output?: Record<string, unknown>;
  }[];
}

export interface ActionStepView {
  kind: "action";
  nodeId: string;
  name: string;
  provider: string;
  actionSlug: string;
  status: "ok" | "dry_run" | "hitl" | "error";
  result?: Record<string, unknown>;
  message?: string;
}

export interface SkippedStepView {
  kind: "skipped";
  nodeId: string;
  name: string;
  reason: string;
}

export interface ExecutionSlice {
  workflowId: string;
  status: ExecRuntimeStatus;
  events: ExecutionEvent[];
  executionId: string | null;
  checkpointId: string | null;
  steps: AgentStepView[];
  actionSteps: ActionStepView[];
  skippedSteps: SkippedStepView[];
  errorMessage?: string;
  summary?: {
    toolCalls: number;
    agentsCompleted: number;
    integrationsRun?: number;
    agentsRun?: number;
    agentsSkipped?: number;
    actionsSucceeded?: number;
    actionsDryRun?: number;
    nodesSkipped?: number;
    totalExecutable?: number;
  };
}

type ExecutionRoot = {
  executions: Record<string, ExecutionSlice>;
  startCanvas: (workflowId: string) => { sessionKey: string; reset: () => void };
  appendEvent: (sessionKey: string, event: ExecutionEvent) => void;
  setAwaitingApproval: (
    sessionKey: string,
    payload: { executionId: string; checkpointId: string | null },
  ) => void;
  approveHitl: (
    workflowId: string,
    executionId: string,
    payload: HITLApprovalBody,
  ) => Promise<void>;
};

function findOrCreateStep(ex: ExecutionSlice, agentId: string, name: string) {
  let step = ex.steps.find((s) => s.agentId === agentId);
  if (!step) {
    step = {
      agentId,
      agentName: name,
      status: "running",
      tools: [],
    };
    ex.steps.push(step);
  }
  return step;
}

export const useExecutionStore = create(
  immer<ExecutionRoot>((set) => ({
    executions: {},

    startCanvas(workflowId) {
      const sessionKey = crypto.randomUUID();
      set((draft) => {
        draft.executions[sessionKey] = {
          workflowId,
          status: "running",
          events: [],
          executionId: null,
          checkpointId: null,
          steps: [],
          actionSteps: [],
          skippedSteps: [],
        };
      });
      return {
        sessionKey,
        reset: () =>
          set((d) => {
            delete d.executions[sessionKey];
          }),
      };
    },

    appendEvent(sessionKey, event) {
      set((draft) => {
        const ex = draft.executions[sessionKey];
        if (!ex) return;
        ex.events.push(event);
        switch (event.type) {
          case "workflow_start":
            ex.status = "running";
            break;
          case "agent_thinking":
            if (event.agent_id) {
              const st = findOrCreateStep(
                ex,
                event.agent_id,
                event.agent_name ?? event.agent_id,
              );
              st.status = "running";
              const m = event.content ?? event.message;
              if (m) {
                const prevOut = st.output ?? "";
                st.output = `${prevOut}${prevOut ? "\n" : ""}${m}`;
              }
            }
            break;
          case "agent_start":
            if (event.agent_id) {
              findOrCreateStep(
                ex,
                event.agent_id,
                event.agent_name ?? event.agent_id,
              );
            }
            break;
          case "agent_complete":
            if (event.agent_id) {
              const st = findOrCreateStep(
                ex,
                event.agent_id,
                event.agent_name ?? event.agent_id,
              );
              st.status = "complete";
              st.output = event.content ?? st.output;
            }
            break;
          case "tool_call":
            if (event.agent_id) {
              const st = findOrCreateStep(
                ex,
                event.agent_id,
                event.agent_name ?? event.agent_id,
              );
              st.tools.push({
                tool: event.tool_name ?? "tool",
                input: (event.data as Record<string, unknown>) ?? {},
              });
            }
            break;
          case "tool_result":
            if (event.agent_id) {
              const st = ex.steps.find((s) => s.agentId === event.agent_id);
              if (st?.tools.length) {
                const last = st.tools[st.tools.length - 1]!;
                last.output = (event.data as Record<string, unknown>) ?? {};
              }
            }
            break;
          case "action_result": {
            const res = event as unknown as Record<string, unknown>;
            const result = (res.result ?? {}) as Record<string, unknown>;
            const isDry = Boolean(result.__dry_run__);
            ex.actionSteps.push({
              kind: "action",
              nodeId: String(res.node_id ?? res.action_slug ?? "action"),
              name: String(res.name ?? res.action_slug ?? "Action"),
              provider: String(res.provider ?? result.__provider__ ?? ""),
              actionSlug: String(res.action_slug ?? ""),
              status: isDry ? "dry_run" : "ok",
              result,
            });
            break;
          }
          case "action_dry_run": {
            const res = event as unknown as Record<string, unknown>;
            const result = (res.result ?? {}) as Record<string, unknown>;
            ex.actionSteps.push({
              kind: "action",
              nodeId: String(res.node_id ?? res.action_slug ?? "action"),
              name: String(res.name ?? res.action_slug ?? "Action"),
              provider: String(res.provider ?? result.__provider__ ?? ""),
              actionSlug: String(res.action_slug ?? ""),
              status: "dry_run",
              result,
            });
            break;
          }
          case "node_skipped": {
            const sres = event as unknown as Record<string, unknown>;
            ex.skippedSteps.push({
              kind: "skipped",
              nodeId: String(sres.node_id ?? "unknown"),
              name: String(sres.name ?? sres.node_id ?? "Node"),
              reason: String(sres.reason ?? "skipped"),
            });
            break;
          }
          case "hitl_required": {
            ex.status = "awaiting_approval";
            ex.executionId =
              (event.execution_id as string | undefined) ?? ex.executionId;
            ex.checkpointId =
              (event.checkpoint_id as string | undefined) ?? ex.checkpointId;
            const hres = event as unknown as Record<string, unknown>;
            ex.actionSteps.push({
              kind: "action",
              nodeId: String(hres.node_id ?? "hitl"),
              name: String(hres.name ?? "Approval gate"),
              provider: "hitl",
              actionSlug: String(hres.action_slug ?? "request_approval"),
              status: "hitl",
              message: String(hres.message ?? "Human approval required."),
            });
            break;
          }
          case "workflow_complete": {
            ex.status = "complete";
            ex.executionId =
              (event.execution_id as string | undefined) ?? ex.executionId;
            const wcEvent = event as unknown as Record<string, unknown>;
            const remote = (wcEvent.summary ?? null) as
              | {
                  agents_run?: number;
                  agents_skipped?: number;
                  actions_succeeded?: number;
                  actions_dry_run?: number;
                  nodes_skipped?: number;
                  total_executable?: number;
                }
              | null;
            const derivedToolCalls =
              ex.events.filter((e) => e.type === "tool_call").length +
              ex.actionSteps.filter((s) => s.status === "ok").length;
            const derivedAgentsCompleted = ex.steps.filter(
              (st) => st.status === "complete",
            ).length;
            ex.summary = {
              toolCalls: derivedToolCalls,
              agentsCompleted:
                remote && typeof remote.agents_run === "number"
                  ? remote.agents_run
                  : derivedAgentsCompleted,
              integrationsRun:
                remote &&
                (typeof remote.actions_succeeded === "number" ||
                  typeof remote.actions_dry_run === "number")
                  ? (remote.actions_succeeded ?? 0) +
                    (remote.actions_dry_run ?? 0)
                  : ex.actionSteps.length,
              agentsRun: remote?.agents_run,
              agentsSkipped: remote?.agents_skipped,
              actionsSucceeded: remote?.actions_succeeded,
              actionsDryRun: remote?.actions_dry_run,
              nodesSkipped: remote?.nodes_skipped,
              totalExecutable: remote?.total_executable,
            };
            break;
          }
          case "error":
            ex.status = "error";
            ex.errorMessage = event.message ?? "Execution error";
            break;
          default:
            break;
        }
      });
    },

    setAwaitingApproval(sessionKey, payload) {
      set((draft) => {
        const ex = draft.executions[sessionKey];
        if (!ex) return;
        ex.status = "awaiting_approval";
        ex.executionId = payload.executionId;
        ex.checkpointId = payload.checkpointId;
      });
    },

    async approveHitl(workflowId, executionId, payload) {
      await api.post(
        `/api/v1/workflows/${workflowId}/executions/${executionId}/approve`,
        payload,
      );
    },
  })),
);

