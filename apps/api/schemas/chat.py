"""Request / response schemas for the public chat API."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OpenSessionRequest(BaseModel):
    """Body for ``POST /chat/{trigger_slug}/sessions``.

    Public endpoint by design — the slug is the discovery mechanism, the
    chat trigger's optional shared secret is the auth. ``metadata`` lets
    the embedding page tag the session with a customer id, locale, etc.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    workflow_id: UUID
    metadata: dict[str, Any] = Field(default_factory=dict)
    secret: str = Field(default="", max_length=256)


class OpenSessionResponse(BaseModel):
    session_id: UUID
    workspace_id: UUID
    workflow_id: UUID
    trigger_slug: str
    agent_node_id: str
    welcome_message: str | None
    created_at: datetime


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=16000)


class SendMessageResponse(BaseModel):
    session_id: UUID
    assistant_text: str
    structured: Any | None = None
    parser_status: str | None = None
    parser_error: str | None = None
    tool_call_count: int
    prompt_tokens: int
    completion_tokens: int


class ChatMessageOut(BaseModel):
    id: UUID
    role: str
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    created_at: datetime
    parser_status: str | None = None

    model_config = ConfigDict(from_attributes=True)


class ChatMessageListResponse(BaseModel):
    items: list[ChatMessageOut]
    total: int


class MemoryInspectResponse(BaseModel):
    scope: str
    scope_id: str | None
    count: int
    ttl: int
    max_turns: int


class SessionUsageResponse(BaseModel):
    """Cumulative cost telemetry for one chat session."""

    session_id: UUID
    total_prompt_tokens: int
    total_completion_tokens: int
    total_messages: int
    total_cost_cents: int
    # Limits configured on the trigger; surfaced so the UI can render a
    # progress bar / "X of Y tokens used" badge.
    rate_limits: dict[str, Any] | None = None


class SessionListItem(BaseModel):
    id: UUID
    workspace_id: UUID
    workflow_id: UUID
    trigger_slug: str
    agent_node_id: str
    status: str
    total_messages: int
    total_cost_cents: int
    last_activity_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SessionListResponse(BaseModel):
    items: list[SessionListItem]
    total: int


class RateLimitedResponse(BaseModel):
    """Wire shape for 429 responses from the non-streaming send route."""

    detail: str
    reason: str
    retry_after_seconds: int | None = None
    snapshot: dict[str, Any] | None = None


__all__ = [
    "ChatMessageListResponse",
    "ChatMessageOut",
    "MemoryInspectResponse",
    "OpenSessionRequest",
    "OpenSessionResponse",
    "RateLimitedResponse",
    "SendMessageRequest",
    "SendMessageResponse",
    "SessionListItem",
    "SessionListResponse",
    "SessionUsageResponse",
]
