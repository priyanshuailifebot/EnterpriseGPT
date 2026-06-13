"""Structured logging for Composio tool calls tied to workflow executions."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import UUIDPKMixin

if TYPE_CHECKING:
    from models.workflow_execution import WorkflowExecution


class ToolExecutionLog(UUIDPKMixin, Base):
    __tablename__ = "tool_execution_logs"

    execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(512), nullable=False)
    input_params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    output_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )

    execution: Mapped["WorkflowExecution | None"] = relationship()
