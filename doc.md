# OB Sports 投注平台 — 技术文档

> **版本**：v2.2.0
> **更新日期**：2026-08-25
> **技术栈**：FastAPI + React + PostgreSQL + Redis + Playwright  

---

## 一、项目概述

OB Sports 是一个**体育赔率监控与 AI 自动投注平台**，支持 OB 体育和平博（Pinnacle）双站滚球赛事的实时盘口监控、AI 分析和自动/手动下单。

### 核心能力

| 能力 | 说明 |
|------|------|
| 双站盘口监控 | OB 体育 + 平博（Pinnacle）实时滚球赔率，通过 Browser Gate 长连接 Chromium 采集 |
| AI 智能分析 | DeepSeek 模型分析全场大小球（under/over），结合赛前上下文（交锋/近况/积分/伤停） |
| 跨站比价择优 | 同一场比赛在 OB 和平博各有一条记录，自动选择赔率最优站点下单 |
| 同场投注控制 | 同一场比赛最多投注 2 次（`MAX_BETS_PER_FIXTURE=2`，可配置） |
| 别名安全生效 | 高置信队名别名自动生效；低于 0.75 的候选只进入审计待复核，不污染运行时索引 |
| 双模式切换 | 人工模式（只出推荐，手动下单）/ 自动模式（命中后自动下单） |
| 严格风控 | 止损/止盈、日注数限制、赔率区间过滤、中国赛事过滤、虚拟盘过滤 |

---

## 二、系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    宿主机 (Mac/GUI)                       │
│  ┌──────────────┐    ┌──────────────────────────────┐   │
│  │ Browser Gate │    │       Docker Compose          │   │
│  │ (可见Chromium)│    │  ┌────────┐  ┌────────────┐  │   │
│  │ :9277        │◄──►│  │Backend │  │  Frontend  │  │   │
│  │ OB + 平博     │    │  │:8000   │  │  :3000     │  │   │
│  └──────────────┘    │  └───┬────┘  └────────────┘  │   │
│                      │      │                         │   │
│                      │  ┌───▼──┐  ┌──────────────┐   │   │
│                      │  │Redis │  │  PostgreSQL  │   │   │
│                      │  │:6379 │  │   :5432      │   │   │
│                      │  └──────┘  └──────────────┘   │   │
│                      │  ┌────────┐                    │   │
│                      │  │AI Engine│ (可选, --with-ai)  │   │
│                      │  └────────┘                    │   │
│                      └──────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### Docker 服务

| 服务 | 镜像 | 端口 | 说明 |
|------|------|------|------|
| postgres | `${BASE_IMAGE_REGISTRY}/library/postgres:16-alpine` | 5432 | 数据库，max_connections=200 |
| redis | `${BASE_IMAGE_REGISTRY}/library/redis:7-alpine` | 6379 | 缓存 + Pub/Sub + 限流 |
| backend | ob-sports-betting-backend | 8000 | FastAPI，多 worker + uvloop |
| frontend | ob-sports-betting-frontend | 3000 | Nginx 静态资源 |
| ai-engine | ob-sports-betting-backend | — | AI 引擎守护进程（可选） |

---

## 三、后端架构

### 3.1 目录结构

```
app/
├── main.py              # FastAPI 入口
├── config.py            # 全局配置（pydantic-settings）
├── database.py          # 异步 SQLAlchemy 引擎
├── ai/                  # AI 分析与投注引擎（9 个文件）
├── api/                 # API 路由（8 个模块）
├── core/                # 基础设施（缓存/加密/安全/WebSocket/类型转换工具）
├── models/user.py       # ORM 数据模型（8 张表）
├── schemas/__init__.py  # Pydantic 请求/响应 Schema
└── services/            # 业务服务层
    ├── bookmakers/      # 博彩站点连接器（28 个文件）
    │   └── plugins/     # OB + Pinnacle 插件
    └── *.py             # 独立服务（16 个文件）
```

### 3.2 AI 模块 (`app/ai/`)

| 文件 | 职责 |
|------|------|
| `analyzer.py` | DeepSeek 赛事分析：调用 LLM 分析全场大小球(under/over)，输出预测方向+置信度+理由 |
| `strategy.py` | 策略引擎：`StrategyConfig` 参数模型，五阶段闸门链（A信号→B结构→C市场→D滚球→E赔率），under/over 独立闸门参数，动态仓位计算 |
| `strategy_gates.py` | 策略门禁：人工/自动共用，最低仓位、仓位上限、站点最低注额协调 |
| `auto_better.py` | 自动投注主调度器：扫描滚球 → LLM 分析 → 策略评估 → 自动下单 |
| `bet_executor.py` | 统一下单执行器：跨站比价、provider 四级 fallback、未连接站自动切换、下单重试 |
| `market_recommend.py` | 盘口推荐：OB/平博单边模式，生成全场大小球推荐 |
| `analysis_filters.py` | 分析候选过滤：超盘口/即将结束/赔率区间过滤 |
| `match_context.py` | 赛前上下文采集：交锋/近况/积分/伤停，DB缓存→捷报爬虫→空结构 |
| `recs_job.py` | 后台预分析任务：赛事页滚球预分析，结果存 Redis |

### 3.3 投注执行链路

```
AIBettingEngine._run_cycle()
  │
  ├─ _scan_candidates()
  │   ├─ 查 LIVE 赛事（足球/篮球）
  │   ├─ 过滤：已下注/虚拟/中国赛事/排除球队/未连接站
  │   ├─ group_matches_by_fixture() ← OB+平博同场分组
  │   └─ load_all_market_odds_pack() ← 并行加载赔率
  │
  ├─ analyze_fixture_group() ← 同组只跑一次 LLM
  │   └─ pick_canonical_match() ← 选代表场（优先 OB）
  │
  ├─ analyze_and_recommend()
  │   ├─ fetch_match_context() ← 赛前上下文
  │   ├─ analyzer.analyze_match() ← DeepSeek 分析
  │   └─ build_match_market_recommendations() ← 跨站比价
  │
  ├─ evaluate_bet() ← 策略闸门（风控/止损止盈/共识门禁）
  │
  └─ _execute_bet() → bet_executor.execute_bet()
      ├─ get_best_market_pack() ← 跨站比价选最优站点
      ├─ provider_code 四级 fallback
      ├─ 未连接站自动切换
      ├─ connector.place_bet() ← 真实下单
      └─ Bet/Transaction 落库 + WS 通知
```

### 3.4 同场投注控制

| 层级 | 机制 | 说明 |
|------|------|------|
| 轮次内 | `placed_fixture_counts` dict | match_id → 已下注次数，≥ MAX_BETS_PER_FIXTURE 跳过 |
| 跨轮次 | `_match_bet_count()` | DB 今日注单 + sibling 注单 + Redis pending 三重计数 |
| 执行层 | `_execute_bet` sibling 检查 | 同场 SUCCESS 注单计数 ≥ 上限则拦截 |

- `MAX_BETS_PER_FIXTURE = 2`（默认 2 次/场，可通过 `AI_MAX_BETS_PER_FIXTURE` 配置）
- `sibling_match_ids` 包含自身，计数递增只走 sib 循环，避免双重计数

### 3.5 别名安全生效

```
匹配失败
  ↓
_record_alias_candidate()
  ├─ 写入候选别名到 Redis
  ├─ best_score ≥ 0.75 → 构建并写入正式别名
  ├─ best_score < 0.75 → 保留候选/审计，等待人工复核
  └─ 生效后清空运行时索引缓存并写审计日志
  ↓
下一轮匹配（≤60s TTL）
  ├─ _get_runtime_alias_index() ← 从 Redis 加载正式别名
  ├─ _team_alias_variants() ← 扩展队名变体
  └─ 匹配成功
```

### 3.6 API 路由

| 前缀 | 模块 | 主要端点 |
|------|------|----------|
| `/api/v1/auth` | auth.py | 注册/登录/刷新Token |
| `/api/v1/matches` | matches.py | 赛事列表/详情/搜索 |
| `/api/v1/matches/{id}/odds` | odds.py | 赔率查询 + WS 实时订阅 |
| `/api/v1/bets` | bets.py | 下单/撤单/历史 |
| `/api/v1/ai` | ai_bets.py | AI 配置/启停/推荐/一键投注 |
| `/api/v1/admin` | admin.py | 数据源开关/预取控制/别名管理 API |
| `/api/v1/monitoring` | monitoring.py | 监控概览/投注模式 |

### 3.7 数据模型

| 模型 | 表名 | 关键字段 |
|------|------|----------|
| User | users | balance, ai_enabled, bet_mode(manual/active), role |
| AIConfig | ai_configs | max_bet_amount, max_daily_bets, stop_loss, take_profit, min/max_odds |
| Match | matches | sport, home_team, away_team, external_id(ob:/pinnacle:), extra_data.ids |
| Odds | odds | bet_type, odds_data(JSON), provider, valid_from/to(版本化) |
| Bet | bets | selection, odds, stake, status, is_ai_bet, provider, external_bet_id |
| BookmakerAccount | bookmaker_accounts | code(ob/pinnacle), session_token_encrypted, balance, status |
| MatchContextRow | match_contexts | fixture_key, payload(JSON), quality |
| Transaction | transactions | type(bet_place/ai_bet), amount, balance_after |

### 3.8 跨站同场匹配

| 函数 | 位置 | 说明 |
|------|------|------|
| `fixture_key()` | fixture_key.py | 稳定键：球类 + 规范化队名（字典序）+ 6h 时间桶 |
| `same_fixture()` | fixture_key.py | 队名相似度 ≥0.72 + 开球时间差 ≤6h |
| `group_matches_by_fixture()` | fixture_key.py | 贪心聚类：OB + 平博同场合并 |
| `sibling_match_ids()` | fixture_key.py | 查同场其他 Match.id（含自身） |
| `pick_canonical_match()` | fixture_key.py | 选分析代表场：优先 OB > 平博 |

---

## 四、前端架构

### 4.1 技术栈

- React 18 + Vite + Tailwind CSS
- 蓝白商务风主题（slate 色系 + blue 品牌色）
- 浅色侧边栏（白底 + 蓝字）
- lucide-react 图标库

### 4.2 页面

| 路由 | 文件 | 说明 |
|------|------|------|
| `/login` | Login.jsx | 登录/注册 |
| `/` | Dashboard.jsx | 工作台概览 |
| `/matches` | Matches.jsx | 赛事列表 |
| `/matches/:id` | MatchDetail.jsx | 赛事详情 + 跨站比价 |
| `/portfolio` | Portfolio.jsx | 投注记录 |
| `/ai` | AIPanel.jsx | AI 投注面板 |
| `/bookmakers` | Bookmakers.jsx | 站点管理 |
| `/logs` | Logs.jsx | AI 日志 |

### 4.3 主题配置

| 文件 | 说明 |
|------|------|
| `tailwind.config.js` | brand 色板（蓝色 9 阶）、ink 色板（slate 10 阶）、语义色 |
| `src/index.css` | CSS 变量 + 组件类（.card/.btn-*/.input/.badge-*）+ App shell + Login |
| `src/main.jsx` | Toaster 全局样式 |

---

## 五、配置项

### 5.1 核心配置 (`app/config.py`)

```python
# === DeepSeek API ===
DEEPSEEK_API_KEY: Optional[str] = None
DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL: str = "deepseek-v4-pro-0813"
DEEPSEEK_TIMEOUT_SEC: float = 90.0          # 单次 DeepSeek 分析总超时（含一次降配重试）
LLM_CLIENT_TIMEOUT_SEC: float = 50.0   # OpenAI-compatible 客户端超时（须 ≥ DEEPSEEK_TIMEOUT_SEC）
LLM_MAX_TOKENS: int = 3072             # DeepSeek 最大输出 tokens
LLM_DEFAULT_CONFIDENCE: float = 0.33   # DeepSeek 未返回置信度时的默认值
LLM_TEMPERATURE: float = 0.35          # DeepSeek 温度（0.2太低导致模板化，0.35平衡）
LLM_CACHE_TTL: int = 600               # 10分钟正缓存
AI_SKIP_CACHE_TTL: int = 180           # DeepSeek 判 skip 的负缓存
LLM_NEG_CACHE_TTL: int = 150            # 无共识结果负缓存

# === AI 分析 ===
AI_MIN_CONFIDENCE: float = 0.47        # 下单最低置信度（含纯盘口模式）
AI_MIN_ODDS: float = 1.65              # 下单最低赔率
AI_MAX_ODDS: float = 5.00              # 下单最高赔率
LIVE_ODDS_MAX_AGE_SEC: int = 90        # 滚球下单允许的赔率最大年龄
AI_SCAN_INTERVAL_SEC: int = 120        # 引擎扫描间隔（有候选时）
AI_IDLE_RESCAN_SEC: int = 30           # 空轮快扫间隔（无候选时）
AI_RECS_LIMIT: int = 80                # 推荐页分析上限
AI_LIVE_SCAN_LIMIT: int = 120          # 自动引擎扫描上限
AI_ANALYZE_CONCURRENCY: int = 8        # DeepSeek 分析并发数
AI_SKIP_COOLDOWN_SEC: int = 300        # 同场 LLM skip 冷却
AI_ENABLE_OVER: bool = True            # 大球 over 下单开关（over 与 under 对等参与闸门评估）

# === 赛前上下文 ===
AI_MATCH_CONTEXT_ENABLED: bool = True
AI_MATCH_CONTEXT_IN_BATCH: bool = True
AI_MATCH_CONTEXT_TTL_SEC: int = 21600   # 6h
AI_CONTEXT_NONE_TTL: int = 900           # source=none 时短缓存 TTL

# === 风控 ===
AI_STOP_LOSS: float = 500.0             # 日止损绝对金额
AI_TAKE_PROFIT: float = 1000.0          # 日止盈绝对金额
AI_STRATEGY_MAX_BET_AMOUNT: float = 100.0  # 策略单笔最大金额
AI_STRATEGY_MAX_DAILY_BETS: int = 10       # 策略每日注数
AI_MIN_BALANCE: float = 10.0            # 最低可用余额阈值
AI_RETRY_SLEEP_SEC: int = 60            # 引擎异常后重试休眠

# === 下单 ===
BET_RETRY_COUNT: int = 2                # 下单失败重试次数
BET_RETRY_DELAY: float = 3.0            # 重试间隔（秒）
MIN_BET_AMOUNT: float = 100.0           # 站点最低注额
MAX_BET_AMOUNT: float = 100000.0        # 站点最高注额
MAX_DAILY_BETS: int = 50                # 每日投注上限（手动）
AI_DEFAULT_STAKE: float = 100.0         # 默认下注金额

# === 风险评分权重 ===
AI_RISK_LOW_CONF_WEIGHT: float = 0.4    # 低置信度风险权重
AI_RISK_HIGH_ODDS_THRESHOLD: float = 5.0
AI_RISK_HIGH_ODDS_PENALTY: float = 0.3
AI_RISK_MID_ODDS_THRESHOLD: float = 3.0
AI_RISK_MID_ODDS_PENALTY: float = 0.15
AI_RISK_ACTIVE_PENALTY: float = 0.02    # 每笔持仓风险系数
AI_RISK_ACTIVE_CAP: float = 0.1         # 持仓风险上限

# === 数据 ===
DATA_RETENTION_HOURS: int = 24          # 自动清理超期数据

# === 时区（今日投注统计使用 UTC+8 本地午夜）===
# config.py 提供 today_start_utc() / month_start_utc() 工具函数
```

> **注意**：`docker-compose.yml` 中的环境变量会覆盖上述默认值。生产环境的实际运行值以 `.env` 和 compose 环境变量为准。

### 5.2 组合闸门参数

生产策略不再维护分散的旧风险参数表。under/over 和足球/篮球统一由
`app/ai/balanced_gate.py` 与 `app/ai/balanced_profile.py` 管理，最终概率、联赛
等级、盘口/赔率窗口、比赛时间/节奏、剩余得分概率和EV检查一次完成。

生产自动引擎启用「70%–80%滚动目标组合闸门」。70%–80%是策略目标，不是未来
胜率保证；最终校准概率、A-E全闸门和有效赔率仍必须同时通过。

- 足球明确重点：五大联赛、沙特职业/超级联赛、美职联、巴甲、葡超、荷甲、阿超、
  墨超、欧冠，以及其他合规超级/甲级联赛。under最终校准概率≥0.68，over≥0.72。
- 其他合规足球职业赛事：under≥0.70、over≥0.78。预备队、青年、女子、友谊赛、
  乙/丙/丁级和MLS NEXT不会因名称包含“联赛”而获得优先级。
- 篮球明确重点：NBA、ACB、欧洲篮球联赛，under≥0.68、over≥0.72；其他合规
  篮球职业赛事under≥0.70、over≥0.78。
- 足球窗口：under盘口(2.0,4.5)/30'–85'；over盘口(2.25,3.5)/25'–75'。
- 篮球窗口：常规时间25%–87.5%；NBA按48分钟、ACB/欧篮联按40分钟计算节奏。
- 自动赔率统一限制为 `[1.70,2.00)`；重点联赛优先于普通赛事进入分析队列。
- 前端推荐卡和AI日志显示“最终校准概率 / 自动门槛”，该数值是方向分析、基本面、
  盘口、比赛时间、节奏、历史校准和全闸门共用的唯一自动下注概率。

生产只保留组合闸门与平衡策略；测试目录、旧回放和测试依赖不属于生产发布内容。
历史重点联赛样本目前不足，不能把小样本胜率当作未来承诺，需要继续按结算结果滚动评估。

生产平衡档不再叠加旧 A/P/B/C/D/E 的数十条独立规则，而是收敛为五个组合检查：

1. 数据与方向一致性：NowScore、亚洲盘口、比赛节奏形成同向共识且无硬冲突。
2. 最终概率：只使用完成质量约束和历史校准后的唯一概率；用户配置只能加严门槛。
3. 市场窗口：按联赛、球类、方向检查比赛时间、盘口线和赔率。
4. 剩余得分概率：足球使用“实时盘口隐含剩余进球 + 节奏投影”的收缩泊松模型；
   篮球使用NBA 48分钟或FIBA 40分钟口径的剩余得分正态模型。足球under不再使用
   几乎不拦截的线性 `remaining/full × league_average` 余量公式。
5. EV与仓位：最终概率覆盖赔率盈亏平衡后才放行；仓位只依赖最终概率、余额和当日
   亏损，不再让小样本方向胜率或短期连胜连败调整门槛/仓位。

旧细粒度闸门链已从生产代码删除。

### 5.3 环境变量 (`.env`)

从 `.env.example` 复制，关键项：

| 变量 | 说明 |
|------|------|
| `SECRET_KEY` | JWT 密钥（启动时校验弱密钥） |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `DEEPSEEK_BASE_URL` | DeepSeek API 地址 |
| `DEEPSEEK_MODEL` | 模型名称 |
| `BOOKMAKER_BROWSER_GATE_URL` | Browser Gate 地址 |
| `DEFAULT_BET_MODE` | 默认下单模式（manual/active） |
| `CORS_ORIGINS` | 跨域白名单 |
| `POSTGRES_PASSWORD` | 数据库密码 |

---

## 六、部署

### 6.1 一键部署（quick.sh）

`quick.sh` 是唯一的部署入口脚本，用 `--init` 区分首次部署和日常部署。

```bash
# 首次部署 / 全量启动（.env生成 → 数据目录 → 基础镜像 → Browser Gate → 全容器）
bash scripts/quick.sh --init --with-ai

# 日常部署（全量语法检查 → backend/frontend 镜像构建 → 容器重建 → 临时缓存清理 → 就绪检查）
bash scripts/quick.sh

# 跳过构建，仅强制重建容器
bash scripts/quick.sh --no-build

# 同时启动 AI 引擎
bash scripts/quick.sh --with-ai

# 部署后跟踪日志
bash scripts/quick.sh --logs

# 查看状态
bash scripts/quick.sh --status

# 停止所有服务（含 Browser Gate）
bash scripts/quick.sh --stop

# 重启容器（不重建镜像）
bash scripts/quick.sh --restart

# 仅预拉基础镜像
bash scripts/quick.sh --pull
```

### 6.2 停止服务

```bash
# 停止所有服务（含 Browser Gate）
bash scripts/quick.sh --stop

# 停止 + 清空持久化数据（危险）
bash scripts/quick.sh --stop --wipe
```

### 6.3 Browser Gate

在宿主机运行可见 Chromium，后端经 `host.docker.internal:9277` 调用：

```bash
# 自动启动（quick.sh --init 内置）
bash scripts/ensure_browser_gate.sh watch

# 手动管理
bash scripts/ensure_browser_gate.sh start
bash scripts/ensure_browser_gate.sh status
bash scripts/ensure_browser_gate.sh stop
```

### 6.4 数据库迁移

```bash
# 容器启动时自动执行
docker exec ob-backend alembic upgrade head

# 手动执行
docker exec ob-backend alembic upgrade head
docker exec ob-backend alembic revision --autogenerate -m "描述"
```

### 6.5 Dockerfile 结构

```dockerfile
ARG BASE_IMAGE_REGISTRY=docker.m.daocloud.io
FROM ${BASE_IMAGE_REGISTRY}/library/python:3.12-slim AS runtime

# 系统依赖（curl 用于健康检查）
RUN apt-get update && apt-get install -y --no-install-recommends curl

# Python 依赖（BuildKit 缓存）
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

# 业务代码（仅 COPY 必要目录，不含根目录 __init__.py）
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

# 健康检查 + 入口
HEALTHCHECK CMD curl -fsS http://127.0.0.1:8000/health || exit 1
CMD ["/app/scripts/docker_entrypoint_backend.sh"]
```

> **注意**：根目录 `__init__.py` 已在 v2.1.0 清理中删除，Dockerfile 不再 `COPY __init__.py`。
> 项目顶层包是 `app.`，根 `__init__.py` 为残留文件，删除后不影响导入。
>
> **国内基础镜像**：默认使用 `docker.m.daocloud.io` 的官方镜像同步。可在 `.env` 设置
> `BASE_IMAGE_REGISTRY=你的企业镜像仓库`，后端、前端、PostgreSQL 和 Redis 会统一切换；
> `bash scripts/quick.sh --pull` 会预拉同一来源的全部基础镜像。
> `ai-engine` 容器与 `backend` 共享同一镜像，入口命令不同（`ai_betting_engine.py`）。

### 6.6 一键部署脚本内部流程

`scripts/quick.sh` 根据模式执行不同步骤：

**日常部署**（`bash scripts/quick.sh`）：
```
1. Python 语法检查     — `python3 -m compileall -q app scripts` 全量检查，任一失败则终止
2. 重建 Docker 镜像     — `docker compose build --no-cache backend frontend`（--no-build 可跳过；ai-engine 复用 backend 镜像）
3. 清除 Redis 临时缓存 — 删除推荐/上下文/LLM 临时缓存；校准、模式、风险调优状态保留
4. 强制重建容器         — `docker compose up -d --force-recreate backend frontend [ai-engine]`
5. 等待服务就绪         — 轮询 `http://127.0.0.1:8000/ready`，超时 90s
6. 输出容器状态         — docker compose ps + 访问地址
```

**首次部署**（`bash scripts/quick.sh --init --with-ai`）：
```
1. 创建数据目录         — data/postgres data/redis logs + chown
2. 检查 .env 配置       — 缺失则从 .env.example 生成 + 写入 SECRET_KEY
3. 安全令牌检查         — INTERNAL_API_TOKEN 为空或弱令牌则终止
4. 预拉基础镜像         — 缺镜像时从 BASE_IMAGE_REGISTRY 并行拉 postgres/redis/python/nginx/node
5. 启动依赖             — postgres + redis，等待健康（45s）
6. 配置 Browser Gate    — .env 注入 GATE_URL + HEADLESS=0
7. 启动 Browser Gate    — 可见 Chromium + 守护进程，等待健康（90s）
8. 全量语法检查 → 构建 backend/frontend 镜像 → 清临时缓存 → 启动全容器（backend+frontend+[ai-engine]）
9. 等待 API 就绪        — 轮询 /ready，超时 90s
```

> 构建命令不再通过 `grep`/`tail` 管道吞掉退出码；Docker 构建失败会立即终止部署。
> `/docs`、`/redoc`、`/openapi.json` 在生产环境关闭，使用 `/health`（存活）和 `/ready`（数据库/Redis 就绪）探测。

---

## 七、生产验证

### 7.1 生产验证

生产环境不保留测试文件。发布前只执行 Python 编译、Docker 构建、Compose 配置校验、
只读组合闸门合成决策和 `/health`、`/ready` 健康检查。

```bash
python3 -m compileall -q app scripts
docker compose config -q
docker compose build backend frontend
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
```

---

## 八、监控与日志

### 8.1 日志

| 位置 | 说明 |
|------|------|
| `./logs/app.log` | 后端应用日志 |
| `/tmp/browser_gate.log` | Browser Gate 日志 |
| `docker logs ob-backend` | 容器 stdout |
| `docker logs ob-ai-engine` | AI 引擎日志 |

### 8.2 关键日志关键词

| 关键词 | 含义 |
|--------|------|
| `alias auto-approved & effective` | 高置信别名自动写入正式别名 |
| `alias candidate pending review` | 低置信别名仅保留候选，等待复核 |
| `runtime alias index loaded` | 运行时别名索引加载 |
| `runtime alias expanded` | 别名扩展了队名变体 |
| `同场注单已达上限` | 同场投注次数达上限被拦截 |
| `[下单] 站点切换` | 未连接站自动切换 |
| `[AI主循环]` | 引擎主循环日志 |

### 8.3 WebSocket

前端通过 WS 实时接收：赔率更新、比分更新、AI 引擎状态、投注结果通知。多 worker 通过 Redis Pub/Sub 跨 worker 扇出。

---

## 九、安全

| 机制 | 说明 |
|------|------|
| JWT 鉴权 | HS256 + bcrypt(12 rounds)，Access 30min / Refresh 7d |
| 凭据加密 | Fernet 对称加密存储博彩站点密码/会话 Token |
| 弱密钥拦截 | 启动时检测默认 SECRET_KEY 并拒绝启动 |
| 速率限制 | 登录接口限流 |
| TrustedHost | 中间件校验 AllowedHosts |
| 实盘门禁 | 禁止演示 URL 和模拟执行 |
| 中国赛事过滤 | 不分析、不展示、不下注 |
| 虚拟盘过滤 | EAFC/NBA2K 等虚拟赛事一律排除 |

---

## 十、运维指南

### 10.1 运维脚本一览

| 脚本 | 用途 |
|------|------|
| `scripts/quick.sh` | **唯一部署/运维入口**：`--init` 首次部署，无参数日常部署，`--status/--stop/--stop --wipe/--restart` 运维 |
| `scripts/ensure_browser_gate.sh` | Browser Gate 启动/守护/停止 |
| `scripts/clean_prod_env.sh` | 清空业务数据（保留账号） |

### 10.2 日常运维操作

```bash
# 查看容器状态
bash scripts/quick.sh --status

# 部署新代码（改了 Python 文件后）
bash scripts/quick.sh

# 快速重启（不改代码，仅重启容器）
bash scripts/quick.sh --restart

# 停止所有服务
bash scripts/quick.sh --stop

# 跟踪后端日志
docker logs ob-backend --tail 50 -f

# 跟踪 AI 引擎日志
docker logs ob-ai-engine --tail 50 -f

# 进入后端容器调试
docker exec -it ob-backend bash

# 查看数据库
docker exec ob-postgres psql -U ob_user -d ob_sports -c "SELECT * FROM bets ORDER BY id DESC LIMIT 10;"

# 仅清理临时推荐缓存（不要在生产使用 FLUSHALL）
bash scripts/quick.sh --no-build

# 执行数据库迁移
docker exec ob-backend alembic upgrade head
```

### 10.3 常见问题排查

| 症状 | 排查 |
|------|------|
| 持仓页"今日投注"显示 0 | 检查 `today_start_utc()` 是否使用 UTC+8 本地午夜；查看 `docker logs ob-backend` 中 portfolio 查询日志 |
| AI 引擎循环重启 | `docker logs ob-ai-engine` 查看异常；通常是导入错误或配置缺失 |
| 下单失败 | 检查 Browser Gate 健康 `curl http://127.0.0.1:9277/health`；查看 `docker logs ob-backend` 中 `[下单]` 日志 |
| 赔率不更新 | 检查站点连接状态；`docker exec ob-redis redis-cli KEYS "odds:*"` 查看缓存 |
| 数据库连接超时 | 检查 `docker compose ps` 中 postgres 状态；`docker exec ob-postgres pg_isready -U ob_user` |
| Docker 构建报 `__init__.py: not found` | Dockerfile 中 `COPY __init__.py` 已移除（v2.1.0）；若旧缓存仍报错，执行 `docker compose build --no-cache` |
| `quick.sh` 构建失败 | 查看 Docker 原始输出；脚本不会吞掉非零退出码，修复网络/依赖后重新部署 |

### 10.4 数据备份与恢复

```bash
# 备份数据库
docker exec ob-postgres pg_dump -U ob_user ob_sports > backup_$(date +%Y%m%d).sql

# 恢复
cat backup_YYYYMMDD.sql | docker exec -i ob-postgres psql -U ob_user -d ob_sports

# 备份 Redis
docker exec ob-redis redis-cli SAVE
cp data/redis/dump.rdb backup_redis_$(date +%Y%m%d).rdb
```

### 10.5 核心工具模块

| 模块 | 位置 | 说明 |
|------|------|------|
| `app/core/convert.py` | 公共类型转换 | `to_float` / `to_int` / `to_float_or_none`，消除全项目重复定义 |
| `app/config.py` | 时区工具 | `today_start_utc()` / `month_start_utc()` / `LOCAL_TZ`（UTC+8） |
| `app/ai/bet_executor.py` | 站点常量 | `SINGLE_SIDE_PROVIDER_NAMES` / `SINGLE_SIDE_PROVIDER_CODES` / `_notify` 唯一定义源 |

### 10.6 闸门策略架构速查

```
StrategyEngine.evaluate_bet() 生产组合闸门：

  G1 数据/方向共识 → G2 最终概率 → G3 联赛/时间/盘口/赔率窗口
  → G4 剩余得分概率 → G5 EV → 单一仓位公式

  → 仓位计算: max_bet × final_conf_factor × 余额锚定(25%) × 日亏递减
```

生产发布采用编译、镜像构建、只读组合决策与运行时健康检查；仓库不保留测试环境。
