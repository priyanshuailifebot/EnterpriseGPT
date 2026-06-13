"""Tests for the RBAC matrix and the ``require_permission`` dependency."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core import permissions
from core.permissions import Permission, ROLE_PERMISSIONS, has_permission
from core.security import create_access_token, get_password_hash
from models.user import User, UserRole


# ---------------------------------------------------------------------------
# Static matrix
# ---------------------------------------------------------------------------


def test_super_admin_has_all_permissions() -> None:
    assert ROLE_PERMISSIONS[UserRole.SUPER_ADMIN] == frozenset(Permission)


def test_viewer_has_only_read_permissions() -> None:
    perms = ROLE_PERMISSIONS[UserRole.VIEWER]
    assert Permission.WORKFLOW_READ in perms
    assert Permission.WORKFLOW_CREATE not in perms
    assert Permission.WORKFLOW_RUN not in perms
    assert Permission.DOCUMENT_UPLOAD not in perms


def test_builder_can_create_but_not_manage() -> None:
    perms = ROLE_PERMISSIONS[UserRole.BUILDER]
    assert Permission.WORKFLOW_CREATE in perms
    assert Permission.DOCUMENT_UPLOAD in perms
    assert Permission.USER_MANAGE not in perms
    assert Permission.WORKSPACE_MANAGE not in perms


def test_operator_can_run_but_not_create() -> None:
    perms = ROLE_PERMISSIONS[UserRole.OPERATOR]
    assert Permission.WORKFLOW_RUN in perms
    assert Permission.WORKFLOW_CREATE not in perms


def test_admin_excludes_super_admin_only_actions_via_sets() -> None:
    # Admin should be a strict subset of SUPER_ADMIN at most equal.
    assert ROLE_PERMISSIONS[UserRole.ADMIN] <= ROLE_PERMISSIONS[UserRole.SUPER_ADMIN]


def test_has_permission_helper() -> None:
    assert has_permission(UserRole.BUILDER, Permission.WORKFLOW_CREATE) is True
    assert has_permission(UserRole.VIEWER, Permission.WORKFLOW_CREATE) is False


# ---------------------------------------------------------------------------
# Live FastAPI dependency check
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    app = FastAPI()

    @app.post(
        "/protected/create",
        dependencies=[permissions.require_permission(Permission.WORKFLOW_CREATE)],
    )
    async def protected_create() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get(
        "/protected/read",
        dependencies=[permissions.require_permission(Permission.WORKFLOW_READ)],
    )
    async def protected_read() -> dict[str, str]:
        return {"ok": "yes"}

    return app


async def _seed_user(db_session, role: UserRole) -> User:
    user = User(
        email=f"{role.value}@example.com",
        hashed_password=get_password_hash("password123"),
        full_name=f"Test {role.value}",
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.mark.asyncio
async def test_viewer_blocked_from_workflow_create(db_session) -> None:
    user = await _seed_user(db_session, UserRole.VIEWER)
    token = create_access_token(subject=str(user.id), role=user.role.value)

    app = _make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp_create = await c.post(
            "/protected/create", headers={"Authorization": f"Bearer {token}"}
        )
        resp_read = await c.get(
            "/protected/read", headers={"Authorization": f"Bearer {token}"}
        )

    assert resp_create.status_code == 403
    assert "Insufficient" in resp_create.json()["detail"]
    assert resp_read.status_code == 200


@pytest.mark.asyncio
async def test_builder_can_create(db_session) -> None:
    user = await _seed_user(db_session, UserRole.BUILDER)
    token = create_access_token(subject=str(user.id), role=user.role.value)

    app = _make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post(
            "/protected/create", headers={"Authorization": f"Bearer {token}"}
        )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_missing_token_returns_401(db_session) -> None:
    app = _make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post("/protected/create")
    assert resp.status_code == 401
