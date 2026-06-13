"""Workspace-scoped key/value table written to by ``data_store`` nodes.

A single JSONB-backed table powers what the n8n screenshot called
"Store Candidates in Dashboard", "Store Interview Schedule",
"Store Interview Results", "Update Ranking in Dashboard" etc. — every one
of those is the same primitive (an upsert keyed by a string into a named
table). The Next.js Data Tables page renders any of these rows as a
generic sortable grid; there is no per-workflow UI to write.

Rows are uniquely identified by (workspace_id, table, key). When the
workflow author leaves ``key`` blank in the ``data_store`` node, we
synthesise a per-execution uuid so multiple rows accumulate.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from models.workspace import Workspace


class WorkflowData(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "workflow_data"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "table_name", "row_key",
            name="uq_workflow_data_ws_tbl_key",
        ),
        Index("ix_workflow_data_ws_tbl", "workspace_id", "table_name"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    row_key: Mapped[str] = mapped_column(String(256), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    last_workflow_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="SET NULL"),
        nullable=True,
    )

    workspace: Mapped["Workspace"] = relationship()


__all__ = ["WorkflowData"]
