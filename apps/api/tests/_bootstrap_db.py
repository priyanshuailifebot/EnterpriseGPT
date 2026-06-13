"""Idempotent test-DB bootstrap.

Creates the ``enterprisegpt_test`` database (if missing) and runs Alembic
upgrade head against it. Imported once from ``conftest.py`` before any
session-scoped fixtures spin up.

Designed for the local-venv test mode: it talks to whatever Postgres is
exposed on ``localhost`` (typically the docker-compose container).
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT


def _parse_admin_dsn(database_url: str) -> tuple[str, str, str, int, str]:
    """Return (user, password, host, port, target_db) from a sqlalchemy URL."""
    parsed = urlparse(database_url)
    user = parsed.username or "egpt"
    password = parsed.password or ""
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    target_db = (parsed.path or "/enterprisegpt_test").lstrip("/")
    return user, password, host, port, target_db


def ensure_test_database() -> str:
    """Make sure the test DB exists and the schema is up to date.

    Returns the (sync, psycopg2-style) URL of the target DB.
    """
    test_url = os.environ["DATABASE_URL"]
    user, password, host, port, target_db = _parse_admin_dsn(test_url)

    admin_conn = psycopg2.connect(
        dbname="postgres",
        user=user,
        password=password,
        host=host,
        port=port,
    )
    admin_conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (target_db,)
            )
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{target_db}"')
    finally:
        admin_conn.close()

    # Run migrations against the now-existing DB.
    from alembic import command
    from alembic.config import Config

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    command.upgrade(cfg, "head")
    return test_url
