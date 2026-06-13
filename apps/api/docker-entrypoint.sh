#!/bin/sh
# Container entrypoint: run Alembic migrations, then exec the CMD.
#
# docker-compose already gates ``api`` on ``postgres: condition: service_healthy``,
# so Postgres is reachable by the time we get here. We still keep migrations
# fail-fast — if they error, the container exits non-zero rather than serving
# a half-migrated schema.

set -e

echo "[entrypoint] running alembic upgrade head..."
python -m alembic upgrade head
echo "[entrypoint] migrations complete; starting: $*"

exec "$@"
