"""Phase 2c — pricing, tool timeout/retry, Anthropic translation."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agents.anthropic_adapter import to_anthropic_messages, to_anthropic_tools
from agents.tool_resolver import _with_timeout_and_retry
from services.llm_pricing import (
    PRICING_REVISION,
    cost_microcents,
    estimate_cents,
    lookup,
    microcents_to_cents,
)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_pricing_revision_present() -> None:
    assert PRICING_REVISION  # frozen string, audited per release


def test_known_model_lookup() -> None:
    p = lookup("gpt-4o-mini")
    assert p is not None
    assert p.family == "openai"
    assert p.input_cents_per_1k > 0
    assert p.output_cents_per_1k > 0


def test_unknown_model_returns_none_not_throws() -> None:
    assert lookup("totally-not-a-real-model") is None
    # Cost calc must fail open to zero, never raise.
    assert cost_microcents(
        "totally-not-a-real-model",
        prompt_tokens=1000,
        completion_tokens=500,
    ) == 0


def test_microcents_roundtrip_rounds_up() -> None:
    # 0 → 0, 1 → 1, 999_999 → 1, 1_000_001 → 2 (round-up)
    assert microcents_to_cents(0) == 0
    assert microcents_to_cents(1) == 1
    assert microcents_to_cents(999_999) == 1
    assert microcents_to_cents(1_000_001) == 2


def test_gpt4o_mini_cost_estimate() -> None:
    # gpt-4o-mini: $0.00015 input + $0.0006 output per 1K tokens.
    # 1000 prompt + 500 completion → input $0.00015 + output $0.0003 = $0.00045
    # = 0.045¢ = 45,000 microcents (with ceil rounding).
    mc = cost_microcents(
        "gpt-4o-mini", prompt_tokens=1000, completion_tokens=500
    )
    assert mc > 0
    # Whole-cent rounding should be 1¢ (the entire cost is fractional).
    assert estimate_cents(
        "gpt-4o-mini", prompt_tokens=1000, completion_tokens=500
    ) == 1


def test_anthropic_model_priced() -> None:
    p = lookup("claude-sonnet-4-6")
    assert p is not None and p.family == "anthropic"


# ---------------------------------------------------------------------------
# Tool timeout + retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_timeout_returns_structured_error() -> None:
    async def slow(_args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.5)
        return {"ok": True}

    wrapped = _with_timeout_and_retry(
        slow,
        timeout_ms=50,  # well below sleep
        max_retries=0,
        initial_delay_ms=0,
        label="t",
    )
    result = await wrapped({})
    assert result["ok"] is False
    assert result["code"] == "timeout"
    assert "50 ms" in result["error"]


@pytest.mark.asyncio
async def test_tool_retry_then_success() -> None:
    calls = {"n": 0}

    async def flaky(_args: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return {"ok": True, "value": "third time lucky"}

    wrapped = _with_timeout_and_retry(
        flaky,
        timeout_ms=1000,
        max_retries=3,
        initial_delay_ms=1,  # don't wait long in the test
        label="flaky",
    )
    result = await wrapped({})
    assert result["ok"] is True
    assert result["value"] == "third time lucky"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_tool_retry_exhausted_returns_last_error() -> None:
    async def always_fail(_args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("persistent")

    wrapped = _with_timeout_and_retry(
        always_fail,
        timeout_ms=1000,
        max_retries=2,
        initial_delay_ms=1,
        label="flaky",
    )
    result = await wrapped({})
    assert result["ok"] is False
    assert result["code"] == "exception"
    assert "persistent" in result["error"]
    assert result["attempt"] == 3  # original + 2 retries


@pytest.mark.asyncio
async def test_tool_explicit_failure_retried() -> None:
    """A tool that returns ``{ok: false}`` should be retried like an exception."""
    calls = {"n": 0}

    async def returns_failure(_args: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] < 2:
            return {"ok": False, "error": "transient upstream"}
        return {"ok": True}

    wrapped = _with_timeout_and_retry(
        returns_failure,
        timeout_ms=1000,
        max_retries=2,
        initial_delay_ms=1,
        label="t",
    )
    result = await wrapped({})
    assert result["ok"] is True
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Anthropic message translation
# ---------------------------------------------------------------------------


def test_system_message_hoisted_out() -> None:
    sys, msgs = to_anthropic_messages(
        [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert sys == "be helpful"
    assert msgs == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_assistant_tool_call_translates_to_tool_use_block() -> None:
    _sys, msgs = to_anthropic_messages(
        [
            {"role": "user", "content": "look me up"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"key": "alice"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"found": true}',
            },
        ]
    )
    # Assistant block contains the tool_use.
    assistant = msgs[1]
    assert assistant["role"] == "assistant"
    types = [b["type"] for b in assistant["content"]]
    assert "tool_use" in types
    tu = next(b for b in assistant["content"] if b["type"] == "tool_use")
    assert tu["id"] == "call_1"
    assert tu["name"] == "lookup"
    assert tu["input"] == {"key": "alice"}
    # Tool result is folded into a following user turn.
    last = msgs[2]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][0]["tool_use_id"] == "call_1"


def test_tool_results_batched_into_one_user_turn() -> None:
    _sys, msgs = to_anthropic_messages(
        [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "a",
                        "function": {"name": "x", "arguments": "{}"},
                    },
                    {
                        "id": "b",
                        "function": {"name": "y", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "1"},
            {"role": "tool", "tool_call_id": "b", "content": "2"},
        ]
    )
    # The two tool results collapse into ONE user turn with two blocks.
    tool_user_turn = msgs[-1]
    assert tool_user_turn["role"] == "user"
    assert len(tool_user_turn["content"]) == 2
    assert {b["tool_use_id"] for b in tool_user_turn["content"]} == {"a", "b"}


def test_to_anthropic_tools_shape() -> None:
    out = to_anthropic_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "find a thing",
                    "parameters": {"type": "object"},
                },
            }
        ]
    )
    assert out == [
        {
            "name": "lookup",
            "description": "find a thing",
            "input_schema": {"type": "object"},
        }
    ]
