"""Phase 3 — help_requests for LangGraph dialog escalations."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c91f2b6a4e52"
down_revision: Union[str, None] = "a7e4c9b2d081"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "help_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_help_requests_workspace_id", "help_requests", ["workspace_id"])
    op.create_index("ix_help_requests_session_id", "help_requests", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_help_requests_session_id", table_name="help_requests")
    op.drop_index("ix_help_requests_workspace_id", table_name="help_requests")
    op.drop_table("help_requests")
