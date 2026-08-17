#!/bin/bash
# 确保本机 Browser Gate 常驻（始终弹出可见 Chromium）。
# 用法: bash scripts/ensure_browser_gate.sh {start|watch|status|stop|fix}
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# 从项目 .env 加载 INTERNAL_API_TOKEN / GATE_HOST（不覆盖已导出变量）
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env" 2>/dev/null || true
  set +u
  set +a
  set -u
fi
LOG="${BROWSER_GATE_LOG:-/tmp/browser_gate.log}"
WATCH_LOG="${BROWSER_GATE_WATCH_LOG:-/tmp/browser_gate_watch.log}"
PORT="${GATE_PORT:-9277}"
GATE_PID_FILE="${BROWSER_GATE_PID:-/tmp/browser_gate.pid}"
WATCH_PID_FILE="${BROWSER_GATE_WATCH_PID:-/tmp/browser_gate_watch.pid}"
HOME_DIR="${HOME:-/root}"
if [[ "$(uname -s)" == "Darwin" ]]; then
  DEFAULT_PW_CACHE="$HOME_DIR/Library/Caches/ms-playwright"
else
  DEFAULT_PW_CACHE="$HOME_DIR/.cache/ms-playwright"
fi
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$DEFAULT_PW_CACHE}"
# 禁止落到 Cursor sandbox 缓存（会导致 Executable doesn't exist）
case "${PLAYWRIGHT_BROWSERS_PATH}" in
  *cursor-sandbox-cache*)
    export PLAYWRIGHT_BROWSERS_PATH="$DEFAULT_PW_CACHE"
    ;;
esac
export HOME="$HOME_DIR"
export BOOKMAKER_BROWSER_HEADLESS="${BOOKMAKER_BROWSER_HEADLESS:-0}"
export BOOKMAKER_MANUAL_VENUE="${BOOKMAKER_MANUAL_VENUE:-0}"
# 本机 Gate 不走系统代理（否则 health/启动自检会误失败）
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY="*"
export no_proxy="*"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
# Chromium Crashpad：写到用户可写缓存，避免 Application Support setxattr 失败导致登录弹窗崩溃
if [[ "$(uname -s)" == "Darwin" ]]; then
  export BREAKPAD_DUMP_LOCATION="${BREAKPAD_DUMP_LOCATION:-$HOME_DIR/Library/Caches/ob-sports-betting/chrome-crashes}"
else
  export BREAKPAD_DUMP_LOCATION="${BREAKPAD_DUMP_LOCATION:-$HOME_DIR/.cache/ob-sports-betting/chrome-crashes}"
fi
mkdir -p "$BREAKPAD_DUMP_LOCATION"

has_playwright() {
  local py="$1"
  [[ -x "$py" ]] || return 1
  "$py" -c "from playwright.async_api import async_playwright" >/dev/null 2>&1
}

pick_python() {
  local candidates=()
  if [[ -n "${PYTHON:-}" ]]; then
    candidates+=("$PYTHON")
  fi
  candidates+=(
    "$ROOT/.venv/bin/python"
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
    /usr/local/bin/python3
    "$(command -v python3 2>/dev/null || true)"
  )
  local c
  for c in "${candidates[@]}"; do
    [[ -n "$c" && -x "$c" ]] || continue
    if has_playwright "$c"; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

ensure_deps() {
  mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
  if PY="$(pick_python)"; then
    echo "使用已具备 Playwright 的解释器: $PY"
  else
    echo "==> 未检测到 Playwright，正在安装到项目 .venv ..."
    if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
      local bootstrap
      bootstrap="$(command -v python3 || true)"
      if [[ -z "$bootstrap" ]]; then
        echo "找不到 python3，无法创建 .venv" >&2
        return 1
      fi
      "$bootstrap" -m venv "$ROOT/.venv"
    fi
    PY="$ROOT/.venv/bin/python"
    "$PY" -m pip install -U pip setuptools wheel
    "$PY" -m pip install -r "$ROOT/requirements.txt"
    if ! has_playwright "$PY"; then
      echo "Playwright 安装后仍无法 import，请手动检查: $PY -c 'import playwright'" >&2
      return 1
    fi
    echo "Playwright 已就绪: $PY"
  fi

  # 确保 Chromium 在稳定路径可用
  if ! PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH" "$PY" - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    path = p.chromium.executable_path
    import os
    raise SystemExit(0 if path and os.path.exists(path) else 1)
PY
  then
    echo "==> 安装 Chromium 到 $PLAYWRIGHT_BROWSERS_PATH ..."
    PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH" "$PY" -m playwright install chromium
  fi
}

# 选定 PY（fix/start 前调用 ensure_deps）
PY=""

is_up() {
  # 避免本机 HTTP_PROXY 把 127.0.0.1 也走代理导致误判 DOWN
  curl -sf --noproxy '*' --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1
}

kill_port() {
  local pids
  pids="$(lsof -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "${pids}" ]; then
    # shellcheck disable=SC2086
    kill ${pids} 2>/dev/null || true
    sleep 1
    # shellcheck disable=SC2086
    kill -9 ${pids} 2>/dev/null || true
  fi
}

start_once() {
  if [[ -z "$PY" ]]; then
    ensure_deps || return 1
    PY="$(pick_python)" || return 1
  fi
  if is_up; then
    # 已在跑但可能是错误 Python：检查健康即可；若用户显式 fix 会先 stop
    echo "Browser Gate 已在运行 (port ${PORT}) · $($PY -c 'import sys; print(sys.executable)')"
    return 0
  fi
  echo "启动 Browser Gate → ${LOG}"
  echo "Python: $PY"
  kill_port
  : >"$LOG"
  nohup env -u PNPM_STORE_PATH -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
    NO_PROXY='*' \
    no_proxy='*' \
    HOME="$HOME_DIR" \
    PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH" \
    BREAKPAD_DUMP_LOCATION="$BREAKPAD_DUMP_LOCATION" \
    BOOKMAKER_BROWSER_HEADLESS="${BOOKMAKER_BROWSER_HEADLESS:-0}" \
    BOOKMAKER_MANUAL_VENUE="${BOOKMAKER_MANUAL_VENUE:-0}" \
    INTERNAL_API_TOKEN="${INTERNAL_API_TOKEN:-}" \
    GATE_HOST="${GATE_HOST:-0.0.0.0}" \
    GATE_PORT="${PORT}" \
    PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PY" "$ROOT/scripts/browser_gate.py" >>"$LOG" 2>&1 &
  echo $! >"$GATE_PID_FILE"
  local i
  for i in $(seq 1 30); do
    sleep 0.4
    if is_up; then
      echo "Browser Gate 就绪: http://127.0.0.1:${PORT}"
      return 0
    fi
  done
  echo "启动失败，请查看日志: $LOG" >&2
  tail -40 "$LOG" >&2 || true
  return 1
}

start_watch_daemon() {
  if [[ -z "$PY" ]]; then
    ensure_deps || return 1
    PY="$(pick_python)" || return 1
  fi
  if [ -f "$WATCH_PID_FILE" ]; then
    local old
    old="$(cat "$WATCH_PID_FILE" 2>/dev/null || true)"
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
      echo "守护进程已在运行 (pid=$old)"
      return 0
    fi
  fi

  BROWSER_GATE_ROOT="$ROOT" \
  BROWSER_GATE_PY="$PY" \
  BROWSER_GATE_LOG="$LOG" \
  BROWSER_GATE_WATCH_LOG="$WATCH_LOG" \
  BROWSER_GATE_PID="$GATE_PID_FILE" \
  BROWSER_GATE_WATCH_PID="$WATCH_PID_FILE" \
  BROWSER_GATE_PORT="$PORT" \
  "$PY" - <<'PY'
import os, sys, time, subprocess
from pathlib import Path

root = Path(os.environ["BROWSER_GATE_ROOT"])
py = os.environ["BROWSER_GATE_PY"]
log = Path(os.environ["BROWSER_GATE_LOG"])
wlog = Path(os.environ["BROWSER_GATE_WATCH_LOG"])
gate_pid_file = Path(os.environ["BROWSER_GATE_PID"])
watch_pid_file = Path(os.environ["BROWSER_GATE_WATCH_PID"])
port = int(os.environ.get("BROWSER_GATE_PORT") or "9277")

def is_up():
    import urllib.request
    import socket
    # 登录/采盘会短暂占满事件循环：health 偶发超时 ≠ 进程已死
    # 1) 端口仍在监听 → 视为存活，绝不杀窗
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            pass
        port_open = True
    except Exception:
        port_open = False
    try:
        # 本机 health 禁止走 HTTP_PROXY，否则误判 DOWN 并杀掉长连接
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            if r.status == 200:
                return True
    except Exception:
        pass
    return port_open

def kill_port():
    try:
        out = subprocess.check_output(["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"], text=True).strip()
    except Exception:
        out = ""
    for pid in out.split():
        try:
            os.kill(int(pid), 15)
        except Exception:
            pass
    time.sleep(1)
    for pid in out.split():
        try:
            os.kill(int(pid), 9)
        except Exception:
            pass

def loop():
    wlog.parent.mkdir(parents=True, exist_ok=True)
    with wlog.open("a") as wf:
        wf.write(f"{time.strftime('%F %T')} watch started py={py}\n")
        wf.flush()
        while True:
            if not is_up():
                wf.write(f"{time.strftime('%F %T')} Gate down, restarting...\n")
                wf.flush()
                kill_port()
                lf = log.open("a")
                env = os.environ.copy()
                pw = env.get("PLAYWRIGHT_BROWSERS_PATH") or ""
                if sys.platform == "darwin":
                    stable = str(Path.home() / "Library" / "Caches" / "ms-playwright")
                else:
                    stable = str(Path.home() / ".cache" / "ms-playwright")
                if (not pw) or ("cursor-sandbox-cache" in pw) or (not Path(pw).exists()):
                    env["PLAYWRIGHT_BROWSERS_PATH"] = stable
                for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                    env.pop(k, None)
                env["NO_PROXY"] = "*"
                env["no_proxy"] = "*"
                env.setdefault("BOOKMAKER_MANUAL_VENUE", "0")
                env.setdefault("BOOKMAKER_BROWSER_HEADLESS", "0")
                env["PYTHONPATH"] = str(root) + ((":" + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
                p = subprocess.Popen(
                    [py, str(root / "scripts" / "browser_gate.py")],
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=env,
                    cwd=str(root),
                )
                gate_pid_file.write_text(str(p.pid))
                time.sleep(2)
                wf.write(
                    f"{time.strftime('%F %T')} "
                    + ("Gate 已恢复\n" if is_up() else "Gate 重启后仍不可用\n")
                )
                wf.flush()
            time.sleep(5)

if os.fork() > 0:
    sys.exit(0)
os.setsid()
if os.fork() > 0:
    sys.exit(0)
os.chdir("/")
watch_pid_file.write_text(str(os.getpid()))
loop()
PY
  sleep 0.5
  echo "守护进程已启动 → ${WATCH_LOG} (pid=$(cat "$WATCH_PID_FILE" 2>/dev/null || echo '?'))"
}

cmd_status() {
  if is_up; then
    echo "Gate: UP  http://127.0.0.1:${PORT}/health"
    curl -fsS --noproxy '*' "http://127.0.0.1:${PORT}/health" || true
    echo
  else
    echo "Gate: DOWN"
  fi
  if [[ -f "$GATE_PID_FILE" ]]; then
    echo "gate_pid_file=$(cat "$GATE_PID_FILE")"
  fi
  if [[ -f "$WATCH_PID_FILE" ]]; then
    echo "watch_pid_file=$(cat "$WATCH_PID_FILE")"
  fi
  lsof -iTCP:${PORT} -sTCP:LISTEN 2>/dev/null || true
}

cmd_stop() {
  if [[ -f "$WATCH_PID_FILE" ]]; then
    local w
    w="$(cat "$WATCH_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$w" ]]; then
      kill "$w" 2>/dev/null || true
      kill -9 "$w" 2>/dev/null || true
    fi
    rm -f "$WATCH_PID_FILE"
  fi
  # 杀掉所有相关进程，避免残留二次绑定 9277
  pkill -f "$ROOT/scripts/browser_gate.py" 2>/dev/null || true
  pkill -f 'BROWSER_GATE_ROOT=' 2>/dev/null || true
  kill_port
  rm -f "$GATE_PID_FILE"
  sleep 1
  echo "已停止 Browser Gate / 守护进程"
}

cmd_fix() {
  echo "==> 一次性修复本机可见浏览器 Gate"
  cmd_stop
  ensure_deps || return 1
  PY="$(pick_python)" || return 1
  start_once || return 1
  # 默认不启 watch：旧版 watch 在登录占事件循环时会误杀长连接。
  # 需要守护时显式: bash scripts/ensure_browser_gate.sh watch
  if [[ "${BROWSER_GATE_ENABLE_WATCH:-0}" == "1" ]]; then
    start_watch_daemon || return 1
  else
    echo "跳过守护进程（设 BROWSER_GATE_ENABLE_WATCH=1 可开启）"
  fi
  echo "==> 自检"
  curl -fsS --noproxy '*' "http://127.0.0.1:${PORT}/health"; echo
  echo "完成。请在「站点」页重新点验证，应弹出 Chromium。"
}

case "${1:-start}" in
  fix)
    cmd_fix
    ;;
  start)
    ensure_deps || exit 1
    PY="$(pick_python)" || exit 1
    start_once
    ;;
  watch)
    ensure_deps || exit 1
    PY="$(pick_python)" || exit 1
    start_once
    start_watch_daemon
    ;;
  status)
    cmd_status
    ;;
  stop)
    cmd_stop
    ;;
  *)
    echo "用法: $0 {start|watch|status|stop|fix}" >&2
    exit 1
    ;;
esac
