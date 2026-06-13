"""Native Dynamiq connections (API-key / OAuth) stored per workspace.

Distinct from ``models.integration.Integration`` (which is Composio-proxied) — these
rows back tools that resolve directly to ``dynamiq.connections.*`` + ``dynamiq.nodes.tools.*``
with no third-party hop. Composio remains available as a fallback when no native
provider is registered.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from models.user import User
    from models.workspace import Workspace


class NativeConnectionStatus(str, enum.Enum):
    ACTIVE = "active"
    ERROR = "error"
    REVOKED = "revoked"


class NativeConnectionAuthType(str, enum.Enum):
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    MCP_SSE = "mcp_sse"


class NativeConnection(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "native_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "provider", "name", name="uq_native_conn_ws_provider_name"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    auth_type: Mapped[NativeConnectionAuthType] = mapped_column(
        SAEnum(
            NativeConnectionAuthType,
            name="native_conn_auth_type",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        default=NativeConnectionAuthType.API_KEY,
    )
    status: Mapped[NativeConnectionStatus] = mapped_column(
        SAEnum(
            NativeConnectionStatus,
            name="native_conn_status",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        default=NativeConnectionStatus.ACTIVE,
        server_default=NativeConnectionStatus.ACTIVE.value,
    )
    config_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workspace: Mapped["Workspace"] = relationship()
    owner: Mapped["User"] = relationship()


__all__ = [
    "NativeConnection",
    "NativeConnectionAuthType",
    "NativeConnectionStatus",
]
