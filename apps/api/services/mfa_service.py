"""TOTP setup + verification + QR code data-URL generation."""

from __future__ import annotations

import base64
import io

import pyotp
import qrcode

from core.crypto import decrypt_secret, encrypt_secret


def generate_totp_secret() -> str:
    """Return a fresh base32 TOTP secret."""
    return pyotp.random_base32()


def encrypt_totp_secret(plain_secret: str) -> str:
    return encrypt_secret(plain_secret)


def decrypt_totp_secret(stored: str) -> str:
    return decrypt_secret(stored)


def provisioning_uri(
    *, account_name: str, secret: str, issuer: str = "EnterpriseGPT"
) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=issuer)


def qr_code_data_url(otpauth_url: str) -> str:
    """Render an otpauth:// URL as a base64 PNG ``data:`` URL."""
    img = qrcode.make(otpauth_url)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def verify_totp(secret: str, code: str, *, valid_window: int = 1) -> bool:
    """Return ``True`` if ``code`` matches the current TOTP for ``secret``.

    ``valid_window=1`` accepts the previous and next 30s slots in addition
    to the current one — generous enough for clock drift, tight enough
    to prevent replay over more than ~90s.
    """
    if not code or not secret:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=valid_window)


__all__ = [
    "decrypt_totp_secret",
    "encrypt_totp_secret",
    "generate_totp_secret",
    "provisioning_uri",
    "qr_code_data_url",
    "verify_totp",
]
