"""Smoke tests for the Phase 1 SQLAlchemy ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from models import (
    APIKey,
    AuditLog,
    Session,
    User,
    UserRole,
    Workspace,
    WorkspaceMember,
)


@pytest.mark.asyncio
async def test_user_round_trip(db_session) -> None:
    user = User(
        email="alice@example.com",
        hashed_password="$2b$12$fake",
        full_name="Alice",
        role=UserRole.BUILDER,
    )
    db_session.add(user)
    await db_session.commit()

    fetched = (
        await db_session.execute(select(User).where(User.email == "alice@example.com"))
    ).scalar_one()
    assert fetched.id == user.id
    assert fetched.role is UserRole.BUILDER
    assert fetched.is_active is True
    assert fetched.mfa_enabled is False
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


@pytest.mark.asyncio
async def test_workspace_membership_chain(db_session) -> None:
    owner = User(
        email="owner@example.com",
        hashed_password="$2b$12$fake",
        full_name="Owner",
        role=UserRole.SUPER_ADMIN,
    )
    db_session.add(owner)
    await db_session.flush()

    ws = Workspace(name="Acme", slug="acme", created_by=owner.id)
    db_session.add(ws)
    await db_session.flush()

    member = WorkspaceMember(
        workspace_id=ws.id, user_id=owner.id, role=UserRole.SUPER_ADMIN
    )
    db_session.add(member)
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == owner.id


@pytest.mark.asyncio
async def test_session_and_audit_records(db_session) -> None:
    user = User(
        email="ops@example.com",
        hashed_password="$2b$12$fake",
        full_name="Ops",
        role=UserRole.OPERATOR,
    )
    db_session.add(user)
    await db_session.flush()

    sess = Session(
        user_id=user.id,
        token_hash="deadbeef" * 8,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    log = AuditLog(
        user_id=user.id,
        action="POST /api/v1/auth/login",
        resource_type="auth",
        resource_id=str(user.id),
        ip_address="127.0.0.1",
        payload={"status": 200},
    )
    db_session.add_all([sess, log])
    await db_session.commit()

    sessions = (
        await db_session.execute(select(Session).where(Session.user_id == user.id))
    ).scalars().all()
    assert len(sessions) == 1
    assert sessions[0].token_hash.startswith("deadbeef")

    logs = (
        await db_session.execute(select(AuditLog).where(AuditLog.user_id == user.id))
    ).scalars().all()
    assert len(logs) == 1
    assert logs[0].payload == {"status": 200}


@pytest.mark.asyncio
async def test_api_key_scopes_array(db_session) -> None:
    user = User(
        email="keys@example.com",
        hashed_password="$2b$12$fake",
        full_name="Keys",
        role=UserRole.ADMIN,
    )
    db_session.add(user)
    await db_session.flush()

    ws = Workspace(name="K", slug=f"k-{uuid.uuid4().hex[:6]}", created_by=user.id)
    db_session.add(ws)
    await db_session.flush()

    key = APIKey(
        workspace_id=ws.id,
        name="prod-key",
        key_prefix="abcd1234",
        key_hash="$2b$12$fakehash",
        scopes=["workflow:run", "workflow:read"],
        created_by=user.id,
    )
    db_session.add(key)
    await db_session.commit()

    fetched = (
        await db_session.execute(select(APIKey).where(APIKey.id == key.id))
    ).scalar_one()
    assert fetched.scopes == ["workflow:run", "workflow:read"]
    assert fetched.key_prefix == "abcd1234"
