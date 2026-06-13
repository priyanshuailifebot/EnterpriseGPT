"""Per-workspace MCP server registry.

A row here represents one MCP-protocol endpoint the workspace can call —
typically Composio's hosted MCP (``connect.composio.dev/mcp``) but the same
table supports self-hosted servers, Smithery, Pipedream MCP, etc.

The ``config_encrypted`` field holds a JSON blob with the API key and any
extra headers, encrypted at rest via ``core.crypto`` (same pattern as
``NativeConnection``).
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


class MCPServerTransport(str, enum.Enum):
    STREAMABLE_HTTP = "streamable-http"
    SSE = "sse"


class MCPServerStatus(str, enum.Enum):
    ACTIVE = "active"
    ERROR = "error"
    DISABLED = "disabled"


class MCPServer(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_mcp_server_workspace_name"),
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

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    transport: Mapped[MCPServerTransport] = mapped_column(
        SAEnum(
            MCPServerTransport,
            name="mcp_server_transport",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        default=MCPServerTransport.STREAMABLE_HTTP,
    )
    status: Mapped[MCPServerStatus] = mapped_column(
        SAEnum(
            MCPServerStatus,
            name="mcp_server_status",
            native_enum=False,
            length=32,
        ),
        nullable=False,
        default=MCPServerStatus.ACTIVE,
        server_default=MCPServerStatus.ACTIVE.value,
    )

    # JSON-encoded {auth_header_name, auth_header_value, extra_headers}
    config_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tool_count: Mapped[int | None] = mapped_column(nullable=True)

    workspace: Mapped["Workspace"] = relationship()
    owner: Mapped["User"] = relationship()


__all__ = ["MCPServer", "MCPServerStatus", "MCPServerTransport"]
