# EnterpriseGPT

> Production-grade enterprise AI platform that turns natural-language commands into agentic workflows. Powered by **Dynamiq** as the orchestration backbone.

This monorepo contains the full stack for EnterpriseGPT:

- **`apps/api`** — FastAPI backend (Python 3.11, SQLAlchemy 2.0 async, Redis, Qdrant)
- **`apps/web`** — Next.js 14 frontend (App Router, TypeScript, Tailwind CSS)
- **`packages/shared-types`** — Shared TypeScript types used across the frontend
- **`packages/ui-kit`** — Shared React component library
- **`dynamiq/`** — Vendored Dynamiq orchestration framework (cloned upstream)
- **`infra/`** — Dockerfiles, Kubernetes manifests, Terraform IaC
- **`scripts/`** — Dev and deploy automation

## Phase 0 status — scaffold

This commit delivers the full **Phase 0 scaffold** described in the EnterpriseGPT roadmap:

- pnpm workspaces wired up (`apps/*`, `packages/*`)
- Docker Compose with `postgres`, `redis`, `qdrant`, `minio`, `api`, `web`
- FastAPI app with config, async DB, Redis pool, structlog logging, CORS, health endpoint
- Next.js 14 (App Router, TS, Tailwind) with React Query provider, theme provider, axios API client
- GitHub Actions CI (lint + type-check + tests for both apps) and Docker build pipeline

## Prerequisites

- Node.js **20+** (see `.nvmrc`)
- pnpm **9+** — `npm i -g pnpm`
- Python **3.11**
- Poetry — `pipx install poetry`
- Docker + Docker Compose

## Quick start

```bash
# 1. Install root + frontend dependencies
pnpm install

# 2. Copy env file and edit secrets as needed
cp .env.example .env

# 3. Bring up the core local stack (fast path)
docker compose up -d

# 4. Optional: include Langfuse observability stack
docker compose --profile observability up -d

# 5. Verify health
curl http://localhost:8000/health
open http://localhost:3000
```

## Common commands

| Task                            | Command                                  |
| ------------------------------- | ---------------------------------------- |
| Start core stack (default)      | `docker compose up -d`                   |
| Start with observability        | `docker compose --profile observability up -d` |
| Stop full stack                 | `docker compose down`                    |
| Tail logs                       | `docker compose logs -f`                 |
| Check container health/status   | `docker compose ps`                      |
| Run API tests                   | `cd apps/api && poetry run pytest`       |
| Run API lint                    | `cd apps/api && poetry run ruff check .` |
| Run frontend dev server         | `pnpm web dev`                           |
| Run frontend lint               | `pnpm web lint`                          |
| Run frontend unit tests         | `pnpm web test`                          |
| Type-check entire workspace     | `pnpm type-check`                        |

## Service map

| Service     | Port | Purpose                          |
| ----------- | ---- | -------------------------------- |
| `api`       | 8000 | FastAPI backend                  |
| `web`       | 3000 | Next.js frontend                 |
| `postgres`  | 5432 | Primary OLTP database            |
| `redis`     | 6379 | Cache, rate limiting, pub/sub    |
| `qdrant`    | 6333 | Vector store for RAG             |
| `minio`     | 9000 | S3-compatible document storage   |
| `minio` UI  | 9001 | MinIO admin console              |
| `langfuse`  | 3100 | Optional self-hosted observability UI |

## Startup modes

- **Core mode (default):** starts `postgres`, `redis`, `qdrant`, `minio`, `api`, `web` and does not block on Langfuse.
- **Observability mode:** starts Langfuse services (including dedicated `langfuse-redis`) when requested via `--profile observability`.
- **Verify state:** run `docker compose ps`; core services should be healthy even if observability profile is not started.

## Documentation

- Roadmap: [`EnterpriseGPT_Roadmap.docx`](./EnterpriseGPT_Roadmap.docx)
- Scope: [`Enterprise Scope Doc.pdf`](./Enterprise%20Scope%20Doc.pdf)

## License

Proprietary. All rights reserved.
