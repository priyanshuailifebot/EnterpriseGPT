# syntax=docker/dockerfile:1.6
# Build from monorepo root:  docker build -f apps/api/Dockerfile -t egpt-api --target production .
#
# Stages:
#   base      — python:3.11-slim, non-root user ``app``, curl (health checks)
#   builder   — Poetry installs main deps into an in-project .venv
#   development — hot-reload as root, source bind-friendly
#   production — non-root app user, 4 workers, healthcheck

# ---------- base ----------
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

# ---------- builder ----------
FROM base AS builder

# VIRTUALENVS_IN_PROJECT=true makes Poetry create .venv inside /app so we can
# COPY the site-packages out. VIRTUALENVS_CREATE=false broke in Poetry 2.x —
# it says "No dependencies to install" but installs nothing into system Python.
ENV POETRY_VIRTUALENVS_CREATE=true \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache \
    PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring

RUN pip install --no-cache-dir "poetry==2.4.1"

COPY dynamiq /dynamiq
COPY apps/api /app

# Path dep ``../../dynamiq`` resolves to ``/dynamiq`` when the project lives at ``/app``.
WORKDIR /app
RUN poetry install --only main --no-root --no-ansi \
    && rm -rf "$POETRY_CACHE_DIR"
WORKDIR /

# ---------- development ----------
FROM base AS development

ENV ENVIRONMENT=development \
    DEBUG=true

# Copy installed packages from the in-project venv into the system interpreter.
# The /app volume mount in docker-compose would shadow /app/.venv at runtime,
# so we bake the packages into the system site-packages instead.
COPY --from=builder /app/.venv/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY dynamiq /dynamiq
COPY apps/api /app

# Stable copy of the entrypoint outside /app so the dev volume mount can't
# shadow it (the script also lives at /app/docker-entrypoint.sh for editability).
COPY apps/api/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER root

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status==200 else 1)" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------- production ----------
FROM base AS production

ENV ENVIRONMENT=production \
    DEBUG=false

COPY --from=builder /app/.venv/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY dynamiq /dynamiq
COPY --chown=app:app apps/api /app

COPY apps/api/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status==200 else 1)" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
