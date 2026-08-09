#!/usr/bin/env bash
# 高性能后端入口：uvloop + httptools + 多 worker（按 CPU 自动）
set -euo pipefail

cd /app

NPROC="$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 4)"
# 默认：min(4, max(2, nproc-2))，配合 DB_POOL 避免打满 Postgres
AUTO_WORKERS=$(( NPROC > 4 ? NPROC - 2 : NPROC ))
if [[ "$AUTO_WORKERS" -lt 2 ]]; then AUTO_WORKERS=2; fi
if [[ "$AUTO_WORKERS" -gt 4 ]]; then AUTO_WORKERS=4; fi

WORKERS="${UVICORN_WORKERS:-0}"
if [[ "$WORKERS" == "0" || -z "$WORKERS" ]]; then
  WORKERS="$AUTO_WORKERS"
fi

LIMIT_CONCURRENCY="${UVICORN_LIMIT_CONCURRENCY:-200}"
BACKLOG="${UVICORN_BACKLOG:-2048}"
KEEPALIVE="${UVICORN_KEEPALIVE:-5}"
# 仅信任本机 / Docker 内网反代；勿用 *
FORWARDED_ALLOW="${UVICORN_FORWARDED_ALLOW_IPS:-127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"

echo "backend start: workers=${WORKERS} cpus=${NPROC} concurrency=${LIMIT_CONCURRENCY} loop=uvloop http=httptools"

if [[ "${SKIP_DB_MIGRATIONS:-0}" != "1" ]]; then
  echo "running db migrations: alembic upgrade head"
  alembic upgrade head
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers "${WORKERS}" \
  --loop uvloop \
  --http httptools \
  --limit-concurrency "${LIMIT_CONCURRENCY}" \
  --backlog "${BACKLOG}" \
  --timeout-keep-alive "${KEEPALIVE}" \
  --proxy-headers \
  --forwarded-allow-ips="${FORWARDED_ALLOW}" \
  --no-access-log
