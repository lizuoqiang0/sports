# OB Sports Betting 产品文档

> 版本 5.0 | 更新日期 2026-08-07

---

## 1. 产品概述

OB Sports Betting 是一个**双站点（OB / 平博）滚球赛事 AI 智能投注平台**。系统通过 6 个 LLM 模型组成的集成分析引擎，对进行中的足球/篮球赛事进行实时分析，在达到置信度门槛时自动真实下单。

### 核心能力

| 能力 | 说明 |
|------|------|
| 双站点对接 | OB + Pinnacle（平博），支持滚球亚洲盘 |
| AI 集成分析 | 6 模型并行分析，共识投票 + 早退机制，单场 ≤ 30s |
| 自动投注 | 每 10 分钟一轮，最多 3 场不同比赛，Kelly 仓位计算 |
| 每日盈亏 | 以午夜总资产为基线，实时计算盈亏，关联止损止盈 |
| 一键投注 | 人工模式下可对推荐盘口一键下单 |
| 捷报数据源 | 比分数据预取，提供赛前上下文增强分析 |

### 站点体系

| 站点代码 | 名称 | 盘口类型 | 状态 |
|---------|------|---------|------|
| `ob` | OB 体育 | 亚洲盘（独赢/让球/大小） | 支持滚球 |
| `pinnacle` | 平博 | 亚洲盘（独赢/让球/大小） | 支持滚球 |

### 球类支持

- **足球**：独赢（1x2）、亚洲让球、亚洲大小
- **篮球**：仅亚洲大小

> 系统自动过滤虚拟盘（EAFC/FIFA/eFootball/NBA 2K 等）与国内赛事（中超/中甲/CBA/港超等）。

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                       Frontend (React)                       │
│  Dashboard · Matches · MatchDetail · Portfolio              │
│  AI Panel · Bookmakers · Logs                               │
└────────────────────────┬────────────────────────────────────┘
                         │ REST API + WebSocket
┌────────────────────────┴────────────────────────────────────┐
│                    Backend (FastAPI + Uvicorn)               │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────────┐  │
│  │  Auth    │ │  Matches │ │  Bets    │ │   AI Engine    │  │
│  │  (JWT)   │ │  + Odds  │ │  (投注)  │ │ (6模型集成)    │  │
│  └──────────┘ └──────────┘ └──────────┘ └───────────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────────┐  │
│  │Bookmakers│ │Monitoring│ │  Admin   │ │  WebSocket     │  │
│  │ (站点管理)│ │ (投注模式)│ │ (赔率/捷报)│ │  (实时推送)    │  │
│  └──────────┘ └──────────┘ └──────────┘ └───────────────┘  │
│                                                               │
│  后台任务: live_poller · balance_poller · context_prefetcher  │
│           nowscore_prefetcher · cleanup_task                  │
│  Worker Leader 选举 + 跨 Worker Redis 扇出                    │
└──────┬─────────────┬──────────────┬──────────────────────────┘
       │             │              │
 ┌─────┴─────┐ ┌────┴────┐ ┌──────┴──────┐
 │ PostgreSQL │ │  Redis  │ │ Browser Gate │
 │   16-alpine│ │7-alpine │ │ (Playwright) │
 │  (持久化)  │ │ (缓存)  │ │ (宿主机运行)  │
 └───────────┘ └─────────┘ └─────────────┘
                      │
              ┌───────┴────────┐
              │ OB / 平博 API   │
              │ (真实站点)      │
              └────────────────┘
```

### 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | React 18 + Vite + TailwindCSS + Nginx 1.27 |
| 后端 | FastAPI 0.115 + Uvicorn（uvloop + httptools，多 worker） |
| 数据库 | PostgreSQL 16-alpine |
| 缓存 | Redis 7-alpine |
| AI | OpenAI 兼容 API（6 模型）+ AsyncOpenAI 客户端 |
| 浏览器 Gate | FastAPI + Playwright（宿主机运行，非容器内） |

### 后台任务

| 任务 | 间隔 | 功能 | 触发条件 |
|------|------|------|---------|
| live_poller | 3-5s | 轮询 OB/平博滚球赛事 + 赔率 | Leader 选举后 |
| balance_poller | 30s | 轮询站点余额 | Leader 选举后 |
| context_prefetcher | 按需 | 赛前上下文预取 | Leader 选举后 |
| nowscore_prefetcher | 按需 | 捷报比分数据预取 | 数据源开启时 |
| cleanup_task | 1h | 清理 24h 前数据 | Leader 选举后 |

> **Worker Leader 选举**：多 worker 部署时，通过 Redis 选出 leader，仅 leader 启动后台轮询任务。AI 引擎状态通过 Redis 跨 worker 共享。

---

## 3. 数据模型

### 3.1 核心表

| 表 | 说明 | 关键字段 |
|----|------|---------|
| `users` | 用户 | username, email, balance, ai_enabled, bet_mode(manual/active), role, is_active, is_verified |
| `bets` | 投注记录 | match_id, provider, market, line, odds, stake, status, is_ai_bet, ai_confidence, ai_reasoning, external_bet_id |
| `matches` | 赛事 | home_team, away_team, league, sport, status, home_score, away_score, start_time, venue, clock, period |
| `odds` | 赔率 | match_id, provider, market(bet_type), line, value(odds_data), spread, total, is_live, valid_from, valid_to |
| `bookmaker_accounts` | 站点账户 | code(ob/pinnacle), base_url, session_token, balance, status, username, password |
| `ai_configs` | AI 策略配置 | strategy, max_bet_amount, max_daily_bets, min_confidence, stop_loss, take_profit, min_odds, max_odds, use_llm_analysis |
| `transactions` | 交易流水 | type(bet_place/ai_bet), amount, bet_id |

### 3.2 注单状态

```
BetStatus 枚举（仅 2 值）
  SUCCESS  -> 下单成功
  FAILED   -> 下单失败
```

下单即最终态，无中间态。无结算状态（settled_win/settled_loss 等已删除）。

### 3.3 交易类型

```
TransactionType 枚举（仅 2 值）
  BET_PLACE  -> 手动下注
  AI_BET     -> AI 投注
```

### 3.4 每日盈亏机制

```
每日 UTC 0:00
  ├─ 记录当前总资产（OB + 平博）作为基线
  │   Redis Key: pnl:baseline:{user_id}:{YYYYMMDD}
  │   TTL = 90000s（25h，自然过期）
  │
  └─ 盈亏 = 当前总资产 - 基线
      ├─ 盈亏 <= -stop_loss -> 触发止损，停止投注
      ├─ 盈亏 >= take_profit -> 触发止盈，停止投注
      └─ 否则继续投注
```

> 盈亏基于站点余额变化，不依赖注单结算。无 `actual_payout` / `settled_at` / `profit` 字段。

---

## 4. 页面与接口映射

### 4.1 登录页 `/login`

| UI 区域 | API 端点 | 方法 | 参数 | 响应字段 |
|---------|---------|------|------|---------|
| 登录表单 | `/api/v1/auth/login` | POST | `{username, password}` | `access_token, refresh_token, token_type, expires_in(=1800), user{...}` |
| 注册表单 | `/api/v1/auth/register` | POST | `{username, email, password}` | `access_token, refresh_token, token_type, expires_in(=1800), user{...}` |

- 登录支持用户名或邮箱
- 注册受 `ALLOW_PUBLIC_REGISTER` 开关控制（生产默认关闭），按 IP 限流 5 次/小时
- 登录按 `IP:username` 限流 20 次/5 分钟
- Token：access（HS256，30 分钟）、refresh（7 天）

### 4.2 工作台 `/`

| UI 区域 | API 端点 | 方法 | 响应字段 |
|---------|---------|------|---------|
| 网站余额卡片 | `/api/v1/bookmakers/balances` | GET | `sites[{code, name, balance, status}], total_balance` |
| 总资产卡片 | `/api/v1/bets/portfolio` | GET | `total_assets` |
| 盈亏卡片 | `/api/v1/bets/portfolio` | GET | `daily_pnl, baseline` |
| AI 运行状态徽章 | `/api/v1/ai/status` | GET | `running, bet_mode` |
| 进行中赛事列表 | `/api/v1/matches/live/now` | GET | `[{id, home_team, away_team, home_score, away_score, league}]` |
| 最近完场赛事列表 | `/api/v1/matches` | GET | `?status=finished&page=1&page_size=5` |
| WebSocket 实时推送 | `ws://host/ws/odds?token={JWT}` | WS | match_update / odds_update / bet_placed / ai_* |

**轮询策略**：WebSocket 连接时 20s，离线 10s。

### 4.3 赛事列表 `/matches`

| UI 区域 | API 端点 | 方法 | 参数 |
|---------|---------|------|------|
| 赛事列表 | `/api/v1/matches` | GET | `?sport=&status=live&provider=&page=1&page_size=200` |
| 赛事搜索 | `/api/v1/matches/search` | GET | `?q=&limit=` |
| 后台预分析 | `/api/v1/ai/recommendations` | GET | `?sport=&limit=16&refresh=false&provider=` |
| WS 断线同步 | `/api/v1/bookmakers/sync-live` | POST | - |

- 站点标签切换：OB / 平博（默认平博）
- 球类标签切换：全部 / 足球 / 篮球
- 搜索 500ms 防抖

**轮询策略**：赛事列表 WS 在线 12s / 离线 5s；滚球同步兜底 WS 在线 15s / 离线 45s。

### 4.4 赛事详情 `/matches/:id`

| UI 区域 | API 端点 | 方法 | 响应字段 |
|---------|---------|------|---------|
| 赛事基本信息 | `/api/v1/matches/{id}` | GET | `id, home_team, away_team, league, sport, status, home_score, away_score, start_time, clock, period, odds[]` |
| 赔率面板 | `/api/v1/matches/{id}/odds` | GET | `[{provider, bet_type, odds_data, spread, total, is_live, valid_from}]` |
| 跨站最优赔率 | `/api/v1/bookmakers/odds-compare/{id}` | GET | `{odds[]}` |
| AI 深度分析 | `/api/v1/ai/recommend/{id}` | GET | `recommendation{market, line, value, confidence, reasoning, risk_level, ev, key_factors[]}` |
| WebSocket 推送 | `ws://host/ws/odds?token={JWT}` | WS | match_update / odds_update |

- 进入页面自动订阅该赛事频道
- "获取 AI 分析"按钮触发单场分析（超时 120s）

**轮询策略**：固定 15s。

### 4.5 投注记录 `/portfolio`

| UI 区域 | API 端点 | 方法 | 参数 |
|---------|---------|------|------|
| 今日/总投注卡片 | `/api/v1/bets/portfolio` | GET | - |
| 投注记录表格 | `/api/v1/bets/history` | GET | `?page=&page_size=20&status=&provider=` |

**投注记录表格列**：

| 列名 | 数据来源 | 格式 |
|------|---------|------|
| 投注比赛 | `match_info` | `联赛名 / 主队 vs 客队` |
| 时间 | `created_at` | `MM-dd HH:mm` |
| 状态 | `status` | `成功` / `失败` |
| 站点 | `provider_code` | `OB` / `平博` |

- 今日投注：每日 UTC 0 点清零
- 总投注：每月末清零
- 每页 20 条，支持站点 + 状态筛选

### 4.6 AI 投注 `/ai`

| UI 区域 | API 端点 | 方法 | 参数 |
|---------|---------|------|------|
| 状态横幅 | `/api/v1/ai/status` | GET | - |
| 策略配置面板 | `/api/v1/ai/config` | GET | - |
| 更新配置 | `/api/v1/ai/config` | PUT | `{strategy?, max_bet_amount?, min_confidence?, stop_loss?, take_profit?, min_odds?, max_odds?, use_llm_analysis?}` |
| 启动 AI 引擎 | `/api/v1/ai/start` | POST | - |
| 停止 AI 引擎 | `/api/v1/ai/stop` | POST | - |
| 预设策略列表 | `/api/v1/ai/strategies` | GET | - |
| AI 推荐列表 | `/api/v1/ai/recommendations` | GET | `?sport=&limit=80&refresh=&provider=` |
| 开始后台分析 | `/api/v1/ai/recommendations/start` | POST | `?sport=&limit=` |
| 停止后台分析 | `/api/v1/ai/recommendations/stop` | POST | `?sport=` |
| 一键投注 | `/api/v1/ai/one-click-bet/{match_id}` | POST | `{stake, markets[]}` |
| 投注模式切换 | `/api/v1/monitoring/bet-mode` | PUT | `{bet_mode: manual\|active}` |
| 捷报数据源开关 | `/api/v1/admin/nowscore/switch` | GET/POST | `?enabled=` |
| 预取进度 | `/api/v1/admin/nowscore/progress` | GET | - |
| 触发预取 | `/api/v1/admin/nowscore/prefetch` | POST | `?sport=all` |
| WebSocket 推送 | `ws://host/ws/odds?token={JWT}` | WS | ai_cycle_complete / ai_recs_ready / ai_bet_placed 等 |

**状态横幅文案**：

| 模式 | 文案 |
|------|------|
| 自动 | 自动投注 - 每 10 分钟一轮，最多 3 场不同比赛真实下单 |
| 人工 | 人工投注 - 轮询分析全部滚球，只展示高胜率供手动确认 |
| 未运行 | AI 引擎未运行 |

**轮询策略**：推荐列表 4s（仅分析开启时）；预取进度 0.5s（数据源开启时）。

### 4.7 站点配置 `/bookmakers`

| UI 区域 | API 端点 | 方法 | 参数 |
|---------|---------|------|------|
| 站点列表 | `/api/v1/bookmakers` | GET | - |
| 站点余额 | `/api/v1/bookmakers/balances` | GET | - |
| 站点目录 | `/api/v1/bookmakers/catalog` | GET | - |
| 批量更新配置 | `/api/v1/bookmakers` | PUT | `{accounts[{code, base_url, username, password, session_token, enabled}]}` |
| 验证连接 | `/api/v1/bookmakers/{code}/verify` | POST | `?manual_venue=false` |
| 批量验证 | `/api/v1/bookmakers/verify-batch` | POST | `{codes[], manual_venue=false}` |
| 断开连接 | `/api/v1/bookmakers/{code}/disconnect` | POST | - |
| 同步赛事 | `/api/v1/bookmakers/sync` | POST | - |
| 同步滚球 | `/api/v1/bookmakers/sync-live` | POST | - |
| Gate 健康 | `/api/v1/bookmakers/gate-health` | GET | - |
| 赔率对比 | `/api/v1/bookmakers/odds-compare/{match_id}` | GET | - |

**轮询策略**：Browser Gate 健康检查 15s。

### 4.8 AI 日志 `/logs`

纯前端页面，无 API 调用。日志数据来自 `useAiLogs()` store，由 AI 面板通过 WebSocket 事件写入。

日志类型：下单成功 / 下单失败 / 风控 / 分析轮次 / 引擎 / 配置 / 分析 / 推荐 / 数据源。

---

## 5. 后端 API 完整清单

### 5.1 根级端点

| 方法 | 路径 | 说明 | 响应 |
|------|------|------|------|
| GET | `/` | 服务信息 | `service, version, environment, endpoints{...}` |
| GET | `/health` | 健康检查（轻量） | `status, service, version, timestamp` |
| GET | `/ready` | 就绪检查（DB + Redis） | `ready: bool, checks{database, redis}` |

> API 文档（`/docs`、`/redoc`、`/openapi.json`）在生产默认关闭，需 `EXPOSE_API_DOCS=true` 显式开启。

### 5.2 认证 `/api/v1/auth`

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| POST | `/register` | `{username(3-50), email, password(8-128)}` | 注册（生产默认关闭） |
| POST | `/login` | `{username, password}` | 登录（支持用户名或邮箱） |
| POST | `/refresh` | `{refresh_token}` | 刷新 Token |
| GET | `/me` | Bearer Token | 获取当前用户信息 |
| POST | `/logout` | Bearer Token | 登出 |
| POST | `/change-password` | Query: `old_password, new_password` | 修改密码（新密码 ≥8 位） |

**UserInfoResponse 字段**：`id, username, email, role, balance, ai_enabled, is_active, is_verified, created_at`

### 5.3 赛事 `/api/v1/matches`

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET | `/` | `?sport=&status=&league=&provider=&page=&page_size=` | 赛事列表（分页） |
| GET | `/sports/grouped` | `?status=` | 按运动分组 |
| GET | `/search` | `?q=&limit=` | 搜索赛事 |
| GET | `/live/now` | - | 进行中赛事（含赔率） |
| GET | `/leagues` | - | 联赛列表 |
| GET | `/{match_id}` | - | 赛事详情（含赔率、时钟、时段） |

- 列表缓存 TTL：live 状态 3s，其他 10s
- 自动过滤虚拟盘与国内赛事
- `provider` 参数：`ob` = OB 体育，`pinnacle` = 平博

### 5.4 赔率 `/api/v1`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/matches/{id}/odds` | 赛事赔率 |
| GET | `/odds/history/{id}` | 赔率历史 |

### 5.5 投注 `/api/v1/bets`

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| POST | `/place` | `{match_id, provider_code, market, line, odds, stake, bet_type}` | 真实下单（禁串关） |
| GET | `/history` | `?status=&provider=&page=&page_size=` | 投注历史 |
| GET | `/portfolio` | - | 持仓概览 |

**portfolio 响应**（5 字段）：

```json
{
  "today_bets": 7,
  "total_bets": 8,
  "total_assets": 105.84,
  "daily_pnl": 0.0,
  "baseline": 105.84
}
```

**history 响应**（7 字段/条）：

```json
{
  "items": [{
    "id": 1,
    "match_id": 123,
    "match_info": "IPBL篮球专业组 / 下诺夫哥罗德 vs 顿河畔罗斯托夫",
    "status": "success",
    "provider": "OB体育",
    "provider_code": "ob",
    "created_at": "2026-08-05T02:30:00+00:00"
  }]
}
```

### 5.6 AI 投注 `/api/v1/ai`

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET | `/config` | - | 获取 AI 配置 |
| PUT | `/config` | `{strategy?, max_bet_amount?, ...}` | 更新配置（热更新） |
| POST | `/start` | - | 启动 AI 引擎 |
| POST | `/stop` | - | 停止 AI 引擎 |
| GET | `/status` | - | 引擎状态 |
| GET | `/recommend/{match_id}` | - | 单场推荐 |
| GET | `/recommendations` | `?sport=&limit=&refresh=&provider=` | 批量推荐（读缓存） |
| POST | `/recommendations/start` | `?sport=&limit=` | 开始后台分析 |
| POST | `/recommendations/stop` | `?sport=` | 停止后台分析 |
| GET | `/strategies` | - | 预设策略列表 |
| GET | `/history` | `?page=&page_size=` | AI 投注历史 |
| POST | `/one-click-bet/{match_id}` | `{stake, markets[]}` | 一键投注 |

### 5.7 站点管理 `/api/v1/bookmakers`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 站点列表 |
| GET | `/balances` | 所有站点余额 |
| GET | `/catalog` | 站点目录 |
| PUT | `/` | 批量更新配置 |
| POST | `/{code}/verify` | 验证连接 |
| POST | `/verify-batch` | 批量验证 |
| POST | `/{code}/disconnect` | 断开连接 |
| POST | `/sync` | 同步赛事 |
| POST | `/sync-live` | 同步滚球 |
| GET | `/gate-health` | Gate 健康检查 |
| GET | `/odds-compare/{match_id}` | 赔率对比 |

### 5.8 监控 `/api/v1/monitoring`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/overview` | 系统总览 |
| GET | `/bet-mode` | 获取投注模式 |
| PUT | `/bet-mode` | 切换投注模式（manual / active） |

### 5.9 管理 `/api/v1/admin`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/odds/update/{match_id}` | 手动更新赔率（触发 WS 推送） |
| GET | `/nowscore/switch` | 获取捷报数据源开关状态 |
| POST | `/nowscore/switch` | 设置捷报数据源开关 |
| POST | `/nowscore/prefetch` | 触发捷报数据预取 |
| GET | `/nowscore/progress` | 获取预取进度 |

---

## 6. WebSocket 实时通信

### 连接

```
ws://host:8000/ws/odds?token={JWT}
```

### 连接行为

- 连接成功后发送 `ping`，订阅 `odds:live` 频道
- 断线 5s 自动重连
- 支持按赛事订阅：`{action: 'subscribe', match_id}`
- 支持按球类订阅：`{action: 'subscribe_sport', sport}`

### 推送消息类型

| 类型 | 触发条件 | 数据内容 |
|------|---------|---------|
| `match_update` | 比分/状态变化 | `{match_id, home_score, away_score, status, clock, period, odds}` |
| `odds_update` | 赔率变化 | `{match_id, provider, market, line, value}` |
| `match_status` | 比赛状态变化 | `{match_id, home_score, away_score, status}` |
| `bet_placed` | 注单下单成功 | `{bet_id, stake, odds, provider}` |
| `ai_recommend` | AI 推荐更新 | `{recommendation{...}}` |
| `ai_cycle_complete` | AI 完成一轮分析 | `{...}` |
| `ai_recs_ready` | 推荐就绪 | `{...}` |
| `ai_bet_placed` | AI 真实下单成功 | `{...}` |
| `ai_bet_failed` | AI 下单失败 | `{...}` |
| `ai_risk_stop` | 风控触发 | `{reason}` |
| `ai_config_updated` | 配置已更新 | `{...}` |
| `snapshot` | 初始快照 | `{...}` |

---

## 7. AI 引擎

### 7.1 六模型集成

| 短名 | 模型标识 | API 网关 | 配置项前缀 |
|------|---------|---------|-----------|
| doubao | doubao-seed-2.0-lite | ark.cn-beijing.volces.com | `DOUBAO_*` |
| gpt | gpt-5.4 | juaiapi.com | `GPT_*` |
| deepseek | deepseek-v4-pro | ark.cn-beijing.volces.com | `DEEPSEEK_*` |
| kimi | kimi-k2.6 | ark.cn-beijing.volces.com | `KIMI_*` |
| minimax | minimax-m3 | ark.cn-beijing.volces.com | `MINIMAX_*` |
| glm | glm-5.2 | ark.cn-beijing.volces.com | `GLM_*` |

所有模型使用 OpenAI 兼容 API（`AsyncOpenAI` 客户端），回退 key 为 `NEWAPI_API_KEY`，回退 base_url 为 `https://www.juaiapi.com/v1`。

### 7.2 集成分析流程

```
单场赛事分析:
  ├─ 按 ENSEMBLE_MODEL_ORDER 选取前 ENSEMBLE_MAX_MODELS(=3) 个模型
  │   默认顺序: deepseek, doubao, gpt, minimax, glm, kimi
  ├─ 并行调用（单模型超时 LLM_CLIENT_TIMEOUT_SEC=15s）
  ├─ 收到 ENSEMBLE_QUORUM(=2) 票即可早退，取消慢模型
  ├─ 共识门槛：
  │   ├─ 同意票数 ≥ ENSEMBLE_MIN_VOTES(=2)
  │   └─ 同意占比 ≥ ENSEMBLE_MIN_CONSENSUS(=0.67)
  ├─ 单模型可用时：置信度 ≥ 0.70 方可放行
  ├─ 总超时 ENSEMBLE_TIMEOUT_SEC(=30s)
  ├─ 模型动态权重：从 Redis 读取历史命中率（60s 缓存）
  └─ 缓存 TTL: LLM_CACHE_TTL(=600s)
```

**有效预测值**：`over`（大）、`under`（小）、`home`（主）、`away`（客）、`draw`（平）

**有效投注类型**：`total`（大小）、`moneyline`（独赢）、`spread`（让球）

**EV 计算**：使用去 vig（去除博彩公司 margin）后的公平赔率计算，`EV = confidence × fair_odds - 1`

### 7.3 自动投注流程

```
每 10 分钟一轮 (AI_SCAN_INTERVAL_SEC=600):
  ├─ 1. 风控检查
  │   ├─ 计算每日盈亏（当前总资产 - 午夜基线）
  │   ├─ 盈亏 <= -stop_loss -> 触发止损，跳过本轮
  │   ├─ 盈亏 >= take_profit -> 触发止盈，跳过本轮
  │   └─ 每日投注笔数达上限 -> 跳过
  │
  ├─ 2. 扫描 OB/平博进行中赛事（跳过已下注/虚拟/中国赛事）
  │   每轮扫描上限: AI_LIVE_SCAN_LIMIT(=120) 场
  │
  ├─ 3. 逐场 AI 集成分析（6模型选3，每场≤30s）
  │   批量并发: ENSEMBLE_CONCURRENCY(=8)
  │
  ├─ 4. 策略门禁筛选
  │   ├─ 共识达成（consensus_reached=True）
  │   ├─ 置信度 ≥ min_confidence（默认 75%）
  │   ├─ 赔率 ∈ [min_odds, max_odds]（默认 1.1~10.0）
  │   ├─ EV ≥ 0.02
  │   └─ 球队排除/球类偏好检查
  │
  ├─ 5. Kelly 仓位计算
  │   ├─ kelly_ratio = max(0.30, min(1.0, kelly × 4.0))
  │   └─ 仓位 ∈ [1, max_bet_amount]
  │
  ├─ 6. 最多选 3 场不同比赛 (AI_MAX_BETS_PER_CYCLE=3)
  │   OB/平博同场只允许一单
  │
  ├─ 7. 真实下单
  │   ├─ Redis 分布式锁防双发
  │   ├─ 赔率逆向变动检测（>0.05 上升 -> 放弃）
  │   ├─ 异常高赔率校验（>3.5 拒绝）
  │   ├─ 站点余额充足检查
  │   ├─ OB 下单后验证 orderNo 真实存在
  │   ├─ 补单重试（BET_RETRY_COUNT=2）
  │   └─ 写库：Bet(status=SUCCESS) + Transaction(type=AI_BET)
  │
  └─ 8. WebSocket 推送 ai_bet_placed / ai_bet_failed / ai_cycle_complete
```

### 7.4 策略预设

| 参数 | conservative | balanced | aggressive |
|------|-------------|----------|------------|
| max_bet_percentage | 2% | 5% | 10% |
| max_daily_loss_percentage | 10% | 20% | 35% |
| max_concurrent_bets | 5 | 10 | 20 |
| min_confidence | 0.80 | 0.75 | 0.70 |
| kelly_fraction_cap | 0.10 | 0.25 | 0.30 |

### 7.5 关键配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `AI_SCAN_INTERVAL_SEC` | 600 | 扫描间隔（秒），默认 10 分钟，范围 [60, ∞) |
| `AI_MAX_BETS_PER_CYCLE` | 3 | 每轮最多投注数 |
| `AI_MIN_CONFIDENCE` | 0.75 | 置信度门槛 |
| `AI_LIVE_SCAN_LIMIT` | 120 | 每轮扫描赛事上限 |
| `AI_RECS_LIMIT` | 80 | 推荐分析上限 |
| `ENSEMBLE_MAX_MODELS` | 3 | 单场并行模型数 |
| `ENSEMBLE_QUORUM` | 2 | 早退票数 |
| `ENSEMBLE_MIN_CONSENSUS` | 0.67 | 共识同意占比门槛 |
| `ENSEMBLE_MIN_VOTES` | 2 | 最少同意票数 |
| `ENSEMBLE_CONCURRENCY` | 8 | 批量分析并发 |
| `ENSEMBLE_TIMEOUT_SEC` | 30 | 集成总超时 |
| `ENSEMBLE_MODEL_ORDER` | deepseek,doubao,gpt,minimax,glm,kimi | 模型优先顺序 |
| `LLM_CLIENT_TIMEOUT_SEC` | 15 | 单模型超时 |
| `LLM_CACHE_TTL` | 600 | LLM 缓存时间（10 分钟） |
| `LLM_TEMPERATURE` | 0.2 | LLM 采样温度 |
| `LLM_MAX_TOKENS` | 2048 | LLM 最大输出 token |
| `AI_STRATEGY_MAX_BET_AMOUNT` | 100.0 | 单笔最大金额 |
| `AI_STRATEGY_MAX_DAILY_BETS` | 10 | 每日投注上限 |
| `AI_STOP_LOSS` | 500.0 | 日止损金额 |
| `AI_TAKE_PROFIT` | 1000.0 | 日止盈金额 |
| `AI_MAX_ODDS` | 10.0 | 最大赔率 |
| `AI_MIN_ODDS` | 1.1 | 最小赔率 |
| `FORCE_LIVE_MODE` | True | 强制真实站点（禁止模拟） |
| `MIN_BET_AMOUNT` | 100.0 | 最小投注金额 |

---

## 8. 投注流程

### 8.1 真实下单流程

```
用户/AI 发起投注:
  ├─ 1. 速率限制（5次/分钟）
  ├─ 2. 串关检查（bet_type=parlay -> 拒绝）
  ├─ 3. 同场检查（OB/平博 sibling 只允许一单）
  ├─ 4. Redis 分布式锁（防双发）
  ├─ 5. 站点验证
  │   ├─ provider_code 存在
  │   ├─ base_url 真实
  │   ├─ status = connected
  │   └─ balance >= stake
  ├─ 6. 调用站点真实下单 API
  ├─ 7. OB 下单后验证 orderNo
  ├─ 8. 写库
  │   ├─ Bet(status=SUCCESS, external_bet_id=orderNo)
  │   └─ Transaction(type=BET_PLACE 或 AI_BET)
  └─ 9. 更新站点余额
```

### 8.2 每日盈亏关联

```
盈亏 = 当前总资产(OB + 平博) - 午夜基线

AI 引擎每轮检查:
  ├─ 盈亏 <= -stop_loss  -> 止损，停止投注
  ├─ 盈亏 >= take_profit -> 止盈，停止投注
  └─ evaluate_bet() 中再次检查 -> 拒绝投注
```

> 系统无自动结算功能。盈亏完全基于站点余额变化，不依赖注单的输赢结算。

---

## 9. 轮询策略汇总

| 页面 | 轮询内容 | WS 在线 | WS 离线 | 条件 |
|------|---------|---------|---------|------|
| Dashboard | 全量加载 | 20s | 10s | 始终 |
| 赛事列表 | 赛事静默刷新 | 12s | 5s | 搜索输入时暂停 |
| 赛事列表 | 滚球同步兜底 | 15s | 45s | 始终 |
| 赛事详情 | 详情+赔率+对比 | 15s | 15s | 始终 |
| AI 面板 | 推荐列表 | 4s | 4s | 仅分析开启时 |
| AI 面板 | 预取进度 | 0.5s | 0.5s | 数据源开启时 |
| 站点配置 | Gate 健康检查 | 15s | 15s | 始终 |

所有轮询均具备**页面可见性感知**：标签页隐藏时跳过执行，重新可见时立即执行一次。

---

## 10. Browser Gate

Browser Gate 是宿主机上运行的 Playwright Chromium 服务，用于 OB/平博站点的浏览器登录与会话管理。

- **运行位置**：宿主机（Mac/GUI 弹出可见 Chromium），非容器内
- **端口**：`9277`
- **后端调用**：`http://host.docker.internal:9277`
- **部署脚本**：`scripts/ensure_browser_gate.sh`
- **健康检查**：`/health` 端点返回 `"runtime":"host"`

> 生产环境使用非 headless 模式（`BOOKMAKER_BROWSER_HEADLESS=0`），弹出可见 Chromium 窗口。

---

## 11. 部署架构

### 11.1 Docker 服务

| 容器 | 镜像 | 端口 | 资源限制 | 说明 |
|------|------|------|---------|------|
| ob-postgres | postgres:16-alpine | 5432 | CPU 2.5 / Mem 2g | PostgreSQL 数据库 |
| ob-redis | redis:7-alpine | 6379 | CPU 1.5 / Mem 768m | Redis 缓存 |
| ob-backend | python:3.12-slim | 8000 | CPU 4.0 / Mem 3g | FastAPI 后端 + AI 引擎 |
| ob-frontend | nginx:1.27-alpine | 3000 | CPU 1.0 / Mem 256m | Nginx 前端 |
| ob-ai-engine | (复用后端镜像) | - | CPU 1.5 / Mem 1g | AI 引擎独立进程（可选，profile ai） |

- 数据库/Redis 端口绑定 `127.0.0.1`（仅本机访问）
- 前端端口默认 `3000`，生产覆盖文件也绑定 `127.0.0.1`（需反向代理 HTTPS）
- 日志驱动：JSON 文件，max-size 20m，max-file 3

### 11.2 开发 vs 生产

| 配置项 | 开发 (docker-compose.yml) | 生产 (+ docker-compose.prod.yml) |
|--------|--------------------------|----------------------------------|
| 代码挂载 | 热挂载 `app/` 和 `scripts/` | 使用镜像内构建产物 |
| ENVIRONMENT | production | production |
| LOG_LEVEL | INFO | WARNING |
| DB_POOL_SIZE | 32 | 8 |
| DB_MAX_OVERFLOW | 64 | 12 |
| UVICORN_WORKERS | 0（自动） | 4 |
| UVICORN_LIMIT_CONCURRENCY | 400 | 200 |
| 前端端口绑定 | 外部可访问 | 127.0.0.1（需反向代理） |
| 健康检查端点 | /health | /ready |

### 11.3 后端启动参数

```
uvicorn app.main:app
  --host 0.0.0.0 --port 8000
  --workers {自动: min(4, max(2, nproc-2))}
  --loop uvloop --http httptools
  --limit-concurrency 200
  --backlog 2048 --timeout-keep-alive 5
  --proxy-headers --forwarded-allow-ips="127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
  --no-access-log
```

### 11.4 CI/CD

| Job | 触发 | 步骤 |
|-----|------|------|
| frontend-build | push/PR to main | Node 20 + npm ci + npm run build |
| backend-image-build | push/PR to main | Docker build + 冒烟测试（import app.main） |

---

## 附录

### A. 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `postgresql+asyncpg://ob_user:...@postgres:5432/ob_sports` | PostgreSQL 连接串 |
| `REDIS_URL` | `redis://redis:6379/0` | Redis 连接串 |
| `SECRET_KEY` | - | JWT 密钥（生产必须强密码，弱密钥拦截） |
| `INTERNAL_API_TOKEN` | - | 内部 API Token（生产必填） |
| `WEAK_SECRET_BLOCK_IN_PROD` | true | 生产弱密钥拦截 |
| `ALGORITHM` | HS256 | JWT 算法 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 30 | Access Token 有效期 |
| `REFRESH_TOKEN_EXPIRE_DAYS` | 7 | Refresh Token 有效期 |
| `BCRYPT_ROUNDS` | 12 | bcrypt 加密轮数 |
| `ENVIRONMENT` | production | 运行环境 |
| `DEBUG` | false | 调试模式 |
| `LOG_LEVEL` | INFO/WARNING | 日志级别 |
| `FORCE_LIVE_MODE` | true | 强制真实站点投注 |
| `ALLOW_PUBLIC_REGISTER` | false | 是否开放注册 |
| `EXPOSE_API_DOCS` | false | 是否暴露 API 文档 |
| `RUN_BACKGROUND_JOBS` | true | 是否运行后台任务 |
| `DEFAULT_BET_MODE` | manual | 默认投注模式 |
| `MIN_BET_AMOUNT` | 100 | 最小投注金额 |
| `CORS_ORIGINS` | `http://localhost:3000,...` | CORS 允许源 |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,backend,ob-backend` | 信任主机 |
| `DB_POOL_SIZE` | 32/8 | 数据库连接池大小 |
| `DB_MAX_OVERFLOW` | 64/12 | 连接池溢出上限 |
| `UVICORN_WORKERS` | 0(自动)/4 | Uvicorn worker 数 |
| `UVICORN_LIMIT_CONCURRENCY` | 400/200 | 并发连接上限 |
| `BOOKMAKER_BROWSER_GATE_URL` | `http://host.docker.internal:9277` | Browser Gate 地址 |
| `BOOKMAKER_BROWSER_HEADLESS` | 0 | 浏览器是否无头 |
| `NOWSCORE_PROXY_URL` | `http://host.docker.internal:7897` | 捷报代理地址 |

### B. 前端 Nginx 路由规则

| 路径 | 处理 |
|------|------|
| `/assets/` | 静态资源长缓存 7 天 |
| `/api/` | 反向代理到后端（读写超时 300s） |
| `/ws` | WebSocket 反向代理（超时 3600s） |
| `/health` `/ready` | 代理到后端 |
| `^/(docs\|redoc\|openapi.json)` | 返回 404（生产默认不暴露） |
| `/` | SPA fallback `try_files $uri $uri/ /index.html` |

### C. Python 依赖

| 包 | 版本 | 说明 |
|----|------|------|
| fastapi | 0.115.0 | Web 框架 |
| uvicorn[standard] | 0.30.6 | ASGI 服务器 |
| sqlalchemy | 2.0.35 | ORM |
| asyncpg | 0.30.0 | PostgreSQL 异步驱动 |
| pydantic | 2.9.0 | 数据校验 |
| python-jose[cryptography] | 3.3.0 | JWT |
| bcrypt | 4.2.1 | 密码哈希 |
| redis | 5.1.0 | Redis 客户端 |
| httpx | 0.27.2 | HTTP 客户端 |
| openai | 1.50.0 | LLM 客户端 |
| APScheduler | 3.10.4 | 定时任务 |
| loguru | 0.7.2 | 日志 |
| playwright | 1.48.0 | 浏览器自动化 |

### D. 已删除功能（不再存在）

| 功能 | 删除原因 |
|------|---------|
| 串关下注 (place_parlay) | 站点不支持 |
| 撤单 (cancel_bet) | 真实站点不支持本地撤单 |
| 提前兑现 (cash_out) | 真实站点不支持本地兑现 |
| 自动结算 (settle_poller) | 盈亏走站点余额基线，不依赖结算 |
| 钱包充值/提现 (wallet.*) | 无钱包体系 |
| BetStatus: PENDING/WON/LOST/CANCELLED/CASHED_OUT | 简化为 SUCCESS/FAILED |
| TransactionType: DEPOSIT/WITHDRAWAL/BET_WIN/BET_REFUND | 无钱包/结算流水 |
| dry_run 参数链 | 已废弃，强制走真实下单 |
| 测试文件 (tests/ + pytest.ini) | 仅保留正式环境代码 |
