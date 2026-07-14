"""Authentication primitives: bcrypt + JWT + Redis token blacklist.

This module is the **only** place JWTs are minted or verified. Routers
must depend on :func:`get_current_user` / :func:`get_current_active_user`
rather than decoding tokens themselves.
"""

from __future__ import annotations

import hashlib
import secrets as _secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import get_db
from core.redis import get_redis as _get_redis_global
from models.user import User

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALGORITHM = "HS256"
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"
BLACKLIST_PREFIX = "egpt:bl:"
BCRYPT_ROUNDS = 12
# bcrypt silently truncates anything past 72 bytes; we pre-hash with SHA-256
# to make the algorithm safe for arbitrary password lengths without leaking
# information about the original byte count.
_BCRYPT_MAX_BYTES = 72


# ---------------------------------------------------------------------------
# Password hashing (direct bcrypt — passlib 1.7 + bcrypt 5.x are incompatible)
# ---------------------------------------------------------------------------


def _normalize_password(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        raw = hashlib.sha256(raw).hexdigest().encode("utf-8")
    return raw


def get_password_hash(password: str) -> str:
    """Hash a plaintext password with bcrypt (cost = ``BCRYPT_ROUNDS``)."""
    return bcrypt.hashpw(
        _normalize_password(password),
        bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
    ).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time bcrypt verification. Returns False on any malformed input."""
    try:
        return bcrypt.checkpw(_normalize_password(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT minting / verification
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(
    subject: str,
    role: str,
    workspace_id: str | None = None,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint an access token for ``subject`` (user id)."""
    settings = get_settings()
    expire_at = _now() + (
        expires_delta or timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "workspace_id": workspace_id,
        "iat": _now(),
        "exp": expire_at,
        "jti": uuid.uuid4().hex,
        "type": TOKEN_TYPE_ACCESS,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(
    subject: str, expires_delta: timedelta | None = None
) -> str:
    """Mint a long-lived refresh token. Stored as SHA-256 hash on the Session row."""
    settings = get_settings()
    expire_at = _now() + (
        expires_delta or timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS)
    )
    payload = {
        "sub": subject,
        "iat": _now(),
        "exp": expire_at,
        "jti": uuid.uuid4().hex,
        "type": TOKEN_TYPE_REFRESH,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


class TokenError(HTTPException):
    """401 raised when a JWT is missing, malformed, expired, or revoked."""

    def __init__(self, detail: str = "Could not validate credentials") -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


def verify_token(
    token: str, expected_type: Literal["access", "refresh"]
) -> dict[str, Any]:
    """Decode + validate a JWT. Raises :class:`TokenError` on any failure."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise TokenError(f"Invalid token: {exc}") from exc

    if payload.get("type") != expected_type:
        raise TokenError(f"Wrong token type (expected {expected_type})")
    if "sub" not in payload:
        raise TokenError("Token missing subject")
    return payload


# ---------------------------------------------------------------------------
# Refresh-token hashing (stored on Session.token_hash)
# ---------------------------------------------------------------------------


def hash_refresh_token(token: str) -> str:
    """SHA-256 of the refresh JWT — used as the lookup key on ``sessions``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_secure_token(num_bytes: int = 32) -> str:
    """URL-safe random token (used by API key generation)."""
    return _secrets.token_urlsafe(num_bytes)


# ---------------------------------------------------------------------------
# Signed trigger context (P7′)
#
# Email links (candidate slot form, recruiter approve/reject) must carry a
# tamper-proof payload — e.g. ``{"candidate_id": "...", "purpose": "slot"}`` —
# that a webhook/form trigger validates and injects into the execution input.
# Minted as a short-lived JWT so it reuses the app secret + jose, and can't be
# forged or replayed past its TTL.
# ---------------------------------------------------------------------------

_TRIGGER_CTX_TYPE = "trigger_ctx"


def sign_trigger_context(
    context: dict[str, Any], *, ttl_seconds: int = 7 * 24 * 3600
) -> str:
    """Sign an arbitrary (JSON-serializable) context dict into a URL-safe token."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "typ": _TRIGGER_CTX_TYPE,
        "ctx": context,
        "iat": now,
        "exp": now + timedelta(seconds=max(1, ttl_seconds)),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def verify_trigger_context(token: str) -> dict[str, Any] | None:
    """Return the signed context dict, or None if the token is invalid/expired."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if payload.get("typ") != _TRIGGER_CTX_TYPE:
        return None
    ctx = payload.get("ctx")
    return ctx if isinstance(ctx, dict) else None


# ---------------------------------------------------------------------------
# Token blacklist (Redis)
# ---------------------------------------------------------------------------


async def blacklist_token(jti: str, ttl_seconds: int) -> None:
    """Mark a JTI as revoked for ``ttl_seconds``."""
    if ttl_seconds <= 0:
        return
    redis: Redis = _get_redis_global()
    await redis.set(f"{BLACKLIST_PREFIX}{jti}", "1", ex=ttl_seconds)


async def is_blacklisted(jti: str) -> bool:
    redis: Redis = _get_redis_global()
    return bool(await redis.exists(f"{BLACKLIST_PREFIX}{jti}"))


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the authenticated user from a Bearer access token."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise TokenError("Missing bearer token")

    payload = verify_token(credentials.credentials, expected_type=TOKEN_TYPE_ACCESS)
    jti = payload.get("jti")
    if jti and await is_blacklisted(jti):
        raise TokenError("Token has been revoked")

    user_id = payload["sub"]
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise TokenError("Invalid subject") from exc

    user = (
        await db.execute(select(User).where(User.id == user_uuid))
    ).scalar_one_or_none()
    if user is None:
        raise TokenError("User not found")
    return user


async def get_current_active_user(
    user: User = Depends(get_current_user),
) -> User:
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User is inactive"
        )
    return user


__all__ = [
    "ALGORITHM",
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_REFRESH",
    "TokenError",
    "bearer_scheme",
    "blacklist_token",
    "create_access_token",
    "create_refresh_token",
    "generate_secure_token",
    "get_current_active_user",
    "get_current_user",
    "get_password_hash",
    "hash_refresh_token",
    "is_blacklisted",
    "verify_password",
    "verify_token",
]
