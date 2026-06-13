"""Integration tests for the Phase 1 middleware stack."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from models.audit_log import AuditLog


# ---------------------------------------------------------------------------
# Request ID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_id_present_on_response(client) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert "x-request-id" in {k.lower() for k in resp.headers.keys()}
    assert len(resp.headers["x-request-id"]) >= 16


@pytest.mark.asyncio
async def test_request_id_echoes_incoming_header(client) -> None:
    rid = "test-rid-1234567890abcdef"
    resp = await client.get("/health", headers={"X-Request-ID": rid})
    assert resp.headers["x-request-id"] == rid


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_security_headers(client) -> None:
    resp = await client.get("/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "strict-origin" in resp.headers["referrer-policy"]


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_allows_under_threshold(client) -> None:
    """A handful of requests on a non-skipped path stay under the limit."""
    # /api/v1 routes don't exist yet, but the middleware runs before
    # routing. Use a non-skipped path that returns 404 so we can count.
    for _ in range(5):
        resp = await client.get("/api/v1/__rate_test__")
        assert resp.status_code in {404, 429}
    assert resp.headers.get("x-ratelimit-limit") == "60"


@pytest.mark.asyncio
async def test_rate_limit_returns_429_after_burst(client) -> None:
    statuses: list[int] = []
    for _ in range(70):
        r = await client.get(
            "/api/v1/__rate_burst__",
            headers={"X-Forwarded-For": "203.0.113.7"},
        )
        statuses.append(r.status_code)
        if r.status_code == 429:
            assert r.headers.get("retry-after") is not None
            assert int(r.headers["retry-after"]) >= 1
            break
    assert 429 in statuses, f"expected at least one 429, got {statuses[-5:]}"


# ---------------------------------------------------------------------------
# Audit middleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_request_creates_audit_log(client, db_session) -> None:
    # Hit any non-skipped POST path. Even a 404 should be audited.
    await client.post("/api/v1/__audit_test__", json={"foo": 1})

    # The audit write is fire-and-forget; give it a moment.
    for _ in range(20):
        rows = (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "POST /api/v1/__audit_test__"
                )
            )
        ).scalars().all()
        if rows:
            break
        await asyncio.sleep(0.05)

    assert rows, "audit log row was not written"
    log = rows[0]
    assert log.payload.get("status_code") == 404
    assert log.resource_type == "__audit_test__"


@pytest.mark.asyncio
async def test_get_request_does_not_audit(client, db_session) -> None:
    await client.get("/api/v1/__audit_get__")
    await asyncio.sleep(0.1)

    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action.like("GET %"))
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_health_does_not_audit(client, db_session) -> None:
    await client.post("/health")  # also blocked by skipped paths
    await asyncio.sleep(0.05)
    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "POST /health")
        )
    ).scalars().all()
    assert rows == []
