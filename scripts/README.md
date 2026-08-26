# Scripts

## 部署与运维（quick.sh — 唯一入口）

- `quick.sh`: 集成首次部署、日常部署、状态查看、停止、重启、清数据为一体。
  - `bash scripts/quick.sh --init --with-ai` — 首次部署/全量启动（.env+数据目录+Browser Gate+frontend）
  - `bash scripts/quick.sh` — 日常部署（全量语法检查→构建 backend/frontend→重建→清临时缓存→就绪检查）
  - `bash scripts/quick.sh --no-build` — 跳过构建，仅重建容器
  - `bash scripts/quick.sh --with-ai` — 同时启动 AI 引擎
  - `bash scripts/quick.sh --logs` — 部署后跟踪日志
  - `bash scripts/quick.sh --status` — 查看状态
  - `bash scripts/quick.sh --stop` — 停止所有服务（含 Browser Gate）
  - `bash scripts/quick.sh --stop --wipe` — 停止 + 清空持久化数据（危险）
  - `bash scripts/quick.sh --restart` — 重启容器（不重建镜像）
  - `bash scripts/quick.sh --pull` — 仅预拉基础镜像

## 运维

- `clean_prod_env.sh`: 清理线上业务数据，只保留指定账号。
- `backtest_balanced_profile.py`: 只读回放足球/篮球70%–80%滚动目标平衡档。
- `backtest_precision_profile.py`: 只读回放旧高精度档，作为策略对照。

## 运行时入口

- `docker_entrypoint_backend.sh`: 后端容器入口。
- `ai_betting_engine.py`: AI 引擎容器入口。
- `browser_gate.py`: 宿主机浏览器网关服务。
- `ensure_browser_gate.sh`: Browser Gate 的启动、守护、检查与停止。
- `clean_env.py`: 由 `clean_prod_env.sh` 在后端容器内调用。

已合并：`prod_up.sh` → `quick.sh --init`；`prod_down.sh` → `quick.sh --stop [--wipe]`；`deploy.sh` 被 `quick.sh` 替代；`deploy_prod.sh` 安全检查合并后删除；`live_monitor.py` 被 `app/services/live_monitor.py` 替代。
