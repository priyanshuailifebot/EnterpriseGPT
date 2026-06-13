"""Human handoff — paused chat sessions awaiting a real-human reply.

Phase 2d minimum-viable handoff:

* Agent at runtime decides "this needs a human" (typically by calling a
  ``HumanHandoffNode`` satellite as a tool).
* The runtime creates a ``HumanHandoffQueueItem`` row referencing the
  session + the customer's last message + an optional reason.
* The chat session's status flips to ``HITL_WAITING`` (re-using the
  existing enum), the SSE stream emits a terminal ``handoff_requested``
  event, the client renders a "We're getting a human" message.
* Human agents poll ``GET /chat/handoff/queue?workspace_id=…`` and
  ``POST /chat/handoff/queue/{id}/claim``.
* The human posts the reply via the standard
  ``POST /chat/sessions/{id}/messages`` route with a special header
  (``X-Handoff-Operator: <user_id>``); the runtime persists it as a
  human-role assistant message, marks the queue item resolved, and the
  customer's panel rehydrates it on next refresh.

This is intentionally minimal. A real CX deployment would add: agent
availability calendars, queue routing rules, SLA timers, transfer-to-
another-agent, supervisor takeover. All of those are layered on top of
this row.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from models.chat_session import ChatSession
    from models.user import User
    from models.workspace import Workspace


class HandoffStatus(str, enum.Enum):
    PENDING = "pending"      # waiting for a human to claim
    CLAIMED = "claimed"      # a human is on it
    RESOLVED = "resolved"    # human responded — session resumes
    CANCELLED = "cancelled"  # customer left / session reset


class HumanHandoffQueueItem(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "human_handoff_queue"
    __table_args__ = (
        Index(
            "ix_handoff_workspace_status_created",
            "workspace_id",
            "status",
            "created_at",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Snapshot of the customer's last message so the human agent has
    # context even if memory has rolled off.
    customer_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[HandoffStatus] = mapped_column(
        SAEnum(
            HandoffStatus,
            name="handoff_status",
            native_enum=False,
            length=16,
        ),
        nullable=False,
        default=HandoffStatus.PENDING,
        server_default=HandoffStatus.PENDING.value,
    )
    claimed_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")

    workspace: Mapped["Workspace"] = relationship()
    session: Mapped["ChatSession"] = relationship()
    claimed_by: Mapped["User | None"] = relationship()


__all__ = ["HandoffStatus", "HumanHandoffQueueItem"]
