/**
 * Test the payload contract between ``useExecutionStream`` and the API.
 *
 * The hook is the single point that translates UI toggles (Demo /
 * Use real LLM) into the ``ExecutionRequest`` body sent to the
 * ``/execute`` endpoint. We don't want the wiring to silently drift,
 * so these tests pin the exact shape.
 *
 * We don't render React here — we exercise the body builder by
 * stubbing ``ssePostStream`` and asserting what it was called with.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";

import { renderHook, act } from "@testing-library/react";

import { useExecutionStream } from "@/components/workflow/useExecutionStream";

// Mock ssePostStream so the hook never actually opens a network stream.
vi.mock("@/lib/api", () => {
  return {
    ssePostStream: vi.fn(async () => {
      // Resolve immediately — no events emitted.
    }),
  };
});

import { ssePostStream as mockSsePostStream } from "@/lib/api";

describe("useExecutionStream → POST body", () => {
  beforeEach(() => {
    (mockSsePostStream as ReturnType<typeof vi.fn>).mockClear();
  });

  it("sends demo=true + use_real_llm=true when both are requested", async () => {
    const { result } = renderHook(() =>
      useExecutionStream({ workflowId: "wf-1" }),
    );
    await act(async () => {
      await result.current.start({
        inputData: { message: "hi" },
        demo: true,
        useRealLlm: true,
      });
    });
    expect(mockSsePostStream).toHaveBeenCalledTimes(1);
    const call = (mockSsePostStream as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(call.path).toBe("/api/v1/workflows/wf-1/execute");
    expect(call.body).toEqual({
      input_data: { message: "hi" },
      variables: {},
      demo: true,
      use_real_llm: true,
      branch_overrides: {},
    });
  });

  it("sends demo=true + use_real_llm=false when only Demo is on", async () => {
    const { result } = renderHook(() =>
      useExecutionStream({ workflowId: "wf-2" }),
    );
    await act(async () => {
      await result.current.start({
        inputData: {},
        demo: true,
        useRealLlm: false,
      });
    });
    const call = (mockSsePostStream as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(call.body).toMatchObject({ demo: true, use_real_llm: false });
  });

  it("forces use_real_llm=false on production runs even if the caller set true", async () => {
    const { result } = renderHook(() =>
      useExecutionStream({ workflowId: "wf-3" }),
    );
    await act(async () => {
      await result.current.start({
        inputData: {},
        demo: false,
        useRealLlm: true, // ignored when demo=false
      });
    });
    const call = (mockSsePostStream as ReturnType<typeof vi.fn>).mock.calls[0][0];
    // Production runs always use the real LLM via the real executor —
    // the explicit ``use_real_llm`` flag is scoped to demo mode and
    // muting it here keeps the contract unambiguous.
    expect(call.body).toMatchObject({ demo: false, use_real_llm: false });
  });

  it("defaults useRealLlm to false when omitted", async () => {
    const { result } = renderHook(() =>
      useExecutionStream({ workflowId: "wf-4" }),
    );
    await act(async () => {
      await result.current.start({ inputData: {}, demo: true });
    });
    const call = (mockSsePostStream as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(call.body).toMatchObject({ demo: true, use_real_llm: false });
  });
});
