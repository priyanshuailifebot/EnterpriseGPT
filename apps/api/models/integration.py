"""Composio-backed OAuth integrations scoped to workspaces."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from models.user import User
    from models.workspace import Workspace


class IntegrationStatus(str, enum.Enum):
    PENDING = "pending"
    CONNECTED = "connected"
    ERROR = "error"
    REVOKED = "revoked"


class Integration(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "integrations"

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    composio_entity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    composio_connection_id: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[IntegrationStatus] = mapped_column(
        SAEnum(
            IntegrationStatus,
            name="integration_status",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        default=IntegrationStatus.PENDING,
        server_default=IntegrationStatus.PENDING.value,
    )
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workspace: Mapped["Workspace"] = relationship()
    owner: Mapped["User"] = relationship()
