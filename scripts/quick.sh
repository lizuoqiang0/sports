#!/usr/bin/env bash
# ============================================================
# OB Sports 一键部署脚本（合并 prod_up.sh）
#
# 用法:
#   bash scripts/quick.sh                # 日常部署（语法检查→构建→重建→清缓存→健康检查）
#   bash scripts/quick.sh --init        # 首次部署/全量启动（.env→数据目录→镜像→Browser Gate→全容器）
#   bash scripts/quick.sh --no-build    # 跳过构建，仅强制重建容器
#   bash scripts/quick.sh --with-ai     # 同时启动 AI 引擎
#   bash scripts/quick.sh --logs        # 部署后跟踪日志
#   bash scripts/quick.sh --status      # 仅查看状态
#   bash scripts/quick.sh --stop        # 停止所有服务（含 Browser Gate）
#   bash scripts/quick.sh --stop --wipe # 停止 + 清空持久化数据
#   bash scripts/quick.sh --restart     # 重启（不重建镜像）
#   bash scripts/quick.sh --pull        # 仅预拉基础镜像
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
INIT_MODE=0
PULL_ONLY=0
WIPE_DATA=0

for arg in "$@"; do
  case "$arg" in
    --init)      INIT_MODE=1 ;;
    --no-build)  FORCE_BUILD=0 ;;
    --with-ai)   WITH_AI=1 ;;
    --logs)      SHOW_LOGS=1 ;;
    --status)    ACTION="status" ;;
    --stop)      ACTION="stop" ;;
    --wipe)      WIPE_DATA=1 ;;
    --restart)   ACTION="restart"; FORCE_BUILD=0 ;;
    --pull)      ACTION="pull" ;;
    -h|--help)
      cat <<'EOF'
OB Sports 一键部署脚本

  bash scripts/quick.sh                日常部署（构建+重建+清缓存+健康检查）
  bash scripts/quick.sh --init         首次部署/全量启动（.env+数据目录+Browser Gate+frontend）
  bash scripts/quick.sh --no-build     跳过构建，仅强制重建容器
  bash scripts/quick.sh --with-ai      同时启动 AI 引擎
  bash scripts/quick.sh --logs         部署后跟踪后端日志
  bash scripts/quick.sh --status       查看容器状态
  bash scripts/quick.sh --stop         停止所有服务（含 Browser Gate）
  bash scripts/quick.sh --stop --wipe  停止 + 清空持久化数据（危险）
  bash scripts/quick.sh --restart      重启容器（不重建镜像）
  bash scripts/quick.sh --pull         仅预拉基础镜像
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

wait_container_healthy() {
  local container="$1" cmd="$2" max="${3:-45}"
  for i in $(seq 1 "$max"); do
    if docker exec "$container" $cmd >/dev/null 2>&1; then
      echo "  $container 就绪 (${i}s)"
      return 0
    fi
    sleep 1
  done
  echo "  ❌ $container 健康检查超时" >&2
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
    docker compose build --no-cache backend 2>&1 | grep -E '(DONE|Built|ERROR)' || true
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
  if [[ "$WIPE_DATA" == "1" ]]; then
    echo -e "  ${RED}⚠️  正在删除持久化数据: $ROOT/data${NC}"
    rm -rf data/postgres data/redis
    mkdir -p data/postgres data/redis
    chown -R 70:70 data/postgres 2>/dev/null || true
    chown -R 999:999 data/redis 2>/dev/null || true
    ok "已清空 data/postgres data/redis"
  else
    ok "已停止；数据保留在 ./data 与 ./logs"
  fi
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

pull_bases() {
  step "预拉基础镜像（并行）"
  docker pull postgres:16-alpine &
  docker pull redis:7-alpine &
  docker pull python:3.12-slim &
  docker pull nginx:1.27-alpine &
  docker pull node:20-alpine &
  wait
  ok "基础镜像就绪"
}

# ── 首次部署初始化（原 prod_up.sh 逻辑） ──
init_environment() {
  step "创建数据目录"
  mkdir -p data/postgres data/redis logs
  chown -R 70:70 data/postgres 2>/dev/null || true
  chown -R 999:999 data/redis 2>/dev/null || true
  chmod -R u+rwX,g+rwX data logs 2>/dev/null || true
  ok "目录就绪"

  step "检查 .env 配置"
  if [[ ! -f .env ]]; then
    info "未找到 .env，从 .env.example 生成..."
    [[ -f .env.example ]] && cp .env.example .env || touch .env
    if ! grep -q '^SECRET_KEY=.\+' .env 2>/dev/null; then
      SK="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets;print(secrets.token_hex(32))')"
      echo "SECRET_KEY=${SK}" >> .env
    fi
    ok ".env 已生成，请按需补充 API Key"
  else
    ok ".env 已存在"
  fi

  # 安全检查：弱内部令牌不可上线
  if grep -qE '^INTERNAL_API_TOKEN=(ob-internal)?[[:space:]]*$' .env 2>/dev/null; then
    fail "INTERNAL_API_TOKEN 为空或弱令牌，请设置: openssl rand -hex 32"
    exit 1
  fi

  # 缺基础镜像时先拉
  local need_pull=0
  for img in postgres:16-alpine redis:7-alpine python:3.12-slim nginx:1.27-alpine; do
    docker image inspect "$img" >/dev/null 2>&1 || need_pull=1
  done
  [[ "$FORCE_BUILD" == "1" ]] && {
    docker image inspect node:20-alpine >/dev/null 2>&1 || need_pull=1
  }
  [[ "$need_pull" == "1" ]] && pull_bases

  step "启动依赖: postgres + redis"
  docker compose up -d --force-recreate postgres redis 2>&1 | tail -5
  wait_container_healthy "ob-postgres" "pg_isready -U ob_user -d ob_sports" 45
  wait_container_healthy "ob-redis" "redis-cli ping" 30

  step "配置 Browser Gate"
  # .env 注入 Browser Gate 配置
  local _sed_i="sed -i"
  [[ "$(uname)" == "Darwin" ]] && _sed_i="sed -i ''"
  if grep -q '^BOOKMAKER_BROWSER_GATE_URL=' .env 2>/dev/null; then
    eval "$_sed_i 's|^BOOKMAKER_BROWSER_GATE_URL=.*|BOOKMAKER_BROWSER_GATE_URL=http://host.docker.internal:9277|' .env"
  else
    echo 'BOOKMAKER_BROWSER_GATE_URL=http://host.docker.internal:9277' >> .env
  fi
  if grep -q '^BOOKMAKER_BROWSER_HEADLESS=' .env 2>/dev/null; then
    eval "$_sed_i 's|^BOOKMAKER_BROWSER_HEADLESS=.*|BOOKMAKER_BROWSER_HEADLESS=0|' .env"
  else
    echo 'BOOKMAKER_BROWSER_HEADLESS=0' >> .env
  fi
  ok "Browser Gate 配置已注入"

  step "启动 Browser Gate（可见 Chromium + 守护）"
  BOOKMAKER_BROWSER_HEADLESS=0 bash scripts/ensure_browser_gate.sh watch
  info "等待 Browser Gate 健康..."
  for i in $(seq 1 90); do
    if curl -fsS --noproxy '*' "http://127.0.0.1:${BROWSER_GATE_PORT:-9277}/health" 2>/dev/null \
      | grep -q '"runtime":"host"'; then
      ok "Browser Gate 就绪 (${i}s)"
      break
    fi
    sleep 1
    [[ "$i" == "90" ]] && fail "Browser Gate 超时，请查看: /tmp/browser_gate.log"
  done
}

start_full_stack() {
  local services=("backend" "frontend")
  [[ "$WITH_AI" == "1" ]] && services+=("ai-engine")

  step "启动容器: ${services[*]}"
  local build_args=()
  [[ "$FORCE_BUILD" == "1" ]] && build_args+=(--build)

  if [[ "$WITH_AI" == "1" ]]; then
    docker compose --profile ai up -d ${build_args[@]+"${build_args[@]}"} backend frontend
    docker compose --profile ai up -d ${build_args[@]+"${build_args[@]}"} ai-engine
  else
    docker compose up -d ${build_args[@]+"${build_args[@]}"} backend frontend
  fi
  ok "容器已启动"
}

# ── 主流程 ──
case "$ACTION" in
  pull)
    pull_bases
    ;;
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
    if [[ "$INIT_MODE" == "1" ]]; then
      # 首次部署/全量启动
      init_environment
      syntax_check
      build_images
      clear_redis_cache
      start_full_stack
      step "等待服务健康"
      wait_healthy "Backend" "http://127.0.0.1:8000/health" 60
      show_status
      [[ "$SHOW_LOGS" == "1" ]] && tail_logs
      echo
      ok "首次部署完成"
      info "数据目录: $ROOT/data/postgres  $ROOT/data/redis"
      info "日志目录: $ROOT/logs"
    else
      # 日常部署
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
    fi
    ;;
esac
