"""Shared pytest fixtures.

The test suite talks to **real** Postgres + Redis (running via
``docker compose up -d postgres redis``). A separate database
``enterprisegpt_test`` is created on first run by ``_bootstrap_db``.

We keep DB tables long-lived (Alembic-managed) and wipe their rows
between tests via the ``clean_db`` autouse fixture. Redis keys living
under the ``egpt:`` prefix are similarly flushed.
"""

from __future__ import annotations

import os
from pathlib import Path
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Environment — must be set BEFORE main / settings imports happen.
# ---------------------------------------------------------------------------
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://egpt:egpt_dev_password@localhost:5432/enterprisegpt_test",
)
os.environ.setdefault(
    "REDIS_URL", "redis://:egpt_dev_redis_password@localhost:6379/1"
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-please-do-not-use-in-prod")
_cache_dir = Path(__file__).resolve().parents[1] / ".composio_cache"
_cache_dir.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("COMPOSIO_CACHE_DIR", str(_cache_dir))
# LLM config for tests. We load the real Azure OpenAI creds from the repo-root
# ``.env`` so ``DynamiqService._resolve_llm`` can BUILD the Azure client the
# same way production does. This is safe: every test that would actually call
# the model mocks the network hop (``run_workflow_stream`` /
# ``hydrate_agent_stage`` / ``WorkflowInterpreter.interpret`` /
# ``_call_real_azure_for_agent``), so only the client *object* is constructed —
# no live tokens are spent. Falls back to a build-only placeholder when no
# ``.env`` is present (e.g. CI without secrets) so the suite still runs.
def _load_llm_creds_from_env_file() -> None:
    root_env = Path(__file__).resolve().parents[3] / ".env"
    wanted = {
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_DEFAULT_MODEL",
    }
    if root_env.exists():
        for raw in root_env.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in wanted:
                # setdefault so an explicit shell export still wins.
                os.environ.setdefault(key, value.strip())


_load_llm_creds_from_env_file()
# Build-only fallbacks when no .env is present (CI without secrets). Valid-
# looking but fake — every model call is mocked, so these are never used live.
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://test.openai.azure.com")
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-azure-key-build-only")
os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-api03-placeholder000000000000000000000000000000000000000000")

# Bootstrap the test DB exactly once per session.
from tests._bootstrap_db import ensure_test_database  # noqa: E402

ensure_test_database()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Async HTTP client bound directly to the FastAPI ASGI app."""
    from main import app  # imported lazily so env vars take effect first

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
def settings():  # type: ignore[no-untyped-def]
    """Settings instance for tests."""
    from core.config import get_settings

    return get_settings()


@pytest_asyncio.fixture
async def db_session():
    """Yield a fresh AsyncSession bound to the test database."""
    from core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    """Truncate all Phase 1 tables and flush Redis test DB before each test."""
    from sqlalchemy import text

    from core.database import get_session_factory
    from core.redis import get_redis

    # ---- Postgres ----
    factory = get_session_factory()
    async with factory() as session:
        # Order respects FK constraints: child rows first.
        await session.execute(text("TRUNCATE TABLE documents CASCADE"))
        await session.execute(text("TRUNCATE TABLE help_requests CASCADE"))
        await session.execute(text("TRUNCATE TABLE integrations CASCADE"))
        await session.execute(text("TRUNCATE TABLE workflow_execution_steps CASCADE"))
        await session.execute(text("TRUNCATE TABLE workflow_executions CASCADE"))
        await session.execute(text("TRUNCATE TABLE workflow_versions CASCADE"))
        await session.execute(text("TRUNCATE TABLE workflows CASCADE"))
        await session.execute(text("TRUNCATE TABLE audit_logs CASCADE"))
        await session.execute(text("TRUNCATE TABLE api_keys CASCADE"))
        await session.execute(text("TRUNCATE TABLE workspace_members CASCADE"))
        await session.execute(text("TRUNCATE TABLE sessions CASCADE"))
        await session.execute(text("TRUNCATE TABLE workspaces CASCADE"))
        await session.execute(text("TRUNCATE TABLE users CASCADE"))
        await session.commit()

    # ---- Redis ----
    redis = get_redis()
    for pattern in ("egpt:*", "clarification:*", "tools:*"):
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=500)
            if keys:
                await redis.delete(*keys)
            if cursor == 0:
                break

    yield
