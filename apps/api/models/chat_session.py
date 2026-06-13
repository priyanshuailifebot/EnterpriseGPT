"""Persistent chat sessions and turn-by-turn message history.

A ``ChatSession`` is opened by a public POST to ``/chat/{trigger_slug}/sessions``
on a workflow that contains a chat trigger. The session is then driven by
``ChatRuntime`` — each inbound user message becomes one ``ChatMessage`` row,
each assistant response another, and each tool call/result a third kind.

Conversation memory at runtime is read/written through ``MemoryStore`` (Redis,
keyed by ``session_id``). The DB rows here are the **durable audit trail** —
Redis can expire / be flushed without losing the conversation.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from models.user import User
    from models.workflow import Workflow
    from models.workspace import Workspace


class ChatSessionStatus(str, enum.Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"


class ChatMessageRole(str, enum.Enum):
    """Roles match the OpenAI / Anthropic tool-calling protocol exactly.

    ``tool`` rows carry the *result* of a tool call (with ``tool_call_id``
    pointing at the assistant message that initiated it). Tool *calls* are
    embedded inside the ``assistant`` row's ``tool_calls`` JSON.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ChatSession(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_chat_sessions_workspace_workflow", "workspace_id", "workflow_id"),
        Index("ix_chat_sessions_trigger_slug", "trigger_slug"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workflow_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Slug of the chat trigger inside the workflow definition that owns
    # this session. One workflow can have multiple chat triggers (e.g.
    # public + internal), each with its own session pool.
    trigger_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    # Node id of the AgentNode the chat trigger feeds into. Cached on the
    # session at open-time so we don't have to walk the graph per message.
    agent_node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Optional bound user (anonymous sessions leave this null).
    started_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[ChatSessionStatus] = mapped_column(
        SAEnum(
            ChatSessionStatus,
            name="chat_session_status",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        default=ChatSessionStatus.ACTIVE,
        server_default=ChatSessionStatus.ACTIVE.value,
    )
    # Arbitrary metadata supplied at session-open time — useful for binding
    # an external user id, customer email, locale, etc. Not used by the
    # runtime directly, exposed back to the agent via memory.
    metadata_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict,
    )
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # ---- Cost telemetry (cumulative across all turns in the session) ----
    # ``total_cost_microcents`` keeps sub-cent precision so many small
    # turns aggregate accurately; the API surfaces whole cents via
    # ``services.llm_pricing.microcents_to_cents``.
    total_prompt_tokens: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0",
    )
    total_completion_tokens: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0",
    )
    total_cost_microcents: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0",
    )
    total_messages: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0",
    )

    workspace: Mapped["Workspace"] = relationship()
    workflow: Mapped["Workflow"] = relationship()
    started_by: Mapped["User | None"] = relationship()


class ChatMessage(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
    )

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[ChatMessageRole] = mapped_column(
        SAEnum(
            ChatMessageRole,
            name="chat_message_role",
            native_enum=False,
            length=16,
        ),
        nullable=False,
    )
    # The natural-language content. For ``tool`` rows this is the tool's
    # serialised result. For ``assistant`` rows it may be empty when the
    # turn was nothing but tool_calls.
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Assistant tool-call instructions (OpenAI shape:
    #   [{ id, type: "function", function: { name, arguments } }, ...])
    # Lives on the ``assistant`` row that triggered the call. Optional.
    tool_calls: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Set on ``tool`` rows — points at the assistant row's tool_call.id.
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Token/cost telemetry — populated for assistant rows when the LLM
    # response includes usage data. Left null for user/tool rows.
    prompt_tokens: Mapped[int | None] = mapped_column(nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(nullable=True)
    # Per-turn cost in micro-cents. Sub-cent precision matters when
    # individual turns frequently price below 1¢ (gpt-4o-mini etc.).
    cost_microcents: Mapped[int | None] = mapped_column(nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Validation outcome from the OutputParser. Null when no parser is
    # attached or the assistant turn was a tool-call.
    parser_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parser_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    session: Mapped[ChatSession] = relationship()


__all__ = [
    "ChatMessage",
    "ChatMessageRole",
    "ChatSession",
    "ChatSessionStatus",
]
