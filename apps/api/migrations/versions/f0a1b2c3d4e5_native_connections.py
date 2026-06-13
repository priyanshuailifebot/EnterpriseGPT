"""Phase A — native_connections table for direct Dynamiq connectors."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, None] = "b2c4d6e8f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "native_connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("auth_type", sa.String(length=32), nullable=False, server_default="api_key"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("config_encrypted", sa.Text(), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_error", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "provider", "name", name="uq_native_conn_ws_provider_name"
        ),
    )
    op.create_index(
        "ix_native_connections_workspace_id", "native_connections", ["workspace_id"]
    )
    op.create_index(
        "ix_native_connections_created_by_id", "native_connections", ["created_by_id"]
    )
    op.create_index(
        "ix_native_connections_provider", "native_connections", ["provider"]
    )


def downgrade() -> None:
    op.drop_index("ix_native_connections_provider", table_name="native_connections")
    op.drop_index("ix_native_connections_created_by_id", table_name="native_connections")
    op.drop_index("ix_native_connections_workspace_id", table_name="native_connections")
    op.drop_table("native_connections")
