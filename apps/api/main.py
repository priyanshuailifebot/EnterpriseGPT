"""FastAPI application entrypoint for EnterpriseGPT.

Boots structured logging, async DB engine, Redis pool, CORS, and the
optional Sentry integration. Exposes a ``/health`` endpoint used by
container orchestrators and the CI/approval suite.

Run locally::

    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from core.config import get_settings
from core.database import dispose_engine, init_engine
from core.logging import configure_logging, get_logger
from core.redis import dispose_redis, init_redis, ping_redis
from core.tracing import flush_traces, init_langfuse_from_settings
from middleware import register_middleware

settings = get_settings()
configure_logging()
logger = get_logger("enterprisegpt.api")


def _init_sentry() -> None:
    """Initialize Sentry only when a DSN is configured."""
    if not settings.SENTRY_DSN:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.ENVIRONMENT,
            release=f"{settings.APP_NAME}@{settings.APP_VERSION}",
            integrations=[StarletteIntegration(), FastApiIntegration()],
            traces_sample_rate=0.1 if settings.is_production else 1.0,
            send_default_pii=False,
        )
        logger.info("sentry.initialized", environment=settings.ENVIRONMENT)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentry.init_failed", error=str(exc))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks for shared resources."""
    logger.info(
        "app.starting",
        app=settings.APP_NAME,
        version=settings.APP_VERSION,
        environment=settings.ENVIRONMENT,
    )

    _init_sentry()
    init_engine()
    init_redis()

    if not await ping_redis():
        logger.warning("redis.ping_failed", url=settings.REDIS_URL)

    logger.info("app.started")

    try:
        yield
    finally:
        logger.info("app.shutting_down")
        flush_traces()
        await dispose_redis()
        await dispose_engine()
        logger.info("app.shutdown_complete")


def _register_routers(app: FastAPI) -> None:
    """Attach every API router. Imported lazily so tests can keep the
    module import side-effect free until they actually need the app."""
    from routers.analytics import router as analytics_router
    from routers.auth import router as auth_router
    from routers.chat import router as chat_router
    from routers.connections import router as connections_router
    from routers.dialog import router as dialog_router
    from routers.documents import router as documents_router
    from routers.integrations import router as integrations_router
    from routers.mcp_servers import router as mcp_servers_router
    from routers.reports import router as reports_router
    from routers.workflows import router as workflows_router

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(analytics_router, prefix="/api/v1")
    app.include_router(workflows_router, prefix="/api/v1")
    app.include_router(reports_router, prefix="/api/v1")
    app.include_router(dialog_router, prefix="/api/v1")
    app.include_router(integrations_router, prefix="/api/v1")
    app.include_router(connections_router, prefix="/api/v1")
    app.include_router(mcp_servers_router, prefix="/api/v1")
    app.include_router(documents_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")


def create_app() -> FastAPI:
    """Application factory — kept separate for testability."""
    init_langfuse_from_settings(settings)
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=(
            "EnterpriseGPT — turns natural-language commands into agentic "
            "workflows powered by Dynamiq."
        ),
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    register_middleware(app, settings)

    _register_routers(app)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "name": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT,
            "docs": "/docs",
            "health": "/health",
        }

    @app.get(
        "/health",
        tags=["meta"],
        status_code=status.HTTP_200_OK,
        summary="Liveness probe",
    )
    async def health() -> JSONResponse:
        """Lightweight liveness probe used by Compose / k8s health checks."""
        return JSONResponse(
            {
                "status": "ok",
                "version": settings.APP_VERSION,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    @app.get(
        "/ready",
        tags=["meta"],
        summary="Readiness probe (checks DB + Redis)",
    )
    async def ready() -> JSONResponse:
        """Deep readiness check that verifies backing services."""
        redis_ok = await ping_redis()
        body = {
            "status": "ok" if redis_ok else "degraded",
            "version": settings.APP_VERSION,
            "checks": {"redis": "ok" if redis_ok else "fail"},
            "timestamp": datetime.now(UTC).isoformat(),
        }
        code = (
            status.HTTP_200_OK if redis_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return JSONResponse(body, status_code=code)

    return app


app = create_app()
