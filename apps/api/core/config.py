"""Application configuration.

Loads settings from environment variables (and optionally a local `.env`
file) into a strongly-typed Pydantic model. Use ``get_settings()`` to fetch
a cached singleton anywhere in the codebase.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---------- App ----------
    APP_NAME: str = "EnterpriseGPT"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: Literal["development", "staging", "production", "test"] = "development"
    DEBUG: bool = False
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ---------- API ----------
    API_HOST: str = "0.0.0.0"  # noqa: S104 — intentional bind on container
    API_PORT: int = 8000
    API_WORKERS: int = 1
    # Base URL for OAuth redirects back to this API (no trailing slash).
    APP_PUBLIC_URL: str = "http://localhost:8000"
    SECRET_KEY: str = "change-me-to-a-long-random-string-in-production"
    JWT_EXPIRE_MINUTES: int = 480
    JWT_REFRESH_EXPIRE_DAYS: int = 30
    # Annotated[..., NoDecode] disables Pydantic-Settings' default JSON decoding
    # for this complex type so the env value (e.g. "http://localhost:3000" or a
    # comma-separated list) is delivered raw to ``_split_cors_origins`` below.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # ---------- Postgres ----------
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "enterprisegpt"
    POSTGRES_USER: str = "egpt"
    POSTGRES_PASSWORD: str = "egpt_dev_password"
    DATABASE_URL: str = (
        "postgresql+asyncpg://egpt:egpt_dev_password@postgres:5432/enterprisegpt"
    )

    # ---------- Redis ----------
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = "egpt_dev_redis_password"
    REDIS_URL: str = "redis://:egpt_dev_redis_password@redis:6379/0"

    # ---------- Qdrant ----------
    QDRANT_URL: str = "http://qdrant:6333"
    QDRANT_API_KEY: str = ""

    # ---------- MinIO ----------
    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_USER: str = "egpt_minio"
    MINIO_PASSWORD: str = "egpt_dev_minio_password"
    MINIO_BUCKET: str = "enterprisegpt"
    MINIO_USE_SSL: bool = False

    # ---------- LLM provider (Azure OpenAI) ----------
    # EnterpriseGPT routes all LLM traffic through Azure OpenAI. The deployment
    # name in Azure AI Studio (e.g. ``gpt-4o-mini``) is what callers reference;
    # the underlying model is informational.
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_API_KEY: str = ""
    AZURE_OPENAI_API_VERSION: str = "2024-08-01-preview"
    AZURE_OPENAI_DEPLOYMENT: str = "gpt-4o-mini"
    AZURE_OPENAI_DEFAULT_MODEL: str = "gpt-4o-mini"
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str = "text-embedding-3-small"
    AZURE_OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"

    # Optional — used by Dynamiq hydration when Azure is not configured
    ANTHROPIC_API_KEY: str = ""

    # Direct OpenAI — used when an agent's ``chat_model.provider == "openai"``.
    # Distinct from Azure on purpose: Azure stays the platform default for the
    # workflow interpreter and clarification agent; Tools-Agent chat agents
    # may opt into a direct-OpenAI route via the schema.
    OPENAI_API_KEY: str = ""

    # ---------- Workflow NL clarification (LangGraph checkpoint-backed) ----------
    CLARIFICATION_ENABLED: bool = True
    CLARIFICATION_MAX_ROUNDS: int = 3
    CLARIFICATION_CONFIDENCE_THRESHOLD: float = 0.75
    CLARIFICATION_SESSION_TTL_SECONDS: int = (
        1800  # Legacy name; retention is governed by LANGGRAPH_CHECKPOINT_DEFAULT_TTL_MINUTES
    )
    # When true, the clarifier must run an interpreter preview and a confirmation step before /interpret returns ready.
    CLARIFICATION_PREVIEW_BEFORE_READY: bool = False

    # Placeholder tool names shown to the NL interpreter until Phase 4 MCP.
    WORKFLOW_PREVIEW_TOOL_NAMES: str = "web_search_placeholder"

    # ---------- Composio (MCP) ----------
    COMPOSIO_API_KEY: str = ""

    # Composio's hosted MCP endpoint. We talk MCP wire protocol here instead
    # of the legacy ComposioToolSet Python SDK (which broke at composio>=1.0).
    # The dashboard exposes a single shared URL and a per-consumer API key
    # passed as the ``X-CONSUMER-API-KEY`` header.
    COMPOSIO_MCP_URL: str = "https://connect.composio.dev/mcp"
    COMPOSIO_MCP_API_KEY: str = ""
    # Transport: "sse" (legacy) or "streamable-http" (newer MCP spec). Composio
    # supports both; default to streamable-http since that's where they're
    # converging.
    COMPOSIO_MCP_TRANSPORT: str = "streamable-http"

    # ---------- Phase B native OAuth connectors ----------
    # Operator registers an app at each provider's developer console once, then
    # populates these. The redirect URI registered with each provider must be
    # ``${OAUTH_REDIRECT_BASE_URL}/oauth-callback`` (a Next.js page).
    OAUTH_REDIRECT_BASE_URL: str = "http://localhost:3000/integrations"
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    SLACK_OAUTH_CLIENT_ID: str = ""
    SLACK_OAUTH_CLIENT_SECRET: str = ""
    ATLASSIAN_OAUTH_CLIENT_ID: str = ""
    ATLASSIAN_OAUTH_CLIENT_SECRET: str = ""
    PIPEDREAM_OAUTH_CLIENT_ID: str = ""
    PIPEDREAM_OAUTH_CLIENT_SECRET: str = ""

    # ---------- Public URL for wait_for_webhook resume links ----------
    # Workflow authors paste resume URLs into emails/candidate pages; this is
    # the public origin that prefixes the path. Leave blank to emit relative
    # URLs (fine when the SPA proxies through the same origin).
    PUBLIC_BASE_URL: str = ""

    # ---------- Phase 3 LangGraph / escalations ----------
    HELP_ESCALATION_WEBHOOK_URL: str = ""
    LANGGRAPH_CHECKPOINTER_MODE: str = (
        "auto"  # auto | redis | memory — auto picks memory on enterprisegpt_test DB
    )
    LANGGRAPH_CHECKPOINT_DEFAULT_TTL_MINUTES: float = 1440.0  # 24h

    # ---------- Phase 5 RAG ----------
    RAG_MIN_SIMILARITY_SCORE: float = 0.6
    RAG_EMBEDDING_BATCH_SIZE: int = 100
    RAG_CHUNK_SIZE: int = 1000
    RAG_CHUNK_OVERLAP: int = 200

    # ---------- Observability ----------
    SENTRY_DSN: str = ""
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, v: object) -> list[str] | object:
        """Accept CORS_ORIGINS in any of these forms:

        * comma-separated string  ``"http://a, http://b"``  (preferred in .env)
        * JSON-encoded list       ``'["http://a", "http://b"]'``
        * already a Python list   (used by tests / imports)
        * empty / blank string    → ``[]``
        """
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                import json

                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed]
            return [origin.strip() for origin in stripped.split(",") if origin.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"

    @property
    def workflow_preview_tools(self) -> list[str]:
        """Tool id strings fed to the interpreter prompt (Phase 4 replaces with MCP)."""
        raw = self.WORKFLOW_PREVIEW_TOOL_NAMES.strip()
        if not raw:
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    The lru_cache makes this safe to call from anywhere (including FastAPI
    dependencies) without re-reading the environment on every request.
    """
    return Settings()
