"""User account model + role enum.

Roles are platform-wide (used by the JWT ``role`` claim and RBAC checks).
Workspace-level roles are stored separately on ``WorkspaceMember``.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base
from models._base import TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:  # pragma: no cover
    from models.session import Session
    from models.workspace import Workspace
    from models.workspace_member import WorkspaceMember


class UserRole(str, enum.Enum):
    """Platform-wide role. Maps to a permission set in ``core.permissions``."""

    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    BUILDER = "builder"
    OPERATOR = "operator"
    VIEWER = "viewer"


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", native_enum=False, length=32),
        nullable=False,
        default=UserRole.VIEWER,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Fernet-encrypted base32 TOTP secret. Plaintext never touches disk.
    mfa_secret: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_login: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    owned_workspaces: Mapped[list["Workspace"]] = relationship(
        back_populates="creator",
        foreign_keys="Workspace.created_by",
    )
    memberships: Mapped[list["WorkspaceMember"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email!r} role={self.role.value}>"
