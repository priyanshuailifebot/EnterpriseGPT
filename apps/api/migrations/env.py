"""Alembic migration environment.

Resolves the database URL at runtime from ``core.config.get_settings()``
and rewrites the ``+asyncpg`` driver to the synchronous ``+psycopg2``
driver — Alembic itself runs sync, but the app keeps its async engine.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import application models so their metadata is attached to ``Base.metadata``.
from core.config import get_settings  # noqa: E402
from core.database import Base  # noqa: E402
from models import *  # noqa: F401,F403,E402  (side-effect imports)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_database_url() -> str:
    """Return the DATABASE_URL with a sync driver suitable for Alembic."""
    raw = get_settings().DATABASE_URL
    return raw.replace("+asyncpg", "+psycopg2")


def run_migrations_offline() -> None:
    """Generate SQL without an active DB connection (``alembic upgrade --sql``)."""
    context.configure(
        url=_sync_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB."""
    config.set_main_option("sqlalchemy.url", _sync_database_url())
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
