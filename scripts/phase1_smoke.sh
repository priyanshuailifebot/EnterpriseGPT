#!/usr/bin/env bash
# Phase 1 repeatable smoke checks (against a running API + Postgres + Redis).
# Prereqs: docker compose up -d postgres redis; migrate apps/api; uvicorn on :8000
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="${ROOT}/apps/api"
BASE_URL="${BASE_URL:-http://localhost:8000}"

log() { printf '%s\n' "$*"; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    log "missing required command: $1"
    exit 1
  }
}

need_cmd curl
need_cmd python3

log "== Health =="
curl -sf "${BASE_URL}/health" | head -c 200 || true
log ""

STAMP="$(date +%s)"
EMAIL="phase1-smoke-${STAMP}@example.com"
PASS="SmokeTestPassword123"

log "== Register =="
REG_JSON="$(curl -sf -X POST "${BASE_URL}/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASS}\",\"full_name\":\"Smoke User\",\"role\":\"viewer\"}")"
if command -v jq >/dev/null 2>&1; then
  TOKEN="$(printf '%s' "${REG_JSON}" | jq -r '.access_token')"
else
  TOKEN="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["access_token"])' "${REG_JSON}")"
fi
log "token acquired (len ${#TOKEN})"

log "== GET /me =="
curl -sf "${BASE_URL}/api/v1/auth/me" -H "Authorization: Bearer ${TOKEN}" | head -c 400 || true
log ""

log "== Wrong password (expect 401) =="
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE_URL}/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"WrongPassword12345\"}")"
test "${code}" = "401"

log "== Rate limit: 61 unauthenticated /me (expect last 429) =="
last=""
for i in $(seq 1 61); do
  last="$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/api/v1/auth/me")"
done
test "${last}" = "429"

log "== PII redact/restore (local Python) =="
export SECRET_KEY="${SECRET_KEY:-test-secret-key-please-do-not-use-in-prod}"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://egpt:egpt_dev_password@localhost:5432/enterprisegpt_test}"
export REDIS_URL="${REDIS_URL:-redis://:egpt_dev_redis_password@localhost:6379/1}"
PY="${API_DIR}/.venv/bin/python"
if [[ -x "${PY}" ]]; then
  "${PY}" -c '
from services.pii_service import PIIService
svc = PIIService()
raw = "Call me at 555-123-4567 and email alice@example.com"
redacted, m = svc.redact(raw)
assert "<<" in redacted and len(m) >= 1
assert svc.restore(redacted, m) == raw
print("PII ok")
'
else
  (cd "${API_DIR}" && PYTHONPATH=. python3 -c '
from services.pii_service import PIIService
svc = PIIService()
raw = "Call me at 555-123-4567 and email alice@example.com"
redacted, m = svc.redact(raw)
assert "<<" in redacted and len(m) >= 1
assert svc.restore(redacted, m) == raw
print("PII ok")
')
fi

log "== RBAC + audit (pytest; requires test DB migrated) =="
if [[ -x "${PY}" ]]; then
  (cd "${API_DIR}" && "${PY}" -m pytest \
    tests/test_auth.py::test_rbac_viewer_blocked_from_workflow_create \
    tests/test_auth.py::test_login_creates_audit_log -q)
else
  log "skip pytest (no ${API_DIR}/.venv); run: cd apps/api && pytest tests/test_auth.py -k rbac_or_audit -q"
fi

log "== Phase 1 smoke complete =="
