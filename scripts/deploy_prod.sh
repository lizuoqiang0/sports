#!/usr/bin/env bash
# 线上启动（无开发挂载）
# 用法: bash scripts/deploy_prod.sh
# 会先把宿主机最新 app/ 与 frontend/dist 打进镜像，避免“重启后变回旧版”
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
test -f .env || { echo "缺少 .env，请从 .env.example 复制并填写"; exit 1; }
mkdir -p logs

# 粗检：弱内部令牌不可上线
if grep -qE '^INTERNAL_API_TOKEN=(ob-internal)?[[:space:]]*$' .env 2>/dev/null; then
  echo "请先设置强 INTERNAL_API_TOKEN: openssl rand -hex 32" >&2
  exit 1
fi

if [[ ! -f frontend/dist/index.html ]]; then
  echo "缺少 frontend/dist，请先在 frontend/ 执行 npm run build" >&2
  exit 1
fi

echo "==> 启动宿主机 Browser Gate"
BOOKMAKER_BROWSER_HEADLESS="${BOOKMAKER_BROWSER_HEADLESS:-0}" \
  bash scripts/ensure_browser_gate.sh start

echo "==> 同步最新代码到镜像（无需 Docker Hub 重建）"
services=(backend frontend)
# AI 引擎是独立进程；已启用时必须随 API 一起重启，才能加载最新策略代码。
if docker inspect ob-ai-engine >/dev/null 2>&1; then
  services+=(ai-engine)
fi

# 确保容器存在以便 docker cp；没有则先用现有镜像起一次
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build postgres redis "${services[@]}" 2>/dev/null || true
sleep 2

if docker inspect ob-backend >/dev/null 2>&1; then
  docker cp "$ROOT/app/." ob-backend:/app/app/
  docker commit ob-backend ob-sports-betting-backend:latest >/dev/null
  echo "backend 镜像已更新"
fi

if docker inspect ob-frontend >/dev/null 2>&1; then
  docker exec ob-frontend sh -c 'rm -rf /usr/share/nginx/html/*'
  docker cp "$ROOT/frontend/dist/." ob-frontend:/usr/share/nginx/html/
  if [[ -f "$ROOT/frontend/nginx.conf" ]]; then
    docker cp "$ROOT/frontend/nginx.conf" ob-frontend:/etc/nginx/conf.d/default.conf
  fi
  docker commit ob-frontend ob-sports-betting-frontend:latest >/dev/null
  echo "frontend 镜像已更新"
fi

# 优先不拉远程；需要完整重建时再传 --build
if [[ "${1:-}" == "--build" ]]; then
  shift
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build "${services[@]}" "$@" || \
    docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build "${services[@]}" "$@"
else
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build --force-recreate "${services[@]}" "$@"
fi

echo "生产栈已启动。前端请经反向代理 HTTPS；API docs 默认关闭。"
echo "Gate: http://127.0.0.1:${GATE_PORT:-9277}/health"
echo "打开: http://127.0.0.1:3000"
