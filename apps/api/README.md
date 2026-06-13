# EnterpriseGPT API

FastAPI backend for the EnterpriseGPT platform.

## Local development

```bash
# Install dependencies
poetry install

# Activate virtual environment
poetry shell

# Run the dev server
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Or via the docker-compose stack from the repo root
docker compose up -d api
```

## Health check

```bash
curl http://localhost:8000/health
# {"status": "ok", "version": "0.1.0", "timestamp": "..."}
```

## Project layout

```
apps/api/
├── core/             # config, database, redis, logging, security primitives
├── routers/          # API route handlers (one module per resource)
├── services/         # Business logic layer
├── agents/           # Dynamiq + LangGraph integrations
├── rag/              # Document ingestion & retrieval
├── mcp/              # MCP tool registry & connectors
├── models/           # SQLAlchemy ORM models
├── schemas/          # Pydantic request/response schemas
├── migrations/       # Alembic DB migrations (created in Phase 1)
├── tests/            # Pytest test suite
├── main.py           # FastAPI app + lifespan + middleware
├── pyproject.toml    # Poetry project metadata + tooling config
└── Dockerfile        # Multi-stage build (base/builder/development/production)
```

## Tooling

```bash
poetry run ruff check .       # lint
poetry run ruff format .      # format
poetry run mypy .             # type-check
poetry run pytest             # tests
poetry run pytest --cov       # tests with coverage
```
