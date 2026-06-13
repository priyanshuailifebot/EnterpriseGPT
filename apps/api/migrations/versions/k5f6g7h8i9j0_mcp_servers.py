"""Per-workspace MCP server registry."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "k5f6g7h8i9j0"
down_revision: Union[str, None] = "j4e5f6g7h8i9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "workspace_id", sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_id", sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("transport", sa.String(length=32), nullable=False),
        sa.Column(
            "status", sa.String(length=32),
            server_default="active", nullable=False,
        ),
        sa.Column("config_encrypted", sa.Text(), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_error", sa.Text(), nullable=True),
        sa.Column("last_tool_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_mcp_server_workspace_name"),
    )
    op.create_index("ix_mcp_servers_workspace_id", "mcp_servers", ["workspace_id"])
    op.create_index("ix_mcp_servers_created_by_id", "mcp_servers", ["created_by_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_servers_created_by_id", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_workspace_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
