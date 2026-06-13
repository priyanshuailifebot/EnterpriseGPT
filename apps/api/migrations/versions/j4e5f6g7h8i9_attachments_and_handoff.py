"""Phase 2d — chat_attachments + human_handoff_queue."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "j4e5f6g7h8i9"
down_revision: Union[str, None] = "i3d4e5f6g7h8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_attachments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "session_id", sa.UUID(),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id", sa.UUID(),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("bucket", sa.String(length=128), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column(
            "uploaded_by_id", sa.UUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_attachments_session_id", "chat_attachments", ["session_id"])
    op.create_index(
        "ix_chat_attachments_session_created",
        "chat_attachments",
        ["session_id", "created_at"],
    )

    op.create_table(
        "human_handoff_queue",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "workspace_id", sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id", sa.UUID(),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("customer_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "status", sa.String(length=16),
            nullable=False, server_default="pending",
        ),
        sa.Column(
            "claimed_by_id", sa.UUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "priority", sa.String(length=16), nullable=False, server_default="normal",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_handoff_workspace_id", "human_handoff_queue", ["workspace_id"])
    op.create_index("ix_handoff_session_id", "human_handoff_queue", ["session_id"])
    op.create_index(
        "ix_handoff_workspace_status_created",
        "human_handoff_queue",
        ["workspace_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_handoff_workspace_status_created", table_name="human_handoff_queue")
    op.drop_index("ix_handoff_session_id", table_name="human_handoff_queue")
    op.drop_index("ix_handoff_workspace_id", table_name="human_handoff_queue")
    op.drop_table("human_handoff_queue")
    op.drop_index("ix_chat_attachments_session_created", table_name="chat_attachments")
    op.drop_index("ix_chat_attachments_session_id", table_name="chat_attachments")
    op.drop_table("chat_attachments")
