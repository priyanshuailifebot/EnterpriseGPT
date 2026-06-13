"""Per-node workflow execution step records + demo discriminator."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "l6g7h8i9j0k1"
down_revision: Union[str, None] = "k5f6g7h8i9j0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflow_executions",
        sa.Column(
            "demo", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.create_table(
        "workflow_execution_steps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("execution_id", sa.UUID(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=255), nullable=False),
        sa.Column("node_name", sa.String(length=255), nullable=True),
        sa.Column("node_kind", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="completed",
            nullable=False,
        ),
        sa.Column(
            "dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "demo", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "input_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "output_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["workflow_executions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_workflow_execution_steps_execution_id"),
        "workflow_execution_steps",
        ["execution_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_workflow_execution_steps_execution_id"),
        table_name="workflow_execution_steps",
    )
    op.drop_table("workflow_execution_steps")
    op.drop_column("workflow_executions", "demo")
