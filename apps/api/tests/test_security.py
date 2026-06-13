"""Unit tests for ``core.security`` and ``core.crypto``."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from freezegun import freeze_time

from core import crypto, security


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_password_hash_round_trip() -> None:
    plain = "correct horse battery staple"
    hashed = security.get_password_hash(plain)
    assert hashed != plain
    assert security.verify_password(plain, hashed) is True
    assert security.verify_password("wrong-password", hashed) is False


def test_password_hash_is_bcrypt_and_not_idempotent() -> None:
    h1 = security.get_password_hash("same-pass")
    h2 = security.get_password_hash("same-pass")
    assert h1 != h2  # bcrypt embeds a random salt
    assert h1.startswith("$2b$")


def test_verify_password_handles_garbage_hash() -> None:
    assert security.verify_password("anything", "not-a-bcrypt-hash") is False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def test_access_token_round_trip() -> None:
    sub = str(uuid.uuid4())
    token = security.create_access_token(subject=sub, role="builder")
    payload = security.verify_token(token, expected_type="access")
    assert payload["sub"] == sub
    assert payload["role"] == "builder"
    assert payload["type"] == "access"
    assert "jti" in payload


def test_refresh_token_round_trip() -> None:
    sub = str(uuid.uuid4())
    token = security.create_refresh_token(subject=sub)
    payload = security.verify_token(token, expected_type="refresh")
    assert payload["sub"] == sub
    assert payload["type"] == "refresh"


def test_wrong_token_type_raises() -> None:
    token = security.create_access_token(subject="x", role="viewer")
    with pytest.raises(security.TokenError):
        security.verify_token(token, expected_type="refresh")


def test_expired_token_raises() -> None:
    with freeze_time("2026-01-01T00:00:00Z"):
        token = security.create_access_token(
            subject="x", role="viewer", expires_delta=timedelta(seconds=10)
        )
    with freeze_time("2026-01-01T00:01:00Z"):
        with pytest.raises(security.TokenError):
            security.verify_token(token, expected_type="access")


def test_tampered_token_raises() -> None:
    token = security.create_access_token(subject="x", role="viewer")
    tampered = token[:-2] + ("AB" if not token.endswith("AB") else "CD")
    with pytest.raises(security.TokenError):
        security.verify_token(tampered, expected_type="access")


def test_refresh_token_hash_is_deterministic_and_64_hex() -> None:
    h1 = security.hash_refresh_token("token-abc")
    h2 = security.hash_refresh_token("token-abc")
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


# ---------------------------------------------------------------------------
# Redis blacklist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blacklist_round_trip() -> None:
    jti = uuid.uuid4().hex
    assert await security.is_blacklisted(jti) is False
    await security.blacklist_token(jti, ttl_seconds=60)
    assert await security.is_blacklisted(jti) is True


@pytest.mark.asyncio
async def test_blacklist_zero_ttl_noop() -> None:
    jti = uuid.uuid4().hex
    await security.blacklist_token(jti, ttl_seconds=0)
    assert await security.is_blacklisted(jti) is False


# ---------------------------------------------------------------------------
# Fernet (MFA secret encryption)
# ---------------------------------------------------------------------------


def test_fernet_round_trip() -> None:
    plaintext = "JBSWY3DPEHPK3PXP"  # example base32 TOTP secret
    enc = crypto.encrypt_secret(plaintext)
    assert enc != plaintext
    assert crypto.decrypt_secret(enc) == plaintext


def test_fernet_rejects_tampered_token() -> None:
    enc = crypto.encrypt_secret("secret")
    tampered = enc[:-2] + ("AA" if not enc.endswith("AA") else "BB")
    with pytest.raises(crypto.InvalidToken):
        crypto.decrypt_secret(tampered)
