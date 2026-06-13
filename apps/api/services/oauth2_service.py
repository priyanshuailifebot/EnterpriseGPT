"""Generic OAuth 2 helper for native connectors (Phase B).

Three providers wired today: Google (Gmail scope), Slack, Atlassian (Jira).
Each is described by an :class:`OAuthProvider` dataclass declaring the
endpoints, scopes, and where to look up client credentials. The service
exposes:

* :func:`build_authorize_url` — returns the provider's consent screen URL
  + a CSRF state token that the callback validates.
* :func:`exchange_code` — completes the auth dance; returns a credential
  dict that the connections layer encrypts and stores.
* :func:`refresh_token_if_needed` — refresh-token rotation, called by the
  tool factory before instantiating any node.

Tokens live inside ``NativeConnection.config_encrypted``; no separate table.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
import structlog
from redis.asyncio import Redis

from core.config import Settings, get_settings

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Provider table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OAuthProvider:
    id: str
    display_name: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id_attr: str
    client_secret_attr: str
    # extra query params on the authorize URL (Google needs access_type=offline)
    extra_authorize_params: dict[str, str]


_PROVIDERS: dict[str, OAuthProvider] = {
    "gmail": OAuthProvider(
        id="gmail",
        display_name="Gmail",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=(
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
        ),
        client_id_attr="GOOGLE_OAUTH_CLIENT_ID",
        client_secret_attr="GOOGLE_OAUTH_CLIENT_SECRET",
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
    ),
    "slack": OAuthProvider(
        id="slack",
        display_name="Slack",
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        scopes=(
            "chat:write",
            "channels:read",
            "users:read",
        ),
        client_id_attr="SLACK_OAUTH_CLIENT_ID",
        client_secret_attr="SLACK_OAUTH_CLIENT_SECRET",
        extra_authorize_params={},
    ),
    "jira": OAuthProvider(
        id="jira",
        display_name="Jira",
        authorize_url="https://auth.atlassian.com/authorize",
        token_url="https://auth.atlassian.com/oauth/token",
        scopes=(
            "read:jira-work",
            "write:jira-work",
            "offline_access",
        ),
        client_id_attr="ATLASSIAN_OAUTH_CLIENT_ID",
        client_secret_attr="ATLASSIAN_OAUTH_CLIENT_SECRET",
        extra_authorize_params={"audience": "api.atlassian.com", "prompt": "consent"},
    ),
    "pipedream": OAuthProvider(
        id="pipedream",
        display_name="Pipedream Connect",
        # Pipedream uses a project-scoped OAuth client. The standard
        # consent URL accepts the same query params as any RFC 6749 dance.
        authorize_url="https://api.pipedream.com/v1/oauth/authorize",
        token_url="https://api.pipedream.com/v1/oauth/token",
        scopes=(
            # Pipedream documents scopes per-action; "connect" + "actions" covers
            # the common case of listing apps and invoking actions on behalf of
            # the connected user.
            "connect",
            "actions",
        ),
        client_id_attr="PIPEDREAM_OAUTH_CLIENT_ID",
        client_secret_attr="PIPEDREAM_OAUTH_CLIENT_SECRET",
        extra_authorize_params={"prompt": "consent"},
    ),
}


def get_oauth_provider(provider_id: str) -> OAuthProvider | None:
    return _PROVIDERS.get(provider_id.strip().lower())


def list_oauth_providers() -> tuple[OAuthProvider, ...]:
    return tuple(_PROVIDERS.values())


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OAuthError(RuntimeError):
    """User-correctable problem (bad config, bad state, provider error)."""


class OAuthNotConfigured(OAuthError):
    """Operator hasn't set CLIENT_ID/SECRET for this provider yet."""


# ---------------------------------------------------------------------------
# State (CSRF + workspace binding) — stashed in Redis for 10 minutes
# ---------------------------------------------------------------------------


_STATE_TTL_SECONDS = 600


def _state_key(token: str) -> str:
    return f"oauth_state::{token}"


async def stash_state(
    redis: Redis,
    *,
    workspace_id: UUID,
    user_id: UUID,
    provider: str,
    connection_name: str,
) -> str:
    token = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "workspace_id": str(workspace_id),
            "user_id": str(user_id),
            "provider": provider,
            "connection_name": connection_name,
            "ts": int(time.time()),
        }
    )
    await redis.set(_state_key(token), payload, ex=_STATE_TTL_SECONDS)
    return token


async def consume_state(redis: Redis, token: str) -> dict[str, Any]:
    key = _state_key(token)
    raw = await redis.get(key)
    if not raw:
        raise OAuthError("invalid or expired state token")
    await redis.delete(key)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------


def _redirect_uri(settings: Settings) -> str:
    base = settings.OAUTH_REDIRECT_BASE_URL.rstrip("/")
    # Same callback page handles every provider — we discriminate via ``state``.
    return f"{base}/oauth-callback"


def _client_creds(settings: Settings, provider: OAuthProvider) -> tuple[str, str]:
    client_id = getattr(settings, provider.client_id_attr, "") or ""
    client_secret = getattr(settings, provider.client_secret_attr, "") or ""
    if not client_id or not client_secret:
        raise OAuthNotConfigured(
            f"{provider.display_name} OAuth is not configured — set "
            f"{provider.client_id_attr} and {provider.client_secret_attr} in the API env."
        )
    return client_id, client_secret


def build_authorize_url(provider: OAuthProvider, state_token: str) -> str:
    settings = get_settings()
    client_id, _ = _client_creds(settings, provider)
    qs = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(settings),
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": state_token,
        **provider.extra_authorize_params,
    }
    return f"{provider.authorize_url}?{urlencode(qs)}"


async def exchange_code(provider: OAuthProvider, code: str) -> dict[str, Any]:
    settings = get_settings()
    client_id, client_secret = _client_creds(settings, provider)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(settings),
        "client_id": client_id,
        "client_secret": client_secret,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            provider.token_url,
            data=data,
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 400:
            raise OAuthError(f"token exchange failed: {resp.status_code} {resp.text[:300]}")
        body = resp.json()

    # Slack wraps its real token under access_token but also returns ok:false on failure.
    if provider.id == "slack" and not body.get("ok", True):
        raise OAuthError(f"slack rejected the exchange: {body.get('error')}")

    return _normalize_token_response(provider, body)


def _normalize_token_response(provider: OAuthProvider, body: dict[str, Any]) -> dict[str, Any]:
    if provider.id == "slack":
        # Slack returns access_token at top level + authed_user.access_token. We
        # store the bot token (top level) — the agent posts as the bot.
        return {
            "access_token": body.get("access_token"),
            "refresh_token": body.get("refresh_token"),
            "expires_at": int(time.time()) + int(body.get("expires_in", 0))
            if body.get("expires_in")
            else None,
            "scope": body.get("scope"),
            "team_id": (body.get("team") or {}).get("id"),
            "bot_user_id": body.get("bot_user_id"),
            "provider": "slack",
        }
    if provider.id == "jira":
        return {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token"),
            "expires_at": int(time.time()) + int(body.get("expires_in", 3600)),
            "scope": body.get("scope"),
            "cloud_id": None,  # populated lazily on first tool call (accessible-resources)
            "provider": "jira",
        }
    # Google / Gmail default
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "expires_at": int(time.time()) + int(body.get("expires_in", 3600)),
        "scope": body.get("scope"),
        "token_type": body.get("token_type", "Bearer"),
        "provider": provider.id,
    }


async def refresh_token_if_needed(
    provider: OAuthProvider, creds: dict[str, Any], *, leeway_seconds: int = 60
) -> dict[str, Any] | None:
    """Refresh access token if it expires within ``leeway_seconds``.

    Returns the *new* full credential dict if a refresh happened, else ``None``.
    """
    expires_at = creds.get("expires_at")
    if not expires_at or not creds.get("refresh_token"):
        return None
    if int(time.time()) + leeway_seconds < int(expires_at):
        return None  # still valid

    settings = get_settings()
    client_id, client_secret = _client_creds(settings, provider)
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            provider.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": creds["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 400:
            log.warning(
                "oauth.refresh_failed",
                provider=provider.id,
                status=resp.status_code,
                body=resp.text[:200],
            )
            raise OAuthError(f"refresh failed: {resp.status_code} {resp.text[:200]}")
        body = resp.json()

    refreshed = _normalize_token_response(provider, body)
    # Refresh tokens may rotate; keep the old one if the provider didn't return a new one.
    if not refreshed.get("refresh_token"):
        refreshed["refresh_token"] = creds.get("refresh_token")
    return refreshed


__all__ = [
    "OAuthProvider",
    "OAuthError",
    "OAuthNotConfigured",
    "get_oauth_provider",
    "list_oauth_providers",
    "build_authorize_url",
    "exchange_code",
    "stash_state",
    "consume_state",
    "refresh_token_if_needed",
]
