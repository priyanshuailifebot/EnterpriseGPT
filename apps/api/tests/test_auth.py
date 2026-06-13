"""End-to-end tests for the auth router and Phase 1 approval gates."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pyotp
import pytest
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select

from core.security import create_access_token
from models.audit_log import AuditLog
from models.user import UserRole
from models.workspace_member import WorkspaceMember


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register(
    client: AsyncClient,
    *,
    email: str,
    password: str = "supersecret123",
    full_name: str = "Test User",
    role: UserRole | str | None = None,
) -> dict:
    body: dict = {"email": email, "password": password, "full_name": full_name}
    if role is not None:
        body["role"] = role.value if isinstance(role, UserRole) else role
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_creates_user_workspace_and_returns_token(
    client: AsyncClient, db_session
) -> None:
    body = await _register(
        client,
        email="alice@acme.io",
        full_name="Alice Smith",
        role=UserRole.BUILDER,
    )

    assert "access_token" in body
    assert body["user"]["email"] == "alice@acme.io"
    assert body["user"]["role"] == UserRole.BUILDER.value
    assert body["user"]["workspaces"], "personal workspace must be created"
    assert body["user"]["workspaces"][0]["role"] == UserRole.BUILDER.value

    membership_count = (
        await db_session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == body["user"]["id"]
            )
        )
    ).scalars().all()
    assert len(membership_count) == 1


@pytest.mark.asyncio
async def test_register_rejects_duplicate_email(client: AsyncClient) -> None:
    await _register(client, email="dup@example.com")
    second = await client.post(
        "/api/v1/auth/register",
        json={"email": "dup@example.com", "password": "12345678", "full_name": "x"},
    )
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_succeeds_with_valid_credentials(client: AsyncClient) -> None:
    await _register(client, email="login@example.com", password="hunter22hunter22")
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "login@example.com", "password": "hunter22hunter22"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    # Refresh cookie should be present.
    assert "egpt_refresh" in resp.cookies


@pytest.mark.asyncio
async def test_login_fails_with_wrong_password(client: AsyncClient) -> None:
    await _register(client, email="wrong@example.com", password="rightpassword1")
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "wrong@example.com", "password": "wrongpassword99"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_fails_for_unknown_user(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@example.com", "password": "whatever12345"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /me + token expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_returns_user_with_role(client: AsyncClient) -> None:
    body = await _register(
        client, email="me@example.com", role=UserRole.OPERATOR
    )
    token = body["access_token"]

    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "me@example.com"
    assert data["role"] == UserRole.OPERATOR.value


@pytest.mark.asyncio
async def test_me_returns_401_without_token(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_401_for_expired_token(
    client: AsyncClient, db_session
) -> None:
    body = await _register(client, email="expired@example.com")
    user_id = body["user"]["id"]

    # Mint a token that's already expired.
    with freeze_time("2026-01-01T00:00:00Z"):
        expired = create_access_token(
            subject=user_id,
            role=UserRole.VIEWER.value,
            expires_delta=timedelta(seconds=10),
        )
    with freeze_time("2026-01-01T00:01:00Z"):
        resp = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"}
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_401_for_garbage_token(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-jwt"}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Refresh + logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_rotates_token(client: AsyncClient) -> None:
    await _register(client, email="rot@example.com", password="passwordpassword")
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "rot@example.com", "password": "passwordpassword"},
    )
    cookies = login.cookies

    refreshed = await client.post("/api/v1/auth/refresh", cookies=cookies)
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]
    # Cookie was rotated.
    assert "egpt_refresh" in refreshed.cookies
    assert refreshed.cookies["egpt_refresh"] != cookies["egpt_refresh"]

    # Old refresh cookie should now be revoked.
    replay = await client.post("/api/v1/auth/refresh", cookies=cookies)
    assert replay.status_code == 401


@pytest.mark.asyncio
async def test_logout_blacklists_access_and_revokes_session(
    client: AsyncClient,
) -> None:
    body = await _register(client, email="bye@example.com", password="byebyebye123")
    token = body["access_token"]
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "bye@example.com", "password": "byebyebye123"},
    )
    cookies = login.cookies
    fresh_token = login.json()["access_token"]

    logout = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {fresh_token}"},
        cookies=cookies,
    )
    assert logout.status_code == 200

    # Same access token must now be rejected.
    me_after = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {fresh_token}"}
    )
    assert me_after.status_code == 401

    # First-issue token from /register should still be valid (different jti).
    me_orig = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me_orig.status_code == 200


# ---------------------------------------------------------------------------
# MFA
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mfa_setup_then_verify_then_login(client: AsyncClient) -> None:
    body = await _register(client, email="mfa@example.com", password="MFAUser1234")
    token = body["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    setup = await client.post("/api/v1/auth/mfa/setup", headers=auth)
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    assert secret
    assert setup.json()["qr_code_data_url"].startswith("data:image/png;base64,")

    code = pyotp.TOTP(secret).now()
    verify = await client.post(
        "/api/v1/auth/mfa/verify", json={"totp_code": code}, headers=auth
    )
    assert verify.status_code == 200

    # Login without TOTP must now fail.
    no_totp = await client.post(
        "/api/v1/auth/login",
        json={"email": "mfa@example.com", "password": "MFAUser1234"},
    )
    assert no_totp.status_code == 401

    with_totp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "mfa@example.com",
            "password": "MFAUser1234",
            "totp_code": pyotp.TOTP(secret).now(),
        },
    )
    assert with_totp.status_code == 200


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password(client: AsyncClient) -> None:
    body = await _register(
        client, email="cp@example.com", password="oldoldoldold123"
    )
    token = body["access_token"]

    resp = await client.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": "oldoldoldold123",
            "new_password": "newnewnewnew123",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    # Old password rejected.
    bad = await client.post(
        "/api/v1/auth/login",
        json={"email": "cp@example.com", "password": "oldoldoldold123"},
    )
    assert bad.status_code == 401

    # New password works.
    ok = await client.post(
        "/api/v1/auth/login",
        json={"email": "cp@example.com", "password": "newnewnewnew123"},
    )
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# RBAC integration with auth router (approval test gate #5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rbac_viewer_blocked_from_workflow_create(client: AsyncClient) -> None:
    """Mount an ephemeral protected route and assert RBAC denies viewers."""
    from core.permissions import Permission, require_permission
    from main import app

    @app.post(
        "/api/v1/__rbac_test__",
        dependencies=[require_permission(Permission.WORKFLOW_CREATE)],
    )
    async def _protected() -> dict[str, str]:
        return {"ok": "yes"}

    body = await _register(
        client, email="viewer@rbac.io", role=UserRole.VIEWER
    )
    token = body["access_token"]
    resp = await client.post(
        "/api/v1/__rbac_test__", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Audit log written on POST /login (approval test gate #10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_creates_audit_log(client: AsyncClient, db_session) -> None:
    await _register(
        client, email="audit@example.com", password="audit-pass-1234"
    )
    await client.post(
        "/api/v1/auth/login",
        json={"email": "audit@example.com", "password": "audit-pass-1234"},
    )

    for _ in range(20):
        rows = (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "POST /api/v1/auth/login")
            )
        ).scalars().all()
        if rows:
            break
        await asyncio.sleep(0.05)
    assert rows, "POST /auth/login should produce an audit log row"
    assert rows[0].payload["status_code"] == 200
