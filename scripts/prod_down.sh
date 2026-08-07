#!/usr/bin/env bash
# 停止服务（默认保留 ./data 持久化数据）
#   bash scripts/prod_down.sh
#   bash scripts/prod_down.sh --wipe   # 危险：删除 data/postgres data/redis
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> 停止宿主机 Browser Gate"
bash scripts/ensure_browser_gate.sh stop 2>/dev/null || true

echo "==> 停止 docker-compose 容器"
docker compose --profile ai down --remove-orphans 2>/dev/null || docker compose down --remove-orphans

if [[ "${1:-}" == "--wipe" ]]; then
  echo "警告: 将删除持久化数据 $ROOT/data"
  rm -rf data/postgres data/redis
  mkdir -p data/postgres data/redis
  chown -R 70:70 data/postgres 2>/dev/null || true
  chown -R 999:999 data/redis 2>/dev/null || true
  echo "已清空 data/postgres data/redis"
else
  echo "已停止容器与 Browser Gate；数据仍保留在 ./data 与 ./logs"
fi
