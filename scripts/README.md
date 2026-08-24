# Scripts

## 一键部署

- `quick.sh`: **一键部署脚本**，集成语法检查→镜像构建→容器重建→缓存清理→健康检查。
  - `bash scripts/quick.sh` — 完整部署
  - `bash scripts/quick.sh --no-build` — 跳过构建，仅重建容器
  - `bash scripts/quick.sh --with-ai` — 同时启动 AI 引擎
  - `bash scripts/quick.sh --logs` — 部署后跟踪日志
  - `bash scripts/quick.sh --status` — 查看状态
  - `bash scripts/quick.sh --stop` — 停止所有服务
  - `bash scripts/quick.sh --restart` — 重启容器（不重建镜像）

## 生产与运维

- `prod_up.sh`: 本机 GUI 生产栈启动，可选 `--build`、`--with-ai`。
- `deploy_prod.sh`: 使用生产 Compose 覆写刷新线上容器，并启动 Browser Gate。
- `prod_down.sh`: 停止容器和 Browser Gate；`--wipe` 会清除持久化数据。
- `clean_prod_env.sh`: 清理线上业务数据，只保留指定账号。

## 运行时入口

- `docker_entrypoint_backend.sh`: 后端容器入口。
- `ai_betting_engine.py`: AI 引擎容器入口。
- `browser_gate.py`: 宿主机浏览器网关服务。
- `ensure_browser_gate.sh`: Browser Gate 的启动、守护、检查与停止。
- `clean_env.py`: 由 `clean_prod_env.sh` 在后端容器内调用。

临时模拟、实盘试投和独立测试脚本已移除；自动化测试统一放在 `tests/`。
