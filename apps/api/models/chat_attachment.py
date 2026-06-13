"""Attachments uploaded to a chat session.

Each row owns an object-storage blob (MinIO) and a metadata record. The
runtime turns ``ChatAttachment`` rows into multimodal content blocks
inside the LLM message array (OpenAI vision shape ``{type: "image_url"}``
for images; non-image attachments are surfaced to the agent as a tool-
visible URL it can reference).

Attachments are NOT redacted — they're opaque blobs. PII redaction in
files is a separate problem (text-extraction + redact pipeline) deferred
to Phase 3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from models.chat_session import ChatMessage, ChatSession
    from models.user import User


class ChatAttachment(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "chat_attachments"
    __table_args__ = (
        Index("ix_chat_attachments_session_created", "session_id", "created_at"),
    )

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Bound to the message the user sent it with. Null when an
    # attachment was uploaded ahead of the next user turn (the runtime
    # binds it on the next ``handle_user_message`` call).
    message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    session: Mapped["ChatSession"] = relationship()
    message: Mapped["ChatMessage | None"] = relationship()
    uploaded_by: Mapped["User | None"] = relationship()


__all__ = ["ChatAttachment"]
