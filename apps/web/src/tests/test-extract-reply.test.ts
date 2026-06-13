import { describe, expect, it } from "vitest";

import { extractReply } from "@/components/workflow/TestRunPanel";
import type { ExecutionEvent } from "@/types/api";

const ev = (p: Partial<ExecutionEvent>): ExecutionEvent => p as ExecutionEvent;

describe("extractReply", () => {
  it("returns the agent's composed reply (the send action just transmits it)", () => {
    const reply = extractReply([
      ev({ type: "agent_complete", agent_id: "resolve", content: "Hi Priya, your order is on the way — refund issued." }),
      ev({
        type: "action_dry_run",
        node_id: "respond_to_customer",
        action_slug: "send_email",
        // A dry-run notice must NOT be surfaced as the reply.
        output_snapshot: { message: "[demo] gmail.send_email would fire here." },
      }),
    ]);
    expect(reply).toBe("Hi Priya, your order is on the way — refund issued.");
  });

  it("falls back to a respond action's genuine body when there's no agent", () => {
    const reply = extractReply([
      ev({
        type: "action_result",
        node_id: "reply",
        action_slug: "reply",
        result: { data: { body: "Your refund is approved." } } as unknown as Record<string, unknown>,
      }),
    ]);
    expect(reply).toBe("Your refund is approved.");
  });

  it("never surfaces a dry-run notice as the reply", () => {
    const reply = extractReply([
      ev({
        type: "action_dry_run",
        node_id: "respond_to_customer",
        action_slug: "send_email",
        output_snapshot: { message: "[demo] gmail.send_email would fire here." },
      }),
    ]);
    expect(reply).toBeNull();
  });

  it("returns null when there's nothing customer-facing", () => {
    const reply = extractReply([
      ev({ type: "node_complete", node_id: "validate", node_kind: "condition" }),
    ]);
    expect(reply).toBeNull();
  });
});
