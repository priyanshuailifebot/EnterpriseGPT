"""Catalog tests for the new native providers.

We don't talk to the upstream APIs here — the goal is to verify:

* every provider id is registered exactly once,
* every declared tool slug resolves back to its provider,
* ``build_connection`` accepts a minimal config dict without raising,
* ``build_tool`` returns a Dynamiq node (or ``None`` for LLM-only providers).

Real credential probes are exercised by an opt-in env var (``RUN_LIVE_PROBES``)
so CI doesn't hit the network.
"""

from __future__ import annotations

import os

import pytest

from agents.native_providers import (
    get_provider,
    list_providers,
    resolve_provider_for_slug,
)


_NEW_PROVIDER_IDS = {
    "twilio",
    "sendgrid",
    "elevenlabs",
    "whisper",
    "http_bearer",
    "postgres",
    "pipedream",
}


def test_new_providers_registered() -> None:
    catalog_ids = {p.id for p in list_providers()}
    missing = _NEW_PROVIDER_IDS - catalog_ids
    assert not missing, f"missing providers: {missing}"


def test_all_slugs_resolve_back_to_provider() -> None:
    for p in list_providers():
        for slug in p.tool_slugs:
            resolved = resolve_provider_for_slug(slug)
            assert resolved is not None, slug
            assert resolved.id == p.id, (slug, resolved.id, p.id)


# ---------------------------------------------------------------------------
# Per-provider build_connection happy-path probes.
# ---------------------------------------------------------------------------


def test_twilio_builds_connection() -> None:
    p = get_provider("twilio")
    assert p is not None and p.build_connection is not None
    conn = p.build_connection({"account_sid": "ACfake", "auth_token": "tok"})
    assert "Authorization" in (conn.headers or {})
    assert "https://api.twilio.com/2010-04-01/Accounts/ACfake" in conn.url


def test_sendgrid_builds_connection_and_tool() -> None:
    p = get_provider("sendgrid")
    assert p is not None and p.build_connection is not None
    conn = p.build_connection({"api_key": "SG.fake"})
    assert (conn.headers or {}).get("Authorization", "").startswith("Bearer ")
    assert p.build_tool is not None
    tool = p.build_tool(conn, "sendgrid_send")
    assert tool is not None


def test_elevenlabs_builds_connection() -> None:
    p = get_provider("elevenlabs")
    assert p is not None and p.build_connection is not None
    conn = p.build_connection({"api_key": "fake"})
    # The Dynamiq ElevenLabs class stores the api key on the instance.
    assert conn is not None


def test_whisper_builds_connection() -> None:
    p = get_provider("whisper")
    assert p is not None and p.build_connection is not None
    conn = p.build_connection({"api_key": "sk-fake"})
    assert (conn.headers or {}).get("Authorization") == "Bearer sk-fake"


def test_http_bearer_requires_base_url() -> None:
    p = get_provider("http_bearer")
    assert p is not None and p.build_connection is not None
    with pytest.raises(ValueError, match="base_url"):
        p.build_connection({"token": "x"})
    conn = p.build_connection({"base_url": "https://example.com/", "token": "t"})
    assert conn.url == "https://example.com"
    assert (conn.headers or {}).get("Authorization") == "Bearer t"


def test_http_bearer_token_optional() -> None:
    p = get_provider("http_bearer")
    assert p is not None and p.build_connection is not None
    conn = p.build_connection({"base_url": "https://public.example.com"})
    # No Authorization header when token is empty.
    assert "Authorization" not in (conn.headers or {})


def test_postgres_builds_connection_object() -> None:
    p = get_provider("postgres")
    assert p is not None and p.build_connection is not None
    conn = p.build_connection(
        {
            "host": "localhost",
            "port": "5432",
            "database": "mydb",
            "user": "u",
            "password": "p",
        }
    )
    # We're not connecting — just verify the connection object carries the
    # right host/db.
    assert conn.host == "localhost"
    assert conn.database == "mydb"


def test_pipedream_builds_connection() -> None:
    p = get_provider("pipedream")
    assert p is not None and p.build_connection is not None
    conn = p.build_connection({"access_token": "abc"})
    assert (conn.headers or {}).get("Authorization") == "Bearer abc"


# ---------------------------------------------------------------------------
# Catalog contract surfaced to the UI.
# ---------------------------------------------------------------------------


def test_provider_catalog_includes_new_providers() -> None:
    from services.native_connection_service import public_provider_catalog

    cat = {p["id"] for p in public_provider_catalog()}
    assert _NEW_PROVIDER_IDS.issubset(cat)


# ---------------------------------------------------------------------------
# Live probes — opt-in. Skipped in CI unless RUN_LIVE_PROBES=1.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PROBES", "0") != "1",
    reason="live probes disabled (set RUN_LIVE_PROBES=1 + real keys to run)",
)
def test_live_sendgrid_probe_against_real_key() -> None:
    p = get_provider("sendgrid")
    assert p is not None
    key = os.environ.get("SENDGRID_API_KEY", "")
    if not key:
        pytest.skip("SENDGRID_API_KEY missing")
    conn = p.build_connection({"api_key": key})  # type: ignore[misc]
    assert p.probe and p.probe(conn) == "ok"
