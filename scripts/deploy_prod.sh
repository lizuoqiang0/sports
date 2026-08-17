#!/usr/bin/env bash
# 生产启动
# 用法: bash scripts/deploy_prod.sh
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

echo "==> 启动宿主机 Browser Gate"
BOOKMAKER_BROWSER_HEADLESS="${BOOKMAKER_BROWSER_HEADLESS:-0}" \
  bash scripts/ensure_browser_gate.sh watch

echo "==> 启动生产容器"
services=(backend frontend)
# AI 引擎是独立进程；已启用时必须随 API 一起重启，才能加载最新策略代码。
if docker inspect ob-ai-engine >/dev/null 2>&1; then
  services+=(ai-engine)
fi

# 需要更新代码或依赖时传 --build；运行容器只使用镜像内产物。
if [[ "${1:-}" == "--build" ]]; then
  shift
  docker compose up -d --build "${services[@]}" "$@"
else
  docker compose up -d --no-build --force-recreate "${services[@]}" "$@"
fi

echo "生产栈已启动。前端请经反向代理 HTTPS；API docs 默认关闭。"
echo "Gate: http://127.0.0.1:${GATE_PORT:-9277}/health"
echo "打开: http://127.0.0.1:3000"
