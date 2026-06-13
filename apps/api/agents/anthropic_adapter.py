"""Anthropic adapter for the chat-runtime LLM abstraction.

Translates between the OpenAI tool-calling shape the runtime speaks
internally and Anthropic's ``tools`` + ``content blocks`` shape. The
runtime stays vendor-neutral; this module owns every shape difference.

Message shape mapping
---------------------

OpenAI (input):
    {role: "user", content: "hi"}
    {role: "assistant", content: "...", tool_calls: [{id, function: {name, arguments}}]}
    {role: "tool", content: "<json>", tool_call_id: "..."}

Anthropic input format:
    {role: "user", content: [{type: "text", text: "..."}]}
    {role: "assistant", content: [
        {type: "text", text: "..."},
        {type: "tool_use", id, name, input: {...}}
    ]}
    {role: "user", content: [
        {type: "tool_result", tool_use_id, content: "<json>"}
    ]}

System message is hoisted out of ``messages`` and passed as the top-level
``system`` parameter.

Tools mapping
-------------

OpenAI:
    {"type": "function", "function": {"name", "description", "parameters"}}
Anthropic:
    {"name", "description", "input_schema"}

Output mapping (non-streaming)
------------------------------

Anthropic returns ``content`` as a list of blocks. We collect text blocks
into the assistant message ``content`` string and ``tool_use`` blocks
into the OpenAI-shaped ``tool_calls`` array. ``stop_reason == "tool_use"``
is the equivalent of OpenAI's ``finish_reason == "tool_calls"``.

Output mapping (streaming)
--------------------------

Anthropic emits ``content_block_delta`` events with ``input_json_delta``
(partial tool arguments) and ``text_delta`` (partial assistant text). We
forward these as the runtime's ``LLMStreamChunk`` shape — same accumulation
logic as the OpenAI path; tool arguments arrive as a JSON-string stream
keyed by the content block's ``index``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from core.config import Settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message + tool translation
# ---------------------------------------------------------------------------


def to_anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Split the OpenAI message array into ``(system, messages_for_anthropic)``.

    Anthropic doesn't accept a ``system`` role in ``messages`` — it has a
    dedicated top-level field. Multiple system messages get concatenated
    with double newlines (matches the official SDK behaviour).
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            out.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for m in messages:
        role = m.get("role")
        if role == "system":
            content = m.get("content") or ""
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue
        if role == "tool":
            # Tool results are batched into the next user turn.
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content": m.get("content") or "",
                }
            )
            continue
        # Any non-tool message — flush any pending tool results first.
        flush_tool_results()
        if role == "user":
            text = m.get("content") or ""
            out.append({"role": "user", "content": [{"type": "text", "text": text}]})
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if isinstance(m.get("content"), str) and m["content"]:
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed_args = (
                        json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
                except json.JSONDecodeError:
                    parsed_args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or "",
                        "name": fn.get("name") or "",
                        "input": parsed_args,
                    }
                )
            if not blocks:
                # Empty assistant message — Anthropic rejects empty content.
                blocks.append({"type": "text", "text": ""})
            out.append({"role": "assistant", "content": blocks})
    flush_tool_results()
    system = "\n\n".join(system_parts) if system_parts else None
    return system, out


def to_anthropic_tools(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for t in tools:
        fn = t.get("function") or {}
        out.append(
            {
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return out


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------


async def complete_anthropic(
    *,
    settings: Settings,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """Returns ``{content, tool_calls, prompt_tokens, completion_tokens}``."""
    from anthropic import AsyncAnthropic

    api_key = (getattr(settings, "ANTHROPIC_API_KEY", "") or "").strip()
    if not api_key:
        raise RuntimeError(
            "agent chat_model.provider=anthropic requires ANTHROPIC_API_KEY"
        )

    system, anth_messages = to_anthropic_messages(messages)
    anth_tools = to_anthropic_tools(tools)

    client = AsyncAnthropic(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": model or "claude-sonnet-4-6",
        "messages": anth_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if system:
        kwargs["system"] = system
    if anth_tools:
        kwargs["tools"] = anth_tools

    response = await client.messages.create(**kwargs)
    text_chunks: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in getattr(response, "content", []) or []:
        b_type = getattr(block, "type", None)
        if b_type == "text":
            text_chunks.append(getattr(block, "text", "") or "")
        elif b_type == "tool_use":
            args = getattr(block, "input", None) or {}
            tool_calls.append(
                {
                    "id": getattr(block, "id", "") or "",
                    "type": "function",
                    "function": {
                        "name": getattr(block, "name", "") or "",
                        "arguments": json.dumps(args, default=str),
                    },
                }
            )
    usage = getattr(response, "usage", None)
    return {
        "content": "".join(text_chunks),
        "tool_calls": tool_calls,
        "prompt_tokens": (
            int(getattr(usage, "input_tokens", 0) or 0) if usage else None
        ),
        "completion_tokens": (
            int(getattr(usage, "output_tokens", 0) or 0) if usage else None
        ),
    }


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def stream_anthropic(
    *,
    settings: Settings,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float,
    max_tokens: int = 4096,
) -> AsyncIterator[dict[str, Any]]:
    """Yield runtime-shaped dicts ``{content_delta?, tool_call_index?,
    tool_call_delta?, finish?, prompt_tokens?, completion_tokens?}``.

    The runtime's ``LLMStreamChunk`` dataclass mirrors this exact shape;
    we yield plain dicts here to keep the adapter independent of the
    consumer's import surface.
    """
    from anthropic import AsyncAnthropic

    api_key = (getattr(settings, "ANTHROPIC_API_KEY", "") or "").strip()
    if not api_key:
        raise RuntimeError(
            "agent chat_model.provider=anthropic requires ANTHROPIC_API_KEY"
        )

    system, anth_messages = to_anthropic_messages(messages)
    anth_tools = to_anthropic_tools(tools)

    client = AsyncAnthropic(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": model or "claude-sonnet-4-6",
        "messages": anth_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if system:
        kwargs["system"] = system
    if anth_tools:
        kwargs["tools"] = anth_tools

    # Map Anthropic content-block index → tool_use id / name so the
    # runtime can accumulate arguments by index in the same shape as the
    # OpenAI path. Text blocks at index 0 are ignored for tool-call
    # accumulation; they yield content_deltas instead.
    tool_blocks: dict[int, dict[str, Any]] = {}

    async with client.messages.stream(**kwargs) as stream:
        async for event in stream:
            etype = getattr(event, "type", None)
            if etype == "content_block_start":
                block = getattr(event, "content_block", None)
                idx = int(getattr(event, "index", 0) or 0)
                if getattr(block, "type", None) == "tool_use":
                    tool_blocks[idx] = {
                        "id": getattr(block, "id", "") or "",
                        "name": getattr(block, "name", "") or "",
                    }
                    yield {
                        "tool_call_index": idx,
                        "tool_call_delta": {
                            "id": tool_blocks[idx]["id"],
                            "function": {"name": tool_blocks[idx]["name"]},
                        },
                    }
            elif etype == "content_block_delta":
                idx = int(getattr(event, "index", 0) or 0)
                delta = getattr(event, "delta", None)
                d_type = getattr(delta, "type", None)
                if d_type == "text_delta":
                    text = getattr(delta, "text", "") or ""
                    if text:
                        yield {"content_delta": text}
                elif d_type == "input_json_delta":
                    partial = getattr(delta, "partial_json", "") or ""
                    if partial and idx in tool_blocks:
                        yield {
                            "tool_call_index": idx,
                            "tool_call_delta": {
                                "function": {"arguments": partial},
                            },
                        }
            elif etype == "message_delta":
                # Stop reason + final usage tokens.
                stop = getattr(getattr(event, "delta", None), "stop_reason", None)
                usage = getattr(event, "usage", None)
                yield {
                    "finish": (
                        "tool_calls" if stop == "tool_use"
                        else "stop" if stop in ("end_turn", "stop_sequence")
                        else stop
                    ),
                    "prompt_tokens": (
                        int(getattr(usage, "input_tokens", 0) or 0)
                        if usage else None
                    ),
                    "completion_tokens": (
                        int(getattr(usage, "output_tokens", 0) or 0)
                        if usage else None
                    ),
                }


__all__ = [
    "complete_anthropic",
    "stream_anthropic",
    "to_anthropic_messages",
    "to_anthropic_tools",
]
