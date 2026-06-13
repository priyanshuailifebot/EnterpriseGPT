"""Pydantic request / response schemas for the auth router."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from models.user import UserRole

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Self-registration payload.

    ``role`` is honored only when ``ENVIRONMENT=development`` (see
    ``routers.auth.register``); production registrations are forced to
    :class:`UserRole.VIEWER`.
    """

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    role: UserRole | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    totp_code: str | None = Field(default=None, min_length=6, max_length=6)


class RefreshRequest(BaseModel):
    """Optional body when the refresh cookie is unavailable (e.g. mobile)."""

    refresh_token: str | None = None


class MFAVerifyRequest(BaseModel):
    totp_code: str = Field(min_length=6, max_length=6)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshResponse(TokenResponse):
    pass


class WorkspaceMembershipResponse(BaseModel):
    workspace_id: UUID
    workspace_name: str
    workspace_slug: str
    role: UserRole

    model_config = ConfigDict(from_attributes=True)


class UserResponse(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str
    role: UserRole
    is_active: bool
    mfa_enabled: bool
    last_login: datetime | None
    created_at: datetime
    workspaces: list[WorkspaceMembershipResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class RegisterResponse(BaseModel):
    user: UserResponse
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class LoginResponse(RegisterResponse):
    pass


class MFASetupResponse(BaseModel):
    secret: str  # base32, shown once for manual setup
    otpauth_url: str  # otpauth://totp/...
    qr_code_data_url: str  # data:image/png;base64,...


class MessageResponse(BaseModel):
    detail: str


__all__ = [
    "ChangePasswordRequest",
    "LoginRequest",
    "LoginResponse",
    "MFASetupResponse",
    "MFAVerifyRequest",
    "MessageResponse",
    "RefreshRequest",
    "RefreshResponse",
    "RegisterRequest",
    "RegisterResponse",
    "TokenResponse",
    "UserResponse",
    "WorkspaceMembershipResponse",
]
