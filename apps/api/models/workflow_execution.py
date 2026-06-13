"""Single workflow run — Dynamo execution record + SSE / HITL lifecycles."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import UUIDPKMixin

if TYPE_CHECKING:
    from models.user import User
    from models.workflow import Workflow
    from models.workflow_execution_step import WorkflowExecutionStep


class WorkflowExecutionStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    HITL_WAITING = "hitl_waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowExecution(UUIDPKMixin, Base):
    __tablename__ = "workflow_executions"

    workflow_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[WorkflowExecutionStatus] = mapped_column(
        SAEnum(
            WorkflowExecutionStatus,
            name="workflow_execution_status",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        default=WorkflowExecutionStatus.PENDING,
        server_default=WorkflowExecutionStatus.PENDING.value,
    )
    input_data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    output_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # True for demo/test runs. These rows are persisted for inspection but
    # excluded from the default executions listing so they never count as
    # production runs.
    demo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    agent_states: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    workflow: Mapped["Workflow"] = relationship(back_populates="executions")
    starter: Mapped["User"] = relationship()
    steps: Mapped[list["WorkflowExecutionStep"]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<WorkflowExecution id={self.id} status={self.status!r}>"
