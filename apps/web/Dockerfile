# syntax=docker/dockerfile:1.6
# Context: repository root (lockfile + workspace live at root)
#   docker build -f apps/web/Dockerfile --target development .

FROM node:20-alpine AS base
ENV PNPM_HOME="/pnpm" PATH="/pnpm:$PATH" NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@9.12.0 --activate \
    && addgroup -S app && adduser -S app -G app

WORKDIR /workspace

# ---------- deps ----------
FROM base AS deps
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json ./apps/web/
COPY packages/shared-types/package.json ./packages/shared-types/
COPY packages/ui-kit/package.json ./packages/ui-kit/
RUN pnpm install --frozen-lockfile --filter @enterprisegpt/web...

# ---------- builder ----------
FROM base AS builder
ENV NODE_ENV=production
WORKDIR /workspace
COPY --from=deps /workspace ./
COPY apps/web ./apps/web
RUN pnpm --filter @enterprisegpt/web build

# ---------- development ----------
FROM base AS development
ENV NODE_ENV=development
WORKDIR /workspace
COPY --from=deps /workspace ./
COPY apps/web ./apps/web
USER root
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD wget -q --spider http://localhost:3000/ || exit 1
CMD ["pnpm", "--filter", "@enterprisegpt/web", "dev"]

# ---------- production ----------
FROM node:20-alpine AS production
ENV NODE_ENV=production PORT=3000
RUN addgroup -S app && adduser -S app -G app
WORKDIR /app
COPY --from=builder --chown=app:app /workspace/apps/web/.next/standalone ./
COPY --from=builder --chown=app:app /workspace/apps/web/.next/static ./.next/static
COPY --from=builder --chown=app:app /workspace/apps/web/public ./public
USER app
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
  CMD wget -q --spider http://localhost:3000/ || exit 1
CMD ["node", "server.js"]
