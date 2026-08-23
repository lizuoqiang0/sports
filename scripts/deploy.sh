#!/usr/bin/env bash
# 部署最新代码到生产环境
#   bash scripts/deploy.sh              # 重建镜像 + 重启 backend/ai-engine + 清缓存
#   bash scripts/deploy.sh --no-build   # 跳过构建，仅重启 + 清缓存
#   bash scripts/deploy.sh --with-ai    # 同时启动 AI 引擎（如未运行）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

WITH_AI=0
FORCE_BUILD=1
for arg in "$@"; do
  case "$arg" in
    --no-build) FORCE_BUILD=0 ;;
    --with-ai)  WITH_AI=1 ;;
    -h|--help)
      echo "用法: bash scripts/deploy.sh [--no-build] [--with-ai]"
      echo "  --no-build  跳过镜像构建，仅重启容器 + 清缓存"
      echo "  --with-ai   同时启动 AI 引擎"
      exit 0
      ;;
  esac
done

echo "==> [1/5] 语法检查"
python3 -c "
import ast, sys
files = [
    'app/ai/calibration.py',
    'app/ai/analyzer.py',
    'app/ai/strategy.py',
    'app/services/bet_settlement.py',
    'app/services/bookmakers/site_bet.py',
    'app/services/bookmakers/plugins/pinnacle/bet_ui.py',
]
ok = True
for f in files:
    try:
        with open(f) as fh:
            ast.parse(fh.read())
        print(f'  ✅ {f}')
    except SyntaxError as e:
        print(f'  ❌ {f}: {e}')
        ok = False
sys.exit(0 if ok else 1)
"

echo "==> [2/5] 重建 Docker 镜像"
if [[ "$FORCE_BUILD" == "1" ]]; then
  docker compose build backend 2>&1 | tail -3
else
  echo "  跳过构建（--no-build）"
fi

echo "==> [3/5] 清除 Redis 旧缓存"
docker exec ob-redis redis-cli DEL \
  "ai:calibration:v1" \
  "ai:patterns:v1" \
  "ai:risk_tuning:v1" \
  "bets:stats:recent" \
  2>/dev/null && echo "  ✅ 缓存已清除" || echo "  ⚠️ Redis 未就绪，跳过"

echo "==> [4/5] 重启容器"
docker compose up -d --force-recreate backend 2>&1 | tail -5
if [[ "$WITH_AI" == "1" ]]; then
  docker compose --profile ai up -d --force-recreate ai-engine 2>&1 | tail -3
else
  # ai-engine 镜像与 backend 共享，需重启以加载新代码
  docker compose up -d --force-recreate ai-engine 2>&1 | tail -3
fi

echo "==> [5/5] 等待服务健康"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8000/health" >/dev/null 2>&1; then
    echo "  ✅ API 就绪 (${i}s)"
    break
  fi
  sleep 1
  [[ "$i" == "30" ]] && echo "  ❌ API 健康检查超时" >&2
done

echo
echo "==> 部署完成"
docker compose ps 2>/dev/null | head -8
echo
echo "日志: docker logs ob-backend --tail 50 -f"
