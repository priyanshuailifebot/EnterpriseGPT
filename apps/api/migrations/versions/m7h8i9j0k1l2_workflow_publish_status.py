"""Workflow publish lifecycle — status + published stamps."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "m7h8i9j0k1l2"
down_revision: Union[str, None] = "l6g7h8i9j0k1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
        ),
    )
    op.add_column(
        "workflows",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workflows",
        sa.Column("published_version_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_workflows_published_version_id",
        "workflows",
        "workflow_versions",
        ["published_version_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_workflows_published_version_id", "workflows", type_="foreignkey"
    )
    op.drop_column("workflows", "published_version_id")
    op.drop_column("workflows", "published_at")
    op.drop_column("workflows", "status")
