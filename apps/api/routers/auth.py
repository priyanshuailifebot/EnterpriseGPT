"""Phase 1 authentication router.

Endpoints (all under ``/api/v1/auth``):

    POST  /register
    POST  /login
    POST  /refresh
    POST  /logout
    GET   /me
    POST  /mfa/setup
    POST  /mfa/verify
    POST  /change-password
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import get_settings
from core.database import get_db
from core.security import (
    TOKEN_TYPE_REFRESH,
    blacklist_token,
    create_access_token,
    create_refresh_token,
    get_current_active_user,
    get_password_hash,
    hash_refresh_token,
    verify_password,
    verify_token,
)
from models.session import Session
from models.user import User, UserRole
from models.workspace import Workspace
from models.workspace_member import WorkspaceMember
from schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    MFASetupResponse,
    MFAVerifyRequest,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    RegisterResponse,
    UserResponse,
    WorkspaceMembershipResponse,
)
from services.auth_service import create_personal_workspace, utcnow
from services.mfa_service import (
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_totp_secret,
    provisioning_uri,
    qr_code_data_url,
    verify_totp,
)

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE_NAME = "egpt_refresh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _access_expires_seconds() -> int:
    return get_settings().JWT_EXPIRE_MINUTES * 60


def _refresh_expires_seconds() -> int:
    return get_settings().JWT_REFRESH_EXPIRE_DAYS * 24 * 60 * 60


def _set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=_refresh_expires_seconds(),
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/api/v1/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/api/v1/auth")


async def _user_with_workspaces(db: AsyncSession, user_id: uuid.UUID) -> User:
    """Eagerly load the user with workspace memberships + workspace metadata."""
    stmt = (
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.memberships).selectinload(WorkspaceMember.workspace))
    )
    user = (await db.execute(stmt)).scalar_one()
    return user


def _serialize_user(user: User) -> UserResponse:
    workspaces: list[WorkspaceMembershipResponse] = []
    if user.memberships is not None:
        for m in user.memberships:
            ws: Workspace | None = m.workspace
            if ws is None:
                continue
            workspaces.append(
                WorkspaceMembershipResponse(
                    workspace_id=ws.id,
                    workspace_name=ws.name,
                    workspace_slug=ws.slug,
                    role=m.role,
                )
            )
    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
        mfa_enabled=user.mfa_enabled,
        last_login=user.last_login,
        created_at=user.created_at,
        workspaces=workspaces,
    )


async def _resolve_role_for_register(role: UserRole | None) -> UserRole:
    """Honor caller-supplied role only in development."""
    settings = get_settings()
    if role is None:
        return UserRole.VIEWER
    if settings.is_development:
        return role
    return UserRole.VIEWER


async def _create_session_record(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    refresh_token: str,
    request: Request,
) -> Session:
    record = Session(
        user_id=user_id,
        token_hash=hash_refresh_token(refresh_token),
        ip_address=(request.client.host if request.client else None),
        user_agent=request.headers.get("user-agent"),
        expires_at=utcnow() + timedelta(seconds=_refresh_expires_seconds()),
    )
    db.add(record)
    await db.flush()
    return record


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    existing = (
        await db.execute(select(User.id).where(User.email == payload.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists",
        )

    role = await _resolve_role_for_register(payload.role)

    user = User(
        email=payload.email,
        hashed_password=get_password_hash(payload.password),
        full_name=payload.full_name,
        role=role,
    )
    db.add(user)
    await db.flush()

    workspace, _ = await create_personal_workspace(db, user)

    # First-class tokens
    access = create_access_token(
        subject=str(user.id), role=user.role.value, workspace_id=str(workspace.id)
    )
    refresh = create_refresh_token(subject=str(user.id))
    await _create_session_record(
        db, user_id=user.id, refresh_token=refresh, request=request
    )

    await db.commit()
    await db.refresh(user)
    user_loaded = await _user_with_workspaces(db, user.id)

    _set_refresh_cookie(response, refresh)
    return RegisterResponse(
        user=_serialize_user(user_loaded),
        access_token=access,
        expires_in=_access_expires_seconds(),
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    user = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User is inactive"
        )

    if user.mfa_enabled:
        if not payload.totp_code:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MFA code required",
            )
        if not user.mfa_secret or not verify_totp(
            decrypt_totp_secret(user.mfa_secret), payload.totp_code
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid MFA code",
            )

    primary_ws_id = (
        await db.execute(
            select(WorkspaceMember.workspace_id).where(
                WorkspaceMember.user_id == user.id
            )
        )
    ).scalar_one_or_none()

    access = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        workspace_id=str(primary_ws_id) if primary_ws_id else None,
    )
    refresh = create_refresh_token(subject=str(user.id))
    await _create_session_record(
        db, user_id=user.id, refresh_token=refresh, request=request
    )

    user.last_login = utcnow()
    await db.commit()

    user_loaded = await _user_with_workspaces(db, user.id)
    _set_refresh_cookie(response, refresh)

    return LoginResponse(
        user=_serialize_user(user_loaded),
        access_token=access,
        expires_in=_access_expires_seconds(),
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh(
    request: Request,
    response: Response,
    body: RefreshRequest = RefreshRequest(),
    cookie_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)] = None,
    db: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    raw_refresh = cookie_token or (body.refresh_token if body else None)
    if not raw_refresh:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing refresh token",
        )

    payload = verify_token(raw_refresh, expected_type=TOKEN_TYPE_REFRESH)
    user_id = uuid.UUID(payload["sub"])

    session_record = (
        await db.execute(
            select(Session).where(
                Session.token_hash == hash_refresh_token(raw_refresh),
                Session.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if session_record is None or session_record.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh session is invalid or has been revoked",
        )
    if session_record.expires_at <= utcnow():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh expired"
        )

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User unavailable"
        )

    # Rotate: revoke old session, mint new pair.
    session_record.revoked_at = utcnow()
    new_refresh = create_refresh_token(subject=str(user.id))
    await _create_session_record(
        db, user_id=user.id, refresh_token=new_refresh, request=request
    )

    primary_ws_id = (
        await db.execute(
            select(WorkspaceMember.workspace_id).where(
                WorkspaceMember.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    new_access = create_access_token(
        subject=str(user.id),
        role=user.role.value,
        workspace_id=str(primary_ws_id) if primary_ws_id else None,
    )
    await db.commit()

    _set_refresh_cookie(response, new_refresh)
    return RefreshResponse(
        access_token=new_access,
        expires_in=_access_expires_seconds(),
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    response: Response,
    cookie_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)] = None,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    # Blacklist the access token's JTI so it cannot be reused until expiry.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        from core.security import verify_token as _vt
        from core.security import TOKEN_TYPE_ACCESS as _ACCESS

        token = auth_header.split(" ", 1)[1].strip()
        try:
            payload = _vt(token, expected_type=_ACCESS)
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                ttl = max(1, int(exp - utcnow().timestamp()))
                await blacklist_token(jti, ttl_seconds=ttl)
        except HTTPException:
            pass  # already invalid → nothing to blacklist

    # Revoke the refresh session so the cookie cannot be reused either.
    if cookie_token:
        record = (
            await db.execute(
                select(Session).where(
                    Session.token_hash == hash_refresh_token(cookie_token),
                    Session.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if record and record.revoked_at is None:
            record.revoked_at = utcnow()
            await db.commit()

    _clear_refresh_cookie(response)
    return MessageResponse(detail="Logged out")


@router.get("/me", response_model=UserResponse)
async def me(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    user_loaded = await _user_with_workspaces(db, user.id)
    return _serialize_user(user_loaded)


# ---------------------------------------------------------------------------
# MFA
# ---------------------------------------------------------------------------


@router.post("/mfa/setup", response_model=MFASetupResponse)
async def mfa_setup(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> MFASetupResponse:
    """Generate a TOTP secret and store it encrypted on the user record.

    The user is *not* considered MFA-enabled until they successfully
    verify a code at ``/mfa/verify``.
    """
    secret = generate_totp_secret()
    user.mfa_secret = encrypt_totp_secret(secret)
    user.mfa_enabled = False
    await db.commit()

    uri = provisioning_uri(account_name=user.email, secret=secret)
    return MFASetupResponse(
        secret=secret,
        otpauth_url=uri,
        qr_code_data_url=qr_code_data_url(uri),
    )


@router.post("/mfa/verify", response_model=MessageResponse)
async def mfa_verify(
    payload: MFAVerifyRequest,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    if not user.mfa_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA setup has not been initiated",
        )
    if not verify_totp(decrypt_totp_secret(user.mfa_secret), payload.totp_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid MFA code"
        )

    user.mfa_enabled = True
    await db.commit()
    return MessageResponse(detail="MFA enabled")


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )

    user.hashed_password = get_password_hash(payload.new_password)

    # Revoke every active session — the user must log back in.
    sessions = (
        await db.execute(
            select(Session).where(
                Session.user_id == user.id, Session.revoked_at.is_(None)
            )
        )
    ).scalars().all()
    now = utcnow()
    for s in sessions:
        s.revoked_at = now

    await db.commit()
    return MessageResponse(detail="Password updated. Please log in again.")
