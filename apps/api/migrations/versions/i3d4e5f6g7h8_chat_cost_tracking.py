"""Phase 2c — cumulative cost columns on chat_sessions + per-turn cost on chat_messages."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i3d4e5f6g7h8"
down_revision: Union[str, None] = "h2c3d4e5f6g7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Cumulative on session.
    op.add_column(
        "chat_sessions",
        sa.Column(
            "total_prompt_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "total_completion_tokens",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "total_cost_microcents",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "total_messages",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # Per-turn cost on message.
    op.add_column(
        "chat_messages",
        sa.Column("cost_microcents", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("model_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "model_id")
    op.drop_column("chat_messages", "cost_microcents")
    op.drop_column("chat_sessions", "total_messages")
    op.drop_column("chat_sessions", "total_cost_microcents")
    op.drop_column("chat_sessions", "total_completion_tokens")
    op.drop_column("chat_sessions", "total_prompt_tokens")
