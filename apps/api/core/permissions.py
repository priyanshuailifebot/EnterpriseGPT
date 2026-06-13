"""Role-based access control.

Source of truth for what each :class:`UserRole` may do. Routers should
attach :func:`require_permission` as a dependency rather than reading
``current_user.role`` directly so the policy stays centralized.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable

from fastapi import Depends, HTTPException, status

from core.security import get_current_active_user
from models.user import User, UserRole


class Permission(str, enum.Enum):
    """Capabilities the platform may grant. Strings double as audit log values."""

    WORKFLOW_CREATE = "workflow:create"
    WORKFLOW_RUN = "workflow:run"
    WORKFLOW_READ = "workflow:read"
    WORKFLOW_DELETE = "workflow:delete"
    DOCUMENT_UPLOAD = "document:upload"
    DOCUMENT_READ = "document:read"
    USER_MANAGE = "user:manage"
    WORKSPACE_MANAGE = "workspace:manage"
    ANALYTICS_READ = "analytics:read"
    MCP_MANAGE = "mcp:manage"


_ALL: frozenset[Permission] = frozenset(Permission)

ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.SUPER_ADMIN: _ALL,
    UserRole.ADMIN: frozenset(
        {
            Permission.WORKFLOW_CREATE,
            Permission.WORKFLOW_RUN,
            Permission.WORKFLOW_READ,
            Permission.WORKFLOW_DELETE,
            Permission.DOCUMENT_UPLOAD,
            Permission.DOCUMENT_READ,
            Permission.USER_MANAGE,
            Permission.WORKSPACE_MANAGE,
            Permission.ANALYTICS_READ,
            Permission.MCP_MANAGE,
        }
    ),
    UserRole.BUILDER: frozenset(
        {
            Permission.WORKFLOW_CREATE,
            Permission.WORKFLOW_RUN,
            Permission.WORKFLOW_READ,
            Permission.DOCUMENT_UPLOAD,
            Permission.DOCUMENT_READ,
        }
    ),
    UserRole.OPERATOR: frozenset(
        {
            Permission.WORKFLOW_RUN,
            Permission.WORKFLOW_READ,
            Permission.DOCUMENT_READ,
        }
    ),
    UserRole.VIEWER: frozenset(
        {
            Permission.WORKFLOW_READ,
            Permission.DOCUMENT_READ,
        }
    ),
}


def has_permission(role: UserRole, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def has_any_permission(role: UserRole, permissions: Iterable[Permission]) -> bool:
    perms = ROLE_PERMISSIONS.get(role, frozenset())
    return any(p in perms for p in permissions)


def require_permission(permission: Permission):
    """FastAPI dependency factory enforcing ``permission`` on the caller.

    Usage::

        @router.post("/workflows", dependencies=[require_permission(Permission.WORKFLOW_CREATE)])
        async def create_workflow(...): ...
    """

    async def _dependency(
        user: User = Depends(get_current_active_user),
    ) -> User:
        if not has_permission(user.role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return Depends(_dependency)


__all__ = [
    "Permission",
    "ROLE_PERMISSIONS",
    "has_any_permission",
    "has_permission",
    "require_permission",
]
