"""v2 — workflow_data key/value store for the ``data_store`` node-kind.

Schema-level mirror of n8n's "Data Tables". One row per (workspace, table,
key); ``data`` is JSONB so any workflow can write any shape. The data-table
viewer route reads from this table; no per-workflow page is required.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "g1b2c3d4e5f6"
down_revision: Union[str, None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_data",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("table_name", sa.String(length=128), nullable=False),
        sa.Column("row_key", sa.String(length=256), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "last_workflow_id",
            sa.UUID(),
            sa.ForeignKey("workflows.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "last_execution_id",
            sa.UUID(),
            sa.ForeignKey("workflow_executions.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
            "workspace_id", "table_name", "row_key",
            name="uq_workflow_data_ws_tbl_key",
        ),
    )
    op.create_index(
        "ix_workflow_data_workspace_id", "workflow_data", ["workspace_id"]
    )
    op.create_index(
        "ix_workflow_data_ws_tbl",
        "workflow_data",
        ["workspace_id", "table_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_data_ws_tbl", table_name="workflow_data")
    op.drop_index("ix_workflow_data_workspace_id", table_name="workflow_data")
    op.drop_table("workflow_data")
