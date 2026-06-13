import { describe, expect, it } from "vitest";

import { computeInputMode } from "@/components/workflow/TestRunPanel";
import type { TriggerNode, WorkflowDefinition } from "@/types/api";

function wf(nodes: unknown[]): WorkflowDefinition {
  return { name: "t", nodes } as unknown as WorkflowDefinition;
}
function trig(extra: Record<string, unknown>): TriggerNode {
  return { id: "trig", kind: "trigger", depends_on: [], ...extra } as unknown as TriggerNode;
}

describe("computeInputMode", () => {
  it("chat trigger → chat input", () => {
    const t = trig({ trigger_type: "chat" });
    expect(computeInputMode(wf([t]), t)).toBe("chat");
  });

  it("form trigger → form input", () => {
    const t = trig({ trigger_type: "form", form_fields: [] });
    expect(computeInputMode(wf([t]), t)).toBe("form");
  });

  it("schedule trigger → auto (no prompt)", () => {
    const t = trig({ trigger_type: "schedule" });
    expect(computeInputMode(wf([t]), t)).toBe("auto");
  });

  it("webhook trigger → auto", () => {
    const t = trig({ trigger_type: "webhook" });
    expect(computeInputMode(wf([t]), t)).toBe("auto");
  });

  it("manual trigger feeding a customer-message condition → manual (asks for input)", () => {
    // Customer-service shape: manual trigger → validate_customer (condition).
    const t = trig({ trigger_type: "manual" });
    const cond = { id: "validate", kind: "condition", depends_on: ["trig"] };
    expect(computeInputMode(wf([t, cond]), t)).toBe("manual");
  });

  it("manual trigger feeding a Google-Sheet read → auto (ICICI pattern)", () => {
    const t = trig({ trigger_type: "manual" });
    const fetch = {
      id: "fetch",
      kind: "action",
      depends_on: ["trig"],
      provider: "googlesheets",
      action_slug: "read_range",
    };
    expect(computeInputMode(wf([t, fetch]), t)).toBe("auto");
  });

  it("manual trigger feeding a data_store read → auto", () => {
    const t = trig({ trigger_type: "manual" });
    const ds = { id: "ds", kind: "data_store", depends_on: ["trig"], op: "read" };
    expect(computeInputMode(wf([t, ds]), t)).toBe("auto");
  });

  it("manual trigger feeding a side-effecting action → manual (not auto)", () => {
    // A send action is not a data source, so the run still needs input.
    const t = trig({ trigger_type: "manual" });
    const send = {
      id: "send",
      kind: "action",
      depends_on: ["trig"],
      provider: "gmail",
      action_slug: "send_email",
    };
    expect(computeInputMode(wf([t, send]), t)).toBe("manual");
  });
});
