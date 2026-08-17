#!/usr/bin/env bash
# Mac/GUI 生产启动（docker-compose 编排 + 宿主机 Browser Gate）
#   bash scripts/prod_up.sh              # 日常：有镜像则秒级 up，不重建
#   bash scripts/prod_up.sh --build      # 代码/依赖变更后重建镜像
#   bash scripts/prod_up.sh --pull       # 仅预拉基础镜像
#   bash scripts/prod_up.sh --with-ai    # 同时启动 AI 引擎
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

WITH_AI=0
FORCE_BUILD=0
PULL_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --with-ai) WITH_AI=1 ;;
    --build) FORCE_BUILD=1 ;;
    --pull) PULL_ONLY=1 ;;
    -h|--help)
      sed -n '2,8p' "$0"
      exit 0
      ;;
  esac
done

mkdir -p data/postgres data/redis logs
chown -R 70:70 data/postgres 2>/dev/null || true
chown -R 999:999 data/redis 2>/dev/null || true
chmod -R u+rwX,g+rwX data logs 2>/dev/null || true

if [[ ! -f .env ]]; then
  echo "未找到 .env，从 .env.example 生成..."
  [[ -f .env.example ]] && cp .env.example .env || touch .env
  if ! grep -q '^SECRET_KEY=.\+' .env 2>/dev/null; then
    SK="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets;print(secrets.token_hex(32))')"
    echo "SECRET_KEY=${SK}" >> .env
  fi
  grep -q '^WEAK_SECRET_BLOCK_IN_PROD=' .env 2>/dev/null || echo "WEAK_SECRET_BLOCK_IN_PROD=true" >> .env
  echo "已写入 .env，请按需补充 API Key"
fi

pull_bases() {
  echo "==> 预拉基础镜像（可并行）"
  docker pull postgres:16-alpine &
  docker pull redis:7-alpine &
  docker pull python:3.12-slim &
  docker pull nginx:1.27-alpine &
  docker pull node:20-alpine &
  wait
  echo "基础镜像就绪"
}

if [[ "$PULL_ONLY" == "1" ]]; then
  pull_bases
  exit 0
fi

# 缺基础镜像时先拉，避免 build 卡死
need_pull=0
for img in postgres:16-alpine redis:7-alpine python:3.12-slim nginx:1.27-alpine; do
  docker image inspect "$img" >/dev/null 2>&1 || need_pull=1
done
if [[ "$FORCE_BUILD" == "1" ]]; then
  docker image inspect node:20-alpine >/dev/null 2>&1 || need_pull=1
fi
if [[ "$need_pull" == "1" ]]; then
  pull_bases
fi

COMPOSE=(docker compose)
PROFILES=()
[[ "$WITH_AI" == "1" ]] && PROFILES+=(--profile ai)

echo "==> 启动依赖: postgres + redis（高性能参数）"
"${COMPOSE[@]}" up -d --force-recreate postgres redis

echo "==> 等待数据库健康..."
for i in $(seq 1 45); do
  if docker exec ob-postgres pg_isready -U ob_user -d ob_sports >/dev/null 2>&1 \
    && docker exec ob-redis redis-cli ping 2>/dev/null | grep -q PONG; then
    echo "依赖就绪 (${i}s)"
    docker exec ob-redis redis-cli CONFIG GET io-threads 2>/dev/null || true
    break
  fi
  sleep 1
  if [[ "$i" == "45" ]]; then
    echo "等待 postgres/redis 超时" >&2
    exit 1
  fi
done

BUILD_ARGS=()
if [[ "$FORCE_BUILD" == "1" ]]; then
  echo "==> 强制重建镜像"
  BUILD_ARGS+=(--build)
elif ! docker image inspect ob-sports-betting-backend:latest >/dev/null 2>&1 \
  || ! docker image inspect ob-sports-betting-frontend:latest >/dev/null 2>&1; then
  echo "==> 业务镜像不存在，首次构建"
  BUILD_ARGS+=(--build)
else
  echo "==> 使用已有镜像快速启动（跳过 build）"
fi

# 宿主机弹出可见浏览器：后端经 host.docker.internal 调用本机 Gate
if [[ -f .env ]]; then
  if grep -q '^BOOKMAKER_BROWSER_GATE_URL=' .env; then
    sed -i.bak 's|^BOOKMAKER_BROWSER_GATE_URL=.*|BOOKMAKER_BROWSER_GATE_URL=http://host.docker.internal:9277|' .env 2>/dev/null \
      || sed -i '' 's|^BOOKMAKER_BROWSER_GATE_URL=.*|BOOKMAKER_BROWSER_GATE_URL=http://host.docker.internal:9277|' .env
  else
    echo 'BOOKMAKER_BROWSER_GATE_URL=http://host.docker.internal:9277' >> .env
  fi
  if grep -q '^BOOKMAKER_BROWSER_HEADLESS=' .env; then
    sed -i.bak 's|^BOOKMAKER_BROWSER_HEADLESS=.*|BOOKMAKER_BROWSER_HEADLESS=0|' .env 2>/dev/null \
      || sed -i '' 's|^BOOKMAKER_BROWSER_HEADLESS=.*|BOOKMAKER_BROWSER_HEADLESS=0|' .env
  else
    echo 'BOOKMAKER_BROWSER_HEADLESS=0' >> .env
  fi
fi

echo "==> 启动宿主机 Browser Gate（可见 Chromium + 守护）"
BOOKMAKER_BROWSER_HEADLESS=0 bash scripts/ensure_browser_gate.sh watch
echo "==> 等待 Browser Gate 健康..."
for i in $(seq 1 90); do
  if curl -fsS --noproxy '*' "http://127.0.0.1:${BROWSER_GATE_PORT:-9277}/health" 2>/dev/null | grep -q '"runtime":"host"'; then
    echo "Browser Gate 就绪 (${i}s)"
    break
  fi
  sleep 1
  if [[ "$i" == "90" ]]; then
    echo "Browser Gate 超时，请查看: /tmp/browser_gate.log" >&2
  fi
done

if [[ "$WITH_AI" == "1" ]]; then
  echo "==> 启动 backend + frontend + ai-engine"
else
  echo "==> 启动 backend + frontend"
fi
"${COMPOSE[@]}" ${PROFILES[@]+"${PROFILES[@]}"} up -d ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} backend frontend
if [[ "$WITH_AI" == "1" ]]; then
  "${COMPOSE[@]}" --profile ai up -d ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} ai-engine
fi

# 等后端健康，最多 60s
echo "==> 等待 API 就绪..."
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${BACKEND_PORT:-8000}/health" >/dev/null 2>&1; then
    echo "API 就绪 (${i}s)"
    break
  fi
  sleep 1
  if [[ "$i" == "60" ]]; then
    echo "API 健康检查超时，请查看: docker logs ob-backend --tail 80" >&2
  fi
done

echo "==> 状态"
"${COMPOSE[@]}" ${PROFILES[@]+"${PROFILES[@]}"} ps
echo
echo "前端:  http://localhost:${FRONTEND_PORT:-3000}"
echo "API:   http://localhost:${BACKEND_PORT:-8000}/docs"
echo "Gate:  http://localhost:${BROWSER_GATE_PORT:-9277}/health"
echo "数据:  $ROOT/data/postgres  $ROOT/data/redis  （重启/重建容器不丢）"
echo "日志:  $ROOT/logs"
echo
echo "日常重启（秒级）: bash scripts/prod_up.sh"
echo "改代码后重建:     bash scripts/prod_up.sh --build"
echo "停止:             bash scripts/prod_down.sh"
