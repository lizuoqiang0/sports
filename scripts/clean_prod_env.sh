#!/usr/bin/env bash
# 清空线上业务数据，保留干净登录环境（生产维护脚本）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> 清空 Postgres 业务数据"
docker cp "$ROOT/scripts/clean_env.py" ob-backend:/app/scripts/clean_env.py
docker exec -w /app -e PYTHONPATH=/app \
  -e CLEAN_KEEP_USER="${CLEAN_KEEP_USER:?请设置环境变量 CLEAN_KEEP_USER}" \
  -e CLEAN_KEEP_PASSWORD="${CLEAN_KEEP_PASSWORD:?请设置环境变量 CLEAN_KEEP_PASSWORD}" \
  -e CLEAN_KEEP_BALANCE="${CLEAN_KEEP_BALANCE:-0}" \
  ob-backend python scripts/clean_env.py

echo "==> 清空 Redis"
docker exec ob-redis redis-cli FLUSHDB >/dev/null

echo "==> 重启 backend（丢掉内存会话）"
docker compose restart backend >/dev/null
sleep 4
curl -sf "http://127.0.0.1:${BACKEND_PORT:-8000}/health" >/dev/null && echo "API ok"
curl -sf "http://127.0.0.1:${BROWSER_GATE_PORT:-9277}/health" >/dev/null && echo "Gate ok"

echo "==> 计数抽查"
docker exec ob-postgres psql -U ob_user -d ob_sports -c \
  "SELECT
     (SELECT count(*) FROM matches) AS matches,
     (SELECT count(*) FROM odds) AS odds,
     (SELECT count(*) FROM bets) AS bets,
     (SELECT count(*) FROM sport_events) AS sport_events,
     (SELECT count(*) FROM users) AS users,
     (SELECT count(*) FROM bookmaker_accounts) AS bookmakers;"

echo "干净环境就绪：保留账号 ${CLEAN_KEEP_USER}，余额 ${CLEAN_KEEP_BALANCE:-0}，双站（OB + 平博）待填真实网址"
