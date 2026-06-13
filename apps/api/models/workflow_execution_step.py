"""Per-node execution record — one row per node a run actually executed.

Powers the n8n-style "click a node, inspect what flowed in and out" drawer
and the ``GET /workflows/{id}/executions/{exec_id}/steps`` endpoint. Rows are
written from the SSE stream loop as ``node_complete`` events arrive (for both
real and demo runs) and committed alongside the parent ``WorkflowExecution``.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import UUIDPKMixin

if TYPE_CHECKING:
    from models.workflow_execution import WorkflowExecution


class WorkflowExecutionStepStatus(str, enum.Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowExecutionStep(UUIDPKMixin, Base):
    __tablename__ = "workflow_execution_steps"

    execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Position within the run, in emission order. Per-stream local counter —
    # not globally unique, scoped to one execution_id.
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Nullable: synthesized node_complete events (native/HITL paths) may not
    # carry a human name; never aliased from node_kind.
    node_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    node_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[WorkflowExecutionStepStatus] = mapped_column(
        SAEnum(
            WorkflowExecutionStepStatus,
            name="workflow_execution_step_status",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        default=WorkflowExecutionStepStatus.COMPLETED,
        server_default=WorkflowExecutionStepStatus.COMPLETED.value,
    )
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # True for rows produced by demo/test runs — excluded from the default
    # executions listing so they never masquerade as production runs.
    demo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    input_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    execution: Mapped["WorkflowExecution"] = relationship(back_populates="steps")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<WorkflowExecutionStep id={self.id} node_id={self.node_id!r} "
            f"status={self.status!r}>"
        )
