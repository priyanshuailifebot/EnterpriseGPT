"""ChatRuntime — the long-running, multi-turn Tools-Agent loop.

Phase 2 brings the *real* runtime for the Tools-Agent composite pattern.
A single ``ChatRuntime`` instance is created per request to handle one
inbound user message; it owns the LLM call + tool loop + memory I/O +
output validation, persisting an audit trail to ``chat_messages``.

The flow per inbound message:

1. Read the conversation memory (Redis) for the bound MemoryNode.
2. Build the message array — system prompt from the agent's role +
   instructions, prior turns from memory, the new user turn.
3. Build the toolset from the agent's satellites (ToolResolver).
4. Loop:
     a. Call the LLM with messages + tools.
     b. If the LLM returns tool_calls, invoke each handler, append the
        tool result, continue.
     c. If the LLM returns a final assistant message, break.
5. If the agent has an ``output_parser_ref``, validate the final text
   against the schema and re-prompt up to ``max_retries`` times.
6. Persist every new turn to ``chat_messages`` + Redis memory.
7. Return the validated final response to the caller.

The LLM is whatever the agent's ``chat_model`` resolves to. We support
OpenAI directly (``openai.AsyncClient``) and Azure OpenAI (existing
config). Anthropic / others land in a follow-up — the abstraction is
``LLMClient`` below.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.tool_resolver import ResolvedToolset, build_toolset
from core.config import Settings
from core.redis import get_redis
from models.chat_session import (
    ChatMessage,
    ChatMessageRole,
    ChatSession,
    ChatSessionStatus,
)
from models.native_connection import NativeConnection
from models.workflow_version import WorkflowVersion
from schemas.workflow import (
    AgentNode,
    MemoryNode,
    OutputParserNode,
    TriggerNode,
    WorkflowDefinition,
)
from services.chat_pii import ChatPIIRedactor
from services.chat_rate_limiter import (
    RateLimitConfig,
    RateLimitDecision,
    RateLimiter,
)
from services.llm_pricing import cost_microcents, microcents_to_cents
from services.memory_store import MemoryStore, Turn
from services.output_parser_service import ParseResult, parse_or_retry
from services.pii_service import PIIService

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants / safety limits
# ---------------------------------------------------------------------------


# Hard cap on the LLM ↔ tool ping-pong. The agent should converge in a
# handful of round-trips; loops past this are almost always a bug or an
# infinite-call situation we want to fail loudly rather than burn tokens.
_MAX_TOOL_LOOP_ITERATIONS = 12


class RateLimitExceeded(RuntimeError):
    """Raised when a session's pre-flight rate-limit check rejects a turn.

    The non-streaming API route catches this and returns 429 with a
    structured body; the streaming route emits a terminal
    ``rate_limited`` SSE event and closes the stream.
    """

    def __init__(self, decision: RateLimitDecision) -> None:
        super().__init__(decision.reason or "rate_limit_exceeded")
        self.decision = decision


# ---------------------------------------------------------------------------
# LLM abstraction
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """Subset of the OpenAI chat-completion response we actually consume."""

    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass
class LLMStreamChunk:
    """One streaming delta from the LLM.

    Exactly one of ``content_delta`` / ``tool_call_index`` / ``finish`` is
    meaningful per chunk; the others are ``None`` so the runtime can fan
    out cheaply without per-chunk branching at every consumer.
    """

    # Incremental assistant text. ``None`` when this chunk carried no text
    # delta (e.g. tool-call argument streaming).
    content_delta: str | None = None
    # OpenAI streams tool calls one chunk at a time, with an ``index``
    # identifying the slot. We forward the index + the delta dict so the
    # runtime can accumulate properly across chunks.
    tool_call_index: int | None = None
    tool_call_delta: dict[str, Any] | None = None
    # Set on the final chunk: "stop" | "tool_calls" | "length" | ...
    finish: str | None = None
    # Token usage only arrives on the final chunk when the model supports it.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMClient:
    """Async wrapper that hides OpenAI vs Azure OpenAI differences.

    The agent's ``chat_model`` field selects the provider. When unset we
    fall back to Azure OpenAI from the platform settings — same default
    every other agent in the codebase uses.
    """

    def __init__(self, settings: Settings, chat_model: dict[str, Any] | None) -> None:
        self._settings = settings
        self._chat_model = chat_model or {}

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        provider = (self._chat_model.get("provider") or "").lower()
        model = self._chat_model.get("model") or ""
        temp = (
            temperature
            if temperature is not None
            else float(self._chat_model.get("temperature", 0.0))
        )

        if provider == "anthropic":
            from agents.anthropic_adapter import complete_anthropic

            raw = await complete_anthropic(
                settings=self._settings,
                model=model,
                messages=messages,
                tools=tools,
                temperature=temp,
            )
            return LLMResponse(
                content=raw.get("content", "") or "",
                tool_calls=raw.get("tool_calls", []) or [],
                prompt_tokens=raw.get("prompt_tokens"),
                completion_tokens=raw.get("completion_tokens"),
            )
        if provider == "openai":
            return await self._call_openai(
                messages=messages,
                tools=tools,
                temperature=temp,
                response_format=response_format,
                model=model or "gpt-4o",
            )
        # Default: Azure OpenAI using existing platform creds.
        return await self._call_azure(
            messages=messages,
            tools=tools,
            temperature=temp,
            response_format=response_format,
            model=model
            or self._settings.AZURE_OPENAI_DEPLOYMENT
            or self._settings.AZURE_OPENAI_DEFAULT_MODEL,
        )

    async def _call_openai(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response_format: dict[str, Any] | None,
        model: str,
    ) -> LLMResponse:
        from openai import AsyncOpenAI

        api_key = (self._settings.OPENAI_API_KEY or "").strip() if hasattr(
            self._settings, "OPENAI_API_KEY"
        ) else ""
        if not api_key:
            raise RuntimeError(
                "agent chat_model.provider=openai requires OPENAI_API_KEY "
                "to be configured in the API settings"
            )
        client = AsyncOpenAI(api_key=api_key)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_format:
            kwargs["response_format"] = response_format
        completion = await client.chat.completions.create(**kwargs)
        return _unpack(completion)

    async def _call_azure(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        response_format: dict[str, Any] | None,
        model: str,
    ) -> LLMResponse:
        from openai import AsyncAzureOpenAI

        ep = (self._settings.AZURE_OPENAI_ENDPOINT or "").strip().rstrip("/")
        key = (self._settings.AZURE_OPENAI_API_KEY or "").strip()
        if not ep or not key:
            raise RuntimeError(
                "Azure OpenAI is not configured "
                "(AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY)"
            )
        client = AsyncAzureOpenAI(
            azure_endpoint=ep,
            api_key=key,
            api_version=self._settings.AZURE_OPENAI_API_VERSION,
        )
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_format:
            kwargs["response_format"] = response_format
        completion = await client.chat.completions.create(**kwargs)
        return _unpack(completion)

    # ------------------------------------------------------------------
    # Streaming variant
    # ------------------------------------------------------------------

    async def complete_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        """Async-iterate the LLM's streaming response.

        Delegates to OpenAI or Azure OpenAI depending on the agent's
        ``chat_model.provider``. Both vendors share the OpenAI streaming
        chunk shape; ``_iter_openai_stream`` handles both.
        """
        provider = (self._chat_model.get("provider") or "").lower()
        model = self._chat_model.get("model") or ""
        temp = (
            temperature
            if temperature is not None
            else float(self._chat_model.get("temperature", 0.0))
        )

        if provider == "anthropic":
            from agents.anthropic_adapter import stream_anthropic

            async for chunk_dict in stream_anthropic(
                settings=self._settings,
                model=model,
                messages=messages,
                tools=tools,
                temperature=temp,
            ):
                yield LLMStreamChunk(**chunk_dict)
            return

        if provider == "openai":
            from openai import AsyncOpenAI

            api_key = getattr(self._settings, "OPENAI_API_KEY", "") or ""
            if not api_key:
                raise RuntimeError(
                    "agent chat_model.provider=openai requires OPENAI_API_KEY"
                )
            client = AsyncOpenAI(api_key=api_key)
            stream = await client.chat.completions.create(
                model=model or "gpt-4o",
                messages=messages,
                temperature=temp,
                tools=tools or None,
                tool_choice=("auto" if tools else None),
                response_format=response_format,
                stream=True,
                stream_options={"include_usage": True},
            )
        else:
            from openai import AsyncAzureOpenAI

            ep = (self._settings.AZURE_OPENAI_ENDPOINT or "").strip().rstrip("/")
            key = (self._settings.AZURE_OPENAI_API_KEY or "").strip()
            if not ep or not key:
                raise RuntimeError(
                    "Azure OpenAI is not configured (endpoint / api key)"
                )
            client_az = AsyncAzureOpenAI(
                azure_endpoint=ep,
                api_key=key,
                api_version=self._settings.AZURE_OPENAI_API_VERSION,
            )
            stream = await client_az.chat.completions.create(
                model=model
                or self._settings.AZURE_OPENAI_DEPLOYMENT
                or self._settings.AZURE_OPENAI_DEFAULT_MODEL,
                messages=messages,
                temperature=temp,
                tools=tools or None,
                tool_choice=("auto" if tools else None),
                response_format=response_format,
                stream=True,
                stream_options={"include_usage": True},
            )

        async for ch in _iter_openai_stream(stream):
            yield ch


# ---------------------------------------------------------------------------
# Streaming chunk decoder — shared by OpenAI direct + Azure (same shape).
# ---------------------------------------------------------------------------


async def _iter_openai_stream(stream: Any) -> AsyncIterator[LLMStreamChunk]:
    """Convert an OpenAI streaming completion into ``LLMStreamChunk`` events.

    Per chunk we may receive (in any order):
    * ``delta.content`` — incremental assistant text.
    * ``delta.tool_calls[i].function.name`` / ``.arguments`` — partial pieces
      of one or more tool calls; the ``index`` identifies which slot a
      chunk belongs to. The runtime accumulates by index.
    * ``finish_reason`` — terminal signal on the LAST chunk.
    * ``usage`` — token totals on the very last chunk (when the model
      sends them; gated by ``stream_options.include_usage``).
    """
    async for raw in stream:
        choices = getattr(raw, "choices", None) or []
        usage = getattr(raw, "usage", None)
        prompt_tokens = None
        completion_tokens = None
        if usage is not None:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) or None
            completion_tokens = (
                int(getattr(usage, "completion_tokens", 0) or 0) or None
            )

        if not choices:
            # Some vendors emit a final usage-only chunk with no choices.
            if prompt_tokens or completion_tokens:
                yield LLMStreamChunk(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            continue

        choice = choices[0]
        delta = getattr(choice, "delta", None)
        finish = getattr(choice, "finish_reason", None)

        if delta is None:
            if finish or prompt_tokens or completion_tokens:
                yield LLMStreamChunk(
                    finish=finish,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            continue

        content_delta = getattr(delta, "content", None)
        if content_delta:
            yield LLMStreamChunk(content_delta=content_delta)

        tool_call_deltas = getattr(delta, "tool_calls", None) or []
        for tc in tool_call_deltas:
            idx = getattr(tc, "index", None)
            if idx is None:
                continue
            fn = getattr(tc, "function", None)
            delta_dict: dict[str, Any] = {}
            if getattr(tc, "id", None):
                delta_dict["id"] = tc.id
            if fn is not None:
                name = getattr(fn, "name", None)
                args = getattr(fn, "arguments", None)
                if name is not None:
                    delta_dict.setdefault("function", {})["name"] = name
                if args is not None:
                    delta_dict.setdefault("function", {})["arguments"] = args
            yield LLMStreamChunk(tool_call_index=int(idx), tool_call_delta=delta_dict)

        if finish or prompt_tokens or completion_tokens:
            yield LLMStreamChunk(
                finish=finish,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )


def _unpack(completion: Any) -> LLMResponse:
    choice = completion.choices[0]
    msg = choice.message
    raw_tool_calls = getattr(msg, "tool_calls", None) or []
    tool_calls: list[dict[str, Any]] = []
    for tc in raw_tool_calls:
        fn = getattr(tc, "function", None)
        tool_calls.append(
            {
                "id": getattr(tc, "id", "") or "",
                "type": "function",
                "function": {
                    "name": getattr(fn, "name", "") if fn else "",
                    "arguments": getattr(fn, "arguments", "") if fn else "",
                },
            }
        )
    usage = getattr(completion, "usage", None)
    return LLMResponse(
        content=msg.content or "",
        tool_calls=tool_calls,
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0) if usage else None,
        completion_tokens=(
            int(getattr(usage, "completion_tokens", 0) or 0) if usage else None
        ),
    )


# ---------------------------------------------------------------------------
# ChatRuntime
# ---------------------------------------------------------------------------


@dataclass
class ChatTurnResult:
    """Returned to API callers after one inbound user message."""

    assistant_text: str
    structured: Any | None
    parser: ParseResult | None
    tool_call_count: int
    prompt_tokens: int
    completion_tokens: int
    finished_at: datetime


class ChatRuntime:
    """One instance per inbound message — stateless across requests."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: AsyncSession,
        session: ChatSession,
        workflow_definition: WorkflowDefinition,
        workspace_connections: list[NativeConnection],
        llm: LLMClient | None = None,
        pii: PIIService | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._session = session
        self._definition = workflow_definition
        self._workspace_connections = workspace_connections
        self._llm = llm  # injected for tests; built lazily otherwise
        # PII redactor — durable storage (DB rows + Redis memory list)
        # never contains raw PII. The LLM + tools always see the real
        # values via ``restore_for_llm``. Token map is per-session in
        # Redis (TTL = MemoryNode.ttl_seconds by default).
        self._pii = ChatPIIRedactor(
            pii or PIIService(),
            session_id=session.id,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle_user_message(self, content: str) -> ChatTurnResult:
        agent = self._resolve_agent()
        # Pre-flight rate-limit + budget gate. Raises ``RateLimitExceeded``
        # which the router translates to HTTP 429. Done BEFORE memory
        # reads / DB writes so a rejected turn doesn't pollute state.
        await self._check_rate_limit()
        # Load any pre-existing PII token map for the session so prior
        # history rehydrates with real values for the LLM.
        await self._pii.load()
        memory_node, mem_store = self._resolve_memory(agent)
        parser_node = self._resolve_parser(agent)
        toolset = build_toolset(
            workflow_definition=self._definition,
            agent=agent,
            workspace_connections=self._workspace_connections,
            workspace_id=self._session.workspace_id,
            workflow_id=self._session.workflow_id,
            db=self._db,
        )
        llm = self._llm or LLMClient(self._settings, agent.chat_model)

        # 1) Read prior conversation history BEFORE appending the new user
        #    turn — otherwise the LLM would see this turn twice (once in
        #    history, once as the trailing user message).
        prior_history: list[dict[str, Any]] = []
        if memory_node is not None and mem_store is not None:
            turns = await mem_store.read(
                memory_node,
                session_id=self._session.id,
                user_id=self._session.started_by_id,
                workflow_id=self._session.workflow_id,
            )
            prior_history = [
                # Restore PII tokens so the LLM sees real content — tool
                # dispatch (``lookup_customer(email)``) depends on it.
                {
                    **t.to_openai_message(),
                    "content": self._pii.restore_for_llm(
                        t.to_openai_message().get("content")
                    ),
                }
                for t in turns
            ]

        # 2) Persist + write the user turn to memory now that we've read.
        user_turn = Turn(role="user", content=content, ts=time.time())
        await self._maybe_append_memory(memory_node, mem_store, user_turn)
        await self._persist_message(role=ChatMessageRole.USER, content=content)

        # 3) Build the message array.
        messages = self._build_message_array(
            agent=agent,
            prior_history=prior_history,
            current_user_text=content,
        )

        # 3) Tool-loop until the LLM returns no tool_calls.
        prompt_tokens = 0
        completion_tokens = 0
        tool_call_count = 0
        final_text = ""
        for _ in range(_MAX_TOOL_LOOP_ITERATIONS):
            response = await llm.complete(
                messages=messages,
                tools=toolset.specs() if toolset.tools else None,
            )
            prompt_tokens += response.prompt_tokens or 0
            completion_tokens += response.completion_tokens or 0
            if response.tool_calls:
                # Persist the assistant's tool_calls + invoke them.
                await self._persist_message(
                    role=ChatMessageRole.ASSISTANT,
                    content=response.content,
                    tool_calls=response.tool_calls,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content or None,
                        "tool_calls": response.tool_calls,
                    }
                )
                handlers = toolset.handlers()
                for tc in response.tool_calls:
                    tool_call_count += 1
                    name = tc.get("function", {}).get("name", "")
                    raw_args = tc.get("function", {}).get("arguments", "{}")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                    handler = handlers.get(name)
                    if handler is None:
                        tool_result: dict[str, Any] = {
                            "ok": False,
                            "error": f"unknown tool {name!r}",
                        }
                    else:
                        try:
                            tool_result = await handler(args)
                        except Exception as exc:  # noqa: BLE001 — surface to LLM
                            log.exception("chat_runtime.tool_invocation_failed")
                            tool_result = {"ok": False, "error": str(exc)}
                    serialised = _safe_json(tool_result)
                    messages.append(
                        {
                            "role": "tool",
                            "content": serialised,
                            "tool_call_id": tc.get("id", ""),
                        }
                    )
                    await self._persist_message(
                        role=ChatMessageRole.TOOL,
                        content=serialised,
                        tool_call_id=tc.get("id", ""),
                        tool_name=name,
                    )
                    await self._maybe_append_memory(
                        memory_node,
                        mem_store,
                        Turn(
                            role="tool",
                            content=serialised,
                            tool_call_id=tc.get("id", ""),
                            tool_name=name,
                            ts=time.time(),
                        ),
                    )
                continue
            # No tool call → this is the final assistant turn.
            final_text = response.content or ""
            break
        else:
            log.warning(
                "chat_runtime.tool_loop_exhausted",
                session=str(self._session.id),
                agent=agent.id,
            )
            final_text = (
                "I wasn't able to finish that within the allotted reasoning "
                "budget. Could you try rephrasing?"
            )

        # 4) Output parser (if attached).
        structured: Any | None = None
        parser_result: ParseResult | None = None
        if parser_node is not None and final_text:
            async def reprompt(error_message: str) -> str:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your prior response failed schema validation: "
                            f"{error_message}. Reply again with ONLY corrected "
                            "JSON conforming to the schema."
                        ),
                    }
                )
                retry_resp = await llm.complete(
                    messages=messages,
                    tools=None,
                    response_format={"type": "json_object"},
                )
                nonlocal prompt_tokens, completion_tokens
                prompt_tokens += retry_resp.prompt_tokens or 0
                completion_tokens += retry_resp.completion_tokens or 0
                return retry_resp.content or ""

            parser_result = await parse_or_retry(
                node=parser_node,
                initial_text=final_text,
                reprompt=reprompt,
            )
            if parser_result.ok:
                structured = parser_result.value
                final_text = parser_result.raw_text or final_text
            else:
                # Validation failed even after retries — record but still
                # surface the agent's best-effort text so the customer
                # doesn't see silence.
                log.info(
                    "chat_runtime.output_parser_failed",
                    session=str(self._session.id),
                    error=parser_result.error,
                )

        # 5) Cost accounting (compute first so we can attach to the row).
        turn_microcents = self._record_cost(
            agent=agent,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        model_id_used = self._agent_model_id(agent)

        # 6) Persist + memory the final assistant turn.
        await self._persist_message(
            role=ChatMessageRole.ASSISTANT,
            content=final_text,
            prompt_tokens=prompt_tokens or None,
            completion_tokens=completion_tokens or None,
            parser_status=(
                "ok" if (parser_result and parser_result.ok) else
                "failed" if parser_result else None
            ),
            parser_error=parser_result.error if parser_result else None,
            cost_microcents=turn_microcents or None,
            model_id=model_id_used or None,
        )
        await self._maybe_append_memory(
            memory_node,
            mem_store,
            Turn(role="assistant", content=final_text, ts=time.time()),
        )

        # 7) Session bookkeeping + PII token-map flush. The flush MUST
        #    succeed AFTER the DB commit (otherwise a crash between the
        #    two leaves redacted rows without a token map to restore).
        self._session.total_messages += 1
        self._session.last_activity_at = datetime.now(timezone.utc)
        await self._db.commit()
        await self._pii.flush()

        return ChatTurnResult(
            assistant_text=final_text,
            structured=structured,
            parser=parser_result,
            tool_call_count=tool_call_count,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finished_at=self._session.last_activity_at,
        )

    # ------------------------------------------------------------------
    # Streaming variant
    # ------------------------------------------------------------------

    async def handle_user_message_stream(
        self, content: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the agent's response as SSE-shaped event dicts.

        Yields the following event types:

        * ``ready``               — session+agent metadata, sent first.
        * ``assistant_delta``     — incremental text from the LLM
                                     (``delta``: partial content).
        * ``tool_call``           — a fully-assembled tool call is about
                                     to be invoked (``id, name, args``).
        * ``tool_result``         — a tool returned (``id, name, result,
                                     duration_ms``).
        * ``parser_validating``   — output parser is running on a draft.
        * ``parser_retry``        — schema mismatch; re-prompting the LLM
                                     (``attempt, error``).
        * ``turn_complete``       — final telemetry + structured output.
        * ``error``               — terminal failure; the stream ends.

        Internally drives the same tool loop as ``handle_user_message``
        but uses ``LLMClient.complete_stream`` so the consumer sees text
        tokens as soon as they arrive. All persistence + memory writes
        happen at the same logical points (after the user turn, after
        each tool call, after the assistant turn finalises).
        """
        agent = self._resolve_agent()
        # Pre-flight rate-limit gate. Emit a structured terminal event
        # rather than raising — the streaming route is one-shot from the
        # client's perspective and rejecting via 429 is impossible once
        # the response headers have already been sent (which happens at
        # the very first ``yield``).
        try:
            await self._check_rate_limit()
        except RateLimitExceeded as exc:
            yield {
                "type": "rate_limited",
                "reason": exc.decision.reason,
                "retry_after_seconds": exc.decision.retry_after_seconds,
                "snapshot": exc.decision.snapshot,
            }
            return
        memory_node, mem_store = self._resolve_memory(agent)
        parser_node = self._resolve_parser(agent)
        toolset = build_toolset(
            workflow_definition=self._definition,
            agent=agent,
            workspace_connections=self._workspace_connections,
            workspace_id=self._session.workspace_id,
            workflow_id=self._session.workflow_id,
            db=self._db,
        )
        llm = self._llm or LLMClient(self._settings, agent.chat_model)

        yield {
            "type": "ready",
            "session_id": str(self._session.id),
            "agent_id": agent.id,
            "agent_name": agent.name,
            "tools": [t.name for t in toolset.tools],
            "memory_ref": agent.memory_ref or None,
            "output_parser_ref": agent.output_parser_ref or None,
        }

        # ---- 1) Read memory, persist user turn, build messages ----------
        await self._pii.load()
        prior_history: list[dict[str, Any]] = []
        if memory_node is not None and mem_store is not None:
            turns = await mem_store.read(
                memory_node,
                session_id=self._session.id,
                user_id=self._session.started_by_id,
                workflow_id=self._session.workflow_id,
            )
            prior_history = [
                {
                    **t.to_openai_message(),
                    "content": self._pii.restore_for_llm(
                        t.to_openai_message().get("content")
                    ),
                }
                for t in turns
            ]

        await self._maybe_append_memory(
            memory_node, mem_store,
            Turn(role="user", content=content, ts=time.time()),
        )
        await self._persist_message(role=ChatMessageRole.USER, content=content)

        messages = self._build_message_array(
            agent=agent,
            prior_history=prior_history,
            current_user_text=content,
        )

        # ---- 2) Stream the tool loop ------------------------------------
        prompt_tokens = 0
        completion_tokens = 0
        tool_call_count = 0
        final_text = ""

        for _ in range(_MAX_TOOL_LOOP_ITERATIONS):
            # Drive one streaming round: text deltas + accumulating tool
            # call slots. We inline rather than extract a helper so the
            # ``yield`` semantics stay in one place — every event goes
            # straight to the SSE consumer with no buffering overhead.
            partial_content: list[str] = []
            partial_tool_calls: dict[int, dict[str, Any]] = {}
            round_prompt_tokens = 0
            round_completion_tokens = 0
            round_finish: str | None = None

            try:
                async for ch in llm.complete_stream(
                    messages=messages,
                    tools=toolset.specs() if toolset.tools else None,
                ):
                    if ch.content_delta:
                        partial_content.append(ch.content_delta)
                        yield {
                            "type": "assistant_delta",
                            "delta": ch.content_delta,
                        }
                    if ch.tool_call_index is not None and ch.tool_call_delta:
                        slot = partial_tool_calls.setdefault(
                            ch.tool_call_index,
                            {"id": "", "type": "function",
                             "function": {"name": "", "arguments": ""}},
                        )
                        d = ch.tool_call_delta
                        if d.get("id"):
                            slot["id"] = d["id"]
                        fn_delta = d.get("function") or {}
                        if "name" in fn_delta and fn_delta["name"]:
                            slot["function"]["name"] = (
                                slot["function"]["name"] + fn_delta["name"]
                            )
                        if "arguments" in fn_delta and fn_delta["arguments"]:
                            slot["function"]["arguments"] = (
                                slot["function"]["arguments"]
                                + fn_delta["arguments"]
                            )
                    if ch.prompt_tokens:
                        round_prompt_tokens += ch.prompt_tokens
                    if ch.completion_tokens:
                        round_completion_tokens += ch.completion_tokens
                    if ch.finish:
                        round_finish = ch.finish
            except Exception as exc:  # noqa: BLE001 — surface as terminal event
                log.exception("chat_runtime.stream_failed")
                yield {"type": "error", "message": str(exc)}
                return

            prompt_tokens += round_prompt_tokens
            completion_tokens += round_completion_tokens

            assembled_text = "".join(partial_content)
            tool_calls = (
                [partial_tool_calls[i] for i in sorted(partial_tool_calls)]
                if partial_tool_calls else []
            )
            if tool_calls and (
                round_finish == "tool_calls" or round_finish is None
            ):
                # Persist assistant message with tool_calls and invoke them.
                await self._persist_message(
                    role=ChatMessageRole.ASSISTANT,
                    content=assembled_text,
                    tool_calls=tool_calls,
                    prompt_tokens=round_prompt_tokens or None,
                    completion_tokens=round_completion_tokens or None,
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": assembled_text or None,
                        "tool_calls": tool_calls,
                    }
                )
                handlers = toolset.handlers()
                for tc in tool_calls:
                    tool_call_count += 1
                    name = tc.get("function", {}).get("name", "")
                    raw_args = tc.get("function", {}).get("arguments", "{}")
                    try:
                        args = (
                            json.loads(raw_args)
                            if isinstance(raw_args, str) and raw_args.strip()
                            else (raw_args or {})
                        )
                    except json.JSONDecodeError:
                        args = {}
                    yield {
                        "type": "tool_call",
                        "id": tc.get("id", ""),
                        "name": name,
                        "args": args,
                    }
                    handler = handlers.get(name)
                    started = time.time()
                    if handler is None:
                        tool_result: dict[str, Any] = {
                            "ok": False, "error": f"unknown tool {name!r}"
                        }
                    else:
                        try:
                            tool_result = await handler(args)
                        except Exception as exc:  # noqa: BLE001 — show to LLM
                            log.exception("chat_runtime.stream_tool_failed")
                            tool_result = {"ok": False, "error": str(exc)}
                    duration_ms = int((time.time() - started) * 1000)
                    serialised = _safe_json(tool_result)
                    messages.append(
                        {
                            "role": "tool",
                            "content": serialised,
                            "tool_call_id": tc.get("id", ""),
                        }
                    )
                    await self._persist_message(
                        role=ChatMessageRole.TOOL,
                        content=serialised,
                        tool_call_id=tc.get("id", ""),
                        tool_name=name,
                    )
                    await self._maybe_append_memory(
                        memory_node, mem_store,
                        Turn(
                            role="tool",
                            content=serialised,
                            tool_call_id=tc.get("id", ""),
                            tool_name=name,
                            ts=time.time(),
                        ),
                    )
                    yield {
                        "type": "tool_result",
                        "id": tc.get("id", ""),
                        "name": name,
                        "result": tool_result,
                        "duration_ms": duration_ms,
                    }
                continue

            # No tool calls → this is the final assistant text.
            final_text = assembled_text
            break
        else:
            log.warning(
                "chat_runtime.stream_tool_loop_exhausted",
                session=str(self._session.id),
            )
            final_text = (
                "I wasn't able to finish that within the allotted reasoning "
                "budget. Could you try rephrasing?"
            )

        # ---- 3) Output parser validation -------------------------------
        structured: Any | None = None
        parser_result: ParseResult | None = None
        if parser_node is not None and final_text:
            yield {"type": "parser_validating"}

            # We need to (a) re-prompt the LLM with a corrective message,
            # (b) collect the corrected reply via streaming so token usage
            # is still accumulated, and (c) emit a ``parser_retry`` SSE
            # event for the client. ``parse_or_retry`` calls ``reprompt``
            # synchronously inside its loop, so it can't ``yield`` for us;
            # we record retry events and flush them after the parser
            # returns.
            retry_events: list[dict[str, Any]] = []

            async def reprompt(error_message: str) -> str:
                nonlocal prompt_tokens, completion_tokens
                retry_events.append(
                    {"type": "parser_retry", "error": error_message}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your prior response failed schema validation: "
                            f"{error_message}. Reply again with ONLY corrected "
                            "JSON conforming to the schema."
                        ),
                    }
                )
                buf: list[str] = []
                async for ch in llm.complete_stream(
                    messages=messages,
                    tools=None,
                    response_format={"type": "json_object"},
                ):
                    if ch.content_delta:
                        buf.append(ch.content_delta)
                    if ch.prompt_tokens:
                        prompt_tokens += ch.prompt_tokens
                    if ch.completion_tokens:
                        completion_tokens += ch.completion_tokens
                return "".join(buf)

            parser_result = await parse_or_retry(
                node=parser_node,
                initial_text=final_text,
                reprompt=reprompt,
            )
            for ev in retry_events:
                yield ev
            if parser_result.ok:
                structured = parser_result.value
                final_text = parser_result.raw_text or final_text

        # ---- 4) Cost accounting first (so we can attach to the row) --
        turn_microcents = self._record_cost(
            agent=agent,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        model_id_used = self._agent_model_id(agent)

        # ---- 5) Persist + commit --------------------------------------
        await self._persist_message(
            role=ChatMessageRole.ASSISTANT,
            content=final_text,
            prompt_tokens=prompt_tokens or None,
            completion_tokens=completion_tokens or None,
            parser_status=(
                "ok" if (parser_result and parser_result.ok) else
                "failed" if parser_result else None
            ),
            parser_error=parser_result.error if parser_result else None,
            cost_microcents=turn_microcents or None,
            model_id=model_id_used or None,
        )
        # Backfill cost on the most recent assistant row. Cheapest path is
        # to just write it as a fresh row — the runtime persisted the
        # finalised assistant message earlier; we do that here in one go.
        await self._maybe_append_memory(
            memory_node, mem_store,
            Turn(role="assistant", content=final_text, ts=time.time()),
        )
        self._session.total_messages += 1
        self._session.last_activity_at = datetime.now(timezone.utc)
        await self._db.commit()
        await self._pii.flush()

        yield {
            "type": "turn_complete",
            "assistant_text": final_text,
            "structured": structured,
            "cost_cents": microcents_to_cents(turn_microcents),
            "model_id": model_id_used,
            "parser_status": (
                "ok" if (parser_result and parser_result.ok) else
                "failed" if parser_result else None
            ),
            "parser_error": parser_result.error if parser_result else None,
            "tool_call_count": tool_call_count,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_agent(self) -> AgentNode:
        for n in self._definition.iter_nodes():
            if n.id == self._session.agent_node_id and isinstance(n, AgentNode):
                return n
        raise RuntimeError(
            f"agent node {self._session.agent_node_id!r} no longer exists in "
            f"workflow {self._session.workflow_id} — schema may have changed "
            "since the session was opened"
        )

    def _resolve_memory(
        self, agent: AgentNode
    ) -> tuple[MemoryNode | None, MemoryStore | None]:
        if not agent.memory_ref:
            return None, None
        for n in self._definition.iter_nodes():
            if n.id == agent.memory_ref and isinstance(n, MemoryNode):
                return n, MemoryStore(get_redis())
        return None, None

    def _resolve_parser(self, agent: AgentNode) -> OutputParserNode | None:
        if not agent.output_parser_ref:
            return None
        for n in self._definition.iter_nodes():
            if n.id == agent.output_parser_ref and isinstance(n, OutputParserNode):
                return n
        return None

    async def _maybe_append_memory(
        self,
        node: MemoryNode | None,
        store: MemoryStore | None,
        turn: Turn,
    ) -> None:
        if node is None or store is None:
            return
        # Redact before persistence — memory list is durable + cross-process.
        redacted = Turn(
            role=turn.role,
            content=self._pii.redact_for_persistence(turn.content) or "",
            tool_calls=turn.tool_calls,
            tool_call_id=turn.tool_call_id,
            tool_name=turn.tool_name,
            ts=turn.ts,
        )
        await store.append(
            node,
            redacted,
            session_id=self._session.id,
            user_id=self._session.started_by_id,
            workflow_id=self._session.workflow_id,
        )

    def _build_message_array(
        self,
        *,
        agent: AgentNode,
        prior_history: list[dict[str, Any]],
        current_user_text: str,
    ) -> list[dict[str, Any]]:
        """System prompt + prior memory turns + current user message."""
        system_parts: list[str] = []
        if agent.role.strip():
            system_parts.append(f"# Role\n{agent.role.strip()}")
        if agent.instructions.strip():
            system_parts.append(f"# Instructions\n{agent.instructions.strip()}")
        trig = self._chat_trigger()
        if trig and trig.chat_welcome_message:
            system_parts.append(
                f"# Greeting you opened with\n{trig.chat_welcome_message.strip()}"
            )
        system_text = "\n\n".join(system_parts) or "You are a helpful assistant."

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_text}]
        messages.extend(prior_history)
        messages.append({"role": "user", "content": current_user_text})
        return messages

    def _chat_trigger(self) -> TriggerNode | None:
        for n in self._definition.iter_nodes():
            if (
                isinstance(n, TriggerNode)
                and n.slug == self._session.trigger_slug
                and n.trigger_type == "chat"
            ):
                return n
        return None

    def _rate_limit_config(self) -> RateLimitConfig | None:
        trig = self._chat_trigger()
        if trig is None:
            return None
        return RateLimitConfig.from_dict(trig.rate_limits)

    async def _check_rate_limit(self) -> None:
        """Pre-flight check; raises ``RateLimitExceeded`` on rejection."""
        config = self._rate_limit_config()
        if config is None:
            return
        limiter = RateLimiter(get_redis())
        decision = await limiter.check(config=config, session=self._session)
        if not decision.allowed:
            raise RateLimitExceeded(decision)
        await limiter.record(config=config, session_id=self._session.id)

    def _agent_model_id(self, agent: AgentNode) -> str:
        return (
            (agent.chat_model or {}).get("model")
            if agent.chat_model
            else None
        ) or (
            self._settings.AZURE_OPENAI_DEPLOYMENT
            or self._settings.AZURE_OPENAI_DEFAULT_MODEL
            or ""
        )

    def _record_cost(
        self,
        *,
        agent: AgentNode,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> int:
        """Add this turn's token + cost totals to the session row. Returns
        the per-turn cost in micro-cents (caller persists it on the
        assistant message row)."""
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return 0
        model_id = self._agent_model_id(agent)
        turn_microcents = cost_microcents(
            model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        self._session.total_prompt_tokens += prompt_tokens
        self._session.total_completion_tokens += completion_tokens
        self._session.total_cost_microcents += turn_microcents
        return turn_microcents

    async def _persist_message(
        self,
        *,
        role: ChatMessageRole,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        parser_status: str | None = None,
        parser_error: str | None = None,
        cost_microcents: int | None = None,
        model_id: str | None = None,
    ) -> None:
        # Redact content + tool-call arguments before INSERT. The token
        # map is flushed to Redis once per turn (see ``handle_user_message``
        # / ``handle_user_message_stream`` tails) so subsequent requests
        # can restore on read.
        redacted_content = self._pii.redact_for_persistence(content) or ""
        redacted_tool_calls = (
            self._redact_tool_calls(tool_calls) if tool_calls else None
        )
        row = ChatMessage(
            session_id=self._session.id,
            role=role,
            content=redacted_content,
            tool_calls=redacted_tool_calls,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            parser_status=parser_status,
            parser_error=parser_error,
            cost_microcents=cost_microcents,
            model_id=model_id,
        )
        self._db.add(row)
        await self._db.flush()

    def _redact_tool_calls(
        self, tool_calls: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Redact the ``arguments`` field of every tool call.

        Tool-call arguments often carry the raw values the LLM is acting
        on (the customer's email, etc.). We redact just the arguments
        string — name + id stay verbatim so the audit log is still
        navigable.
        """
        out: list[dict[str, Any]] = []
        for tc in tool_calls:
            new = dict(tc)
            fn = dict(new.get("function") or {})
            if isinstance(fn.get("arguments"), str):
                fn["arguments"] = self._pii.redact_for_persistence(
                    fn["arguments"]
                ) or ""
            new["function"] = fn
            out.append(new)
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json(val: Any) -> str:
    try:
        return json.dumps(val, default=str)
    except (TypeError, ValueError):
        return str(val)


# ---------------------------------------------------------------------------
# Memory pre-loader — used by the API route to populate the messages array
# with prior turns before the runtime is invoked.
# ---------------------------------------------------------------------------


async def load_memory_for_messages(
    *,
    definition: WorkflowDefinition,
    session: ChatSession,
) -> list[dict[str, Any]]:
    """Return prior turns in OpenAI message shape for use in ``handle_user_message``.

    Lives outside the runtime so it can be awaited from the caller without
    threading a coroutine through ``_build_message_array``.
    """
    for n in definition.iter_nodes():
        if n.id == session.agent_node_id and isinstance(n, AgentNode):
            agent = n
            break
    else:
        return []
    if not agent.memory_ref:
        return []
    memory_node: MemoryNode | None = None
    for n in definition.iter_nodes():
        if n.id == agent.memory_ref and isinstance(n, MemoryNode):
            memory_node = n
            break
    if memory_node is None:
        return []
    store = MemoryStore(get_redis())
    turns = await store.read(
        memory_node,
        session_id=session.id,
        user_id=session.started_by_id,
        workflow_id=session.workflow_id,
    )
    return [t.to_openai_message() for t in turns]


__all__ = ["ChatRuntime", "ChatTurnResult", "LLMClient", "load_memory_for_messages"]
