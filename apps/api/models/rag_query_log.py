"""Per-query RAG analytics (workspace-scoped; fed from POST /documents/query)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import UUIDPKMixin

if TYPE_CHECKING:
    from models.workspace import Workspace


class RagQueryLog(UUIDPKMixin, Base):
    __tablename__ = "rag_query_logs"

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    unanswerable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    citation_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    top_document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()", index=True
    )
    question_excerpt: Mapped[str | None] = mapped_column(String(512), nullable=True)

    workspace: Mapped["Workspace"] = relationship()
