"""Auth domain helpers used by the ``routers/auth.py`` HTTP layer.

Centralizing these here keeps the router file focused on request/response
plumbing and lets us unit-test the business logic without spinning up
the ASGI app.
"""

from __future__ import annotations

import re
import secrets as _secrets
import unicodedata
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User, UserRole
from models.workspace import Workspace
from models.workspace_member import WorkspaceMember


def slugify(value: str, *, max_length: int = 48) -> str:
    """ASCII-only, lowercase, hyphenated slug. Used for auto workspace slugs."""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    if not normalized:
        normalized = "workspace"
    return normalized[:max_length].rstrip("-") or "workspace"


async def _slug_unique(db: AsyncSession, base_slug: str) -> str:
    """Return ``base_slug`` or a numeric-suffix variant that doesn't collide."""
    slug = base_slug
    suffix = 1
    while True:
        existing = (
            await db.execute(select(Workspace.id).where(Workspace.slug == slug))
        ).scalar_one_or_none()
        if existing is None:
            return slug
        suffix += 1
        candidate = f"{base_slug}-{suffix}"
        if len(candidate) > 64:
            candidate = f"{base_slug[:55]}-{_secrets.token_hex(2)}"
        slug = candidate


async def create_personal_workspace(
    db: AsyncSession, user: User
) -> tuple[Workspace, WorkspaceMember]:
    """Create a workspace owned by ``user`` and add them as a member.

    The workspace is the user's "home" tenant; subsequent users are
    invited into existing workspaces by an admin.
    """
    base = slugify(user.full_name or user.email.split("@")[0])
    slug = await _slug_unique(db, base)
    workspace = Workspace(
        name=f"{user.full_name}'s Workspace",
        slug=slug,
        created_by=user.id,
    )
    db.add(workspace)
    await db.flush()

    membership = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role=user.role,
    )
    db.add(membership)
    return workspace, membership


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "create_personal_workspace",
    "slugify",
    "utcnow",
]
