# EnterpriseGPT — production deployment

This guide assumes a single Linux VM (or equivalent) with Docker Engine and Docker Compose v2, and a public hostname for HTTPS.

## Prerequisites

- **Docker** 24+ and **Docker Compose** v2 (`docker compose`).
- **Open ports**: `80` / `443` if you terminate TLS at the bundled nginx (`edge` profile), or only what your external load balancer requires.
- **Secrets**: generate strong random values for `SECRET_KEY`, Langfuse `NEXTAUTH_SECRET` / `SALT`, database passwords, and Redis / MinIO credentials.

## Environment variables

1. Copy `.env.example` to `.env` at the monorepo root.
2. Set **all** variables referenced in `docker-compose.prod.yml` (Postgres, Redis, MinIO, Langfuse DB, Langfuse app secrets, `NEXT_PUBLIC_API_URL` as seen **by the browser** — often the public origin, e.g. `https://app.example.com` when nginx fronts both UI and API).
3. **Langfuse** (self‑hosted in compose):
   - `LANGFUSE_DB_PASSWORD`, optional `LANGFUSE_DB_USER` / `LANGFUSE_DB_NAME`
   - `LANGFUSE_NEXTAUTH_SECRET`, `LANGFUSE_SALT` (long random strings)
   - `LANGFUSE_HOST`: URL the **API** uses to push traces, typically `http://langfuse:3000` inside Compose; for local dev with published port `3100`, use `http://localhost:3100`.
4. **API observability** (optional but recommended): `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and the same host as above (mapped to Langfuse’s base URL in `core/tracing.py`).
5. **Persistence root**: `DATA_ROOT` (default `./data`). Bind mounts expect these directories to exist:
   ```bash
   mkdir -p data/postgres data/redis data/qdrant data/minio data/langfuse-postgres
   ```

## Start the production stack

From the repository root:

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

Optional **edge nginx** (reverse proxy, gzip, security headers, long timeouts for `/api/`):

```bash
docker compose -f docker-compose.prod.yml --profile edge up -d
```

Ensure `infra/nginx/nginx.conf` matches your topology. Place TLS certificate and key under `infra/nginx/certs/` (or set `NGINX_TLS_CERT_DIR`) and extend the `server { listen 443 ssl; ... }` block as needed — the shipped config focuses on HTTP port 80.

## Database migrations

Run Alembic **once** against the **application** Postgres (not Langfuse’s DB) before serving traffic:

```bash
docker compose -f docker-compose.prod.yml exec api \
  alembic upgrade head
```

(If `alembic` is not on `PATH` in the image, run the equivalent with `python -m alembic` from `/app`.)

## First admin user

Use the existing registration / bootstrap flow for your environment (e.g. `POST /api/v1/auth/register` if enabled, or a one-off script). Grant **Admin** or **Super admin** so the user receives `analytics:read` and workspace management permissions (`core/permissions.py`).

## SSL certificates

- **Recommended**: terminate TLS at a managed load balancer or CDN, forwarding HTTP to nginx on the VM.
- **On-VM TLS**: mount PEM files into the nginx container and add an `ssl_certificate` / `ssl_certificate_key` server block (not generated here so you can use Let’s Encrypt, ZeroSSL, or corporate CAs).

## Health checks

| Check | URL / command |
| --- | --- |
| API liveness | `GET http://<api-host>:8000/health` (or through nginx `/api/health` if you add a route — default config forwards `/api/` only) |
| API readiness | `GET /ready` (Redis ping) |
| Web | `GET http://<web-host>:3000/` |
| Nginx probe | `GET http://<host>:80/healthz` |
| Langfuse | `GET /api/public/health` on the Langfuse port |

Inside Compose, services define `healthcheck` entries; `docker compose ps` shows health state.

## Langfuse UI

With dev compose, Langfuse is published on host port **3100**. With production compose (no host port by default), attach to the `egpt-net` network from a jump box or publish ports intentionally — adjust compose for your security model.

## Operations notes

- Resource limits are expressed under `deploy.resources` for compatibility; your Compose build may ignore limits unless using Swarm — treat them as documentation or migrate to `mem_limit` / cgroup settings if you need hard caps on plain Compose.
- Traces flush on API shutdown via `flush_traces()` in `main.py` lifespan.
- Rough **cost** and **token** figures in analytics depend on `workflow_executions.output_data` carrying usage metadata; enrich those payloads in your runners if you need accounting‑grade numbers.
