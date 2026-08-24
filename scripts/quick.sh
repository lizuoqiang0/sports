#!/usr/bin/env bash
# ============================================================
# OB Sports 一键部署脚本
#   集成语法检查 → 镜像构建 → 容器重建 → 缓存清理 → 健康检查
#
# 用法:
#   bash scripts/quick.sh                # 完整部署（构建+重建+清缓存）
#   bash scripts/quick.sh --no-build     # 跳过构建，仅强制重建容器
#   bash scripts/quick.sh --with-ai     # 同时启动 AI 引擎
#   bash scripts/quick.sh --logs        # 部署后跟踪日志
#   bash scripts/quick.sh --status      # 仅查看状态
#   bash scripts/quick.sh --stop        # 停止所有服务
#   bash scripts/quick.sh --restart     # 重启（不重建镜像）
#   bash scripts/quick.sh -h            # 帮助
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

# ── 颜色 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✅${NC} $1"; }
fail() { echo -e "  ${RED}❌${NC} $1"; }
info() { echo -e "  ${CYAN}ℹ️${NC} $1"; }
step() { echo -e "\n${BOLD}${CYAN}==> $1${NC}"; }

# ── 参数解析 ──
ACTION="deploy"
FORCE_BUILD=1
WITH_AI=0
SHOW_LOGS=0

for arg in "$@"; do
  case "$arg" in
    --no-build)  FORCE_BUILD=0 ;;
    --with-ai)   WITH_AI=1 ;;
    --logs)      SHOW_LOGS=1 ;;
    --status)    ACTION="status" ;;
    --stop)      ACTION="stop" ;;
    --restart)   ACTION="restart"; FORCE_BUILD=0 ;;
    -h|--help)
      cat <<'EOF'
OB Sports 一键部署脚本

  bash scripts/quick.sh                完整部署（构建+重建+清缓存+健康检查）
  bash scripts/quick.sh --no-build     跳过构建，仅强制重建容器
  bash scripts/quick.sh --with-ai      同时启动 AI 引擎
  bash scripts/quick.sh --logs         部署后跟踪后端日志
  bash scripts/quick.sh --status       查看容器状态
  bash scripts/quick.sh --stop         停止所有服务
  bash scripts/quick.sh --restart      重启容器（不重建镜像）
EOF
      exit 0
      ;;
    *)
      echo "未知参数: $arg（使用 -h 查看帮助）"
      exit 1
      ;;
  esac
done

# ── 工具函数 ──
wait_healthy() {
  local name="$1" url="$2" max="${3:-60}"
  for i in $(seq 1 "$max"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      ok "$name 就绪 (${i}s)"
      return 0
    fi
    sleep 1
  done
  fail "$name 健康检查超时"
  return 1
}

clear_redis_cache() {
  step "清除 Redis 旧缓存"
  docker exec ob-redis redis-cli DEL \
    "ai:calibration:v1" \
    "ai:patterns:v1" \
    "ai:risk_tuning:v1" \
    "bets:stats:recent" \
    2>/dev/null && ok "缓存已清除" || info "Redis 未就绪，跳过"
}

syntax_check() {
  step "Python 语法检查"
  local files=(
    app/ai/strategy.py
    app/ai/auto_better.py
    app/ai/analyzer.py
    app/ai/bet_executor.py
    app/ai/strategy_gates.py
    app/api/bets.py
    app/api/ai_bets.py
    app/config.py
    app/core/convert.py
  )
  local all_ok=true
  for f in "${files[@]}"; do
    if python3 -c "import ast; ast.parse(open('$f').read())" 2>/dev/null; then
      ok "$f"
    else
      fail "$f 语法错误"
      all_ok=false
    fi
  done
  if [[ "$all_ok" == "false" ]]; then
    echo -e "\n${RED}语法检查未通过，终止部署${NC}"
    exit 1
  fi
}

build_images() {
  step "重建 Docker 镜像"
  if [[ "$FORCE_BUILD" == "1" ]]; then
    docker compose build --no-cache backend 2>&1 | tail -3
    ok "镜像构建完成"
  else
    info "跳过构建（--no-build）"
  fi
}

recreate_containers() {
  local services=("backend")
  [[ "$WITH_AI" == "1" ]] && services+=("ai-engine")

  step "强制重建容器: ${services[*]}"
  if [[ "$WITH_AI" == "1" ]]; then
    docker compose --profile ai up -d --force-recreate "${services[@]}" 2>&1 | tail -8
  else
    docker compose up -d --force-recreate "${services[@]}" 2>&1 | tail -8
  fi
  ok "容器已重建"
}

show_status() {
  step "容器状态"
  docker compose ps 2>/dev/null
  echo
  info "前端:  http://localhost:3000"
  info "API:   http://localhost:8000/docs"
  info "Gate:  http://localhost:9277/health"
  echo
  info "日志: docker logs ob-backend --tail 50 -f"
  info "AI:   docker logs ob-ai-engine --tail 50 -f"
}

stop_all() {
  step "停止所有服务"
  bash scripts/ensure_browser_gate.sh stop 2>/dev/null || true
  docker compose --profile ai down --remove-orphans 2>/dev/null \
    || docker compose down --remove-orphans
  ok "已停止"
}

restart_containers() {
  step "重启容器（不重建镜像）"
  docker compose restart backend 2>&1 | tail -3
  docker compose --profile ai restart ai-engine 2>/dev/null \
    || docker compose restart ai-engine 2>/dev/null \
    || true
  ok "已重启"
}

tail_logs() {
  echo
  docker logs ob-backend --tail 50 -f
}

# ── 主流程 ──
case "$ACTION" in
  status)
    show_status
    ;;
  stop)
    stop_all
    ;;
  restart)
    restart_containers
    wait_healthy "Backend" "http://127.0.0.1:8000/health" 30
    show_status
    ;;
  deploy)
    syntax_check
    build_images
    clear_redis_cache
    recreate_containers
    step "等待服务健康"
    wait_healthy "Backend" "http://127.0.0.1:8000/health" 60
    show_status
    [[ "$SHOW_LOGS" == "1" ]] && tail_logs
    echo
    ok "部署完成"
    ;;
esac
