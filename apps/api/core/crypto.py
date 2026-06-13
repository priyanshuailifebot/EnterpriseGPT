"""Symmetric encryption helpers used for MFA secrets at rest.

Derives a 32-byte Fernet key from ``SECRET_KEY`` via HKDF-SHA256 with a
fixed application salt. Rotating ``SECRET_KEY`` will invalidate any
previously-stored MFA secrets — that is the intended trade-off for now;
full key rotation is deferred to Phase 7 (KMS / Vault).
"""

from __future__ import annotations

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from core.config import get_settings

_HKDF_SALT = b"enterprisegpt::mfa::v1"
_HKDF_INFO = b"fernet-key"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    settings = get_settings()
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    ).derive(settings.SECRET_KEY.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a string secret. Returns a URL-safe base64 token."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """Decrypt a token produced by :func:`encrypt_secret`.

    Raises :class:`cryptography.fernet.InvalidToken` if the ciphertext is
    tampered with, or if ``SECRET_KEY`` has changed.
    """
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")


__all__ = ["encrypt_secret", "decrypt_secret", "InvalidToken"]
