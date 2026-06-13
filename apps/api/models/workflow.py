"""Saved workflow (logical entity) per workspace — multi-version definitions."""

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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import TimestampMixin, UUIDPKMixin


class WorkflowStatus(str, enum.Enum):
    """Publish lifecycle. Gates whether real-world side effects may fire.

    * ``draft``     — building/testing; side-effecting actions are always
                      previewed, never executed, regardless of run mode.
    * ``published`` — live; a production (non-demo) run performs real actions.
    * ``archived``  — retired; cannot run live.
    """

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"

if TYPE_CHECKING:
    from models.user import User
    from models.workflow_execution import WorkflowExecution
    from models.workflow_version import WorkflowVersion
    from models.workspace import Workspace


class Workflow(UUIDPKMixin, TimestampMixin, Base):
    """User-designed agentic workflow persisted for a workspace."""

    __tablename__ = "workflows"
    __table_args__ = (UniqueConstraint("workspace_id", "slug", name="uq_workflows_workspace_slug"),)

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    status: Mapped[WorkflowStatus] = mapped_column(
        SAEnum(
            WorkflowStatus,
            name="workflow_status",
            native_enum=False,
            length=32,
            # Store the lowercase VALUES ("draft", …) rather than the enum
            # NAMES, matching the migration's server_default and so existing
            # rows read back cleanly.
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=WorkflowStatus.DRAFT,
        server_default=WorkflowStatus.DRAFT.value,
    )
    # When published: the version that went live + audit stamps.
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    workspace: Mapped["Workspace"] = relationship()
    creator: Mapped["User"] = relationship()
    versions: Mapped[list["WorkflowVersion"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowVersion.version",
        # Disambiguate: ``published_version_id`` adds a second FK path between
        # workflows and workflow_versions, so this relationship must declare
        # which FK column links the children back to the parent.
        foreign_keys="WorkflowVersion.workflow_id",
    )
    executions: Mapped[list["WorkflowExecution"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Workflow id={self.id} slug={self.slug!r} v={self.current_version}>"
