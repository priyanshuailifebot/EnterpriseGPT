"""RAG query log rows for analytics (confidence, citations, document affinity)."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c4d6e8f0a1"
down_revision: Union[str, None] = "e7a2b1c0d3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rag_query_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("unanswerable", sa.Boolean(), nullable=False),
        sa.Column("citation_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("top_document_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("question_excerpt", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["top_document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rag_query_logs_workspace_id_created_at",
        "rag_query_logs",
        ["workspace_id", "created_at"],
    )
    op.create_index(op.f("ix_rag_query_logs_top_document_id"), "rag_query_logs", ["top_document_id"], unique=False)
    op.create_index(op.f("ix_rag_query_logs_user_id"), "rag_query_logs", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_rag_query_logs_user_id"), table_name="rag_query_logs")
    op.drop_index(op.f("ix_rag_query_logs_top_document_id"), table_name="rag_query_logs")
    op.drop_index("ix_rag_query_logs_workspace_id_created_at", table_name="rag_query_logs")
    op.drop_table("rag_query_logs")
