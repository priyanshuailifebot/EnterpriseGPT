#!/usr/bin/env bash
# EnterpriseGPT — local dev bootstrap.
# Brings up the Docker stack and waits for /health to return 200.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "→ .env missing, copying from .env.example"
  cp .env.example .env
fi

echo "→ docker compose up -d"
docker compose up -d --remove-orphans

echo "→ waiting for API /health (timeout 90s)"
for i in {1..30}; do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    echo "✓ API is healthy"
    curl -s http://localhost:8000/health | sed 's/,/,\n  /g'
    exit 0
  fi
  sleep 3
done

echo "✗ API did not become healthy in time"
docker compose ps
exit 1
