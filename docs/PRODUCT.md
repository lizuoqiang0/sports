# OB Sports Betting 产品文档

> 版本 4.0 | 更新日期 2026-08-05

---

## 1. 产品概述

OB Sports Betting 是一个**双站点（OB / 平博）滚球赛事 AI 智能投注平台**。系统通过 6 个 LLM 模型组成的集成分析引擎，对进行中的足球/篮球赛事进行实时分析，在达到置信度门槛时自动真实下单。

### 核心能力

| 能力 | 说明 |
|------|------|
| 双站点对接 | OB + Pinnacle（平博），支持滚球亚洲盘 |
| AI 集成分析 | 6 模型并行分析，共识投票 + 早退机制，单场 < 30s |
| 自动投注 | 每 10 分钟一轮，最多 2 场不同比赛，每单 10 元真实下单 |
| 每日盈亏 | 以午夜总资产为基线，实时计算盈亏，关联止损止盈 |
| 一键投注 | 人工模式下可对推荐盘口一键下单 |

### 站点体系

| 站点代码 | 名称 | 盘口类型 | 状态 |
|---------|------|---------|------|
| `ob` | OB | 亚洲盘（独赢/让球/大小） | 支持滚球 |
| `pinnacle` | 平博 | 亚洲盘（独赢/让球/大小） | 支持滚球 |

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                       Frontend (React)                       │
│  Dashboard · Matches · Portfolio · AI Panel · Bookmakers    │
└────────────────────────┬────────────────────────────────────┘
                         │ REST API + WebSocket
┌────────────────────────┴────────────────────────────────────┐
│                    Backend (FastAPI)                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────────┐  │
│  │  Auth    │ │  Matches │ │  Bets    │ │   AI Engine    │  │
│  │  (JWT)   │ │  + Odds  │ │  (投注)  │ │ (6模型集成)    │  │
│  └──────────┘ └──────────┘ └──────────┘ └───────────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                    │
│  │Bookmakers│ │Monitoring│ │  Admin   │                    │
│  │ (站点管理)│ │ (投注模式)│ │ (赔率管理)│                    │
│  └──────────┘ └──────────┘ └──────────┘                    │
│                                                               │
│  后台任务: live_poller · balance_poller · context_prefetcher  │
│           cleanup_task                                        │
└──────────┬──────────────┬──────────────┬─────────────────────┘
           │              │              │
     ┌─────┴─────┐  ┌────┴────┐  ┌─────┴─────┐
     │ PostgreSQL │  │  Redis  │  │ OB/平博 API│
     │  (持久化)  │  │ (缓存)  │  │ (真实站点) │
     └───────────┘  └─────────┘  └───────────┘
```

### 后台任务

| 任务 | 间隔 | 功能 |
|------|------|------|
| live_poller | 3-5s | 轮询 OB/平博滚球赛事 + 赔率 |
| balance_poller | 30s | 轮询站点余额 |
| context_prefetcher | 按需 | 赛前上下文预取 |
| cleanup_task | 1h | 清理 24h 前数据 |

> 注：结算轮询（settle_poller）已移除。系统不再自动结算注单，盈亏通过站点余额基线差计算。

---

## 3. 数据模型

### 3.1 核心表

| 表 | 说明 | 关键字段 |
|----|------|---------|
| `users` | 用户 | username, balance, ai_enabled, bet_mode(manual/active) |
| `bets` | 投注记录 | match_id, provider, market, line, odds, stake, status, is_ai_bet, ai_confidence, ai_reasoning, external_bet_id |
| `matches` | 赛事 | home_team, away_team, league, sport, status, home_score, away_score, start_time |
| `odds` | 赔率 | match_id, provider, market, line, value, is_live, opening |
| `bookmaker_accounts` | 站点账户 | code(ob/pinnacle), base_url, session_token, balance, status |
| `ai_configs` | AI 策略配置 | strategy, max_bet_amount, max_daily_bets, min_confidence, stop_loss, take_profit, min_odds, max_odds |
| `transactions` | 交易流水 | type(bet_place/ai_bet), amount, bet_id |

### 3.2 注单状态

```
BetStatus 枚举（仅 2 值）
┌─────────┐
│ SUCCESS │ -> 下单成功
└─────────┘
┌─────────┐
│ FAILED  │ -> 下单失败
└─────────┘
```

无中间态。下单即最终态。无结算状态（settled_win/settled_loss 等已删除）。

### 3.3 交易类型

```
TransactionType 枚举（仅 2 值）
┌───────────┐
│ BET_PLACE │ -> 手动下注
└───────────┘
┌────────┐
│ AI_BET │ -> AI投注
└────────┘
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

> 注：盈亏基于站点余额变化，不依赖注单结算。无 `actual_payout` / `settled_at` / `profit` 字段。

---

## 4. 页面与接口映射

### 4.1 登录页 `/login`

| UI 区域 | API 端点 | 方法 | 参数 | 响应字段 |
|---------|---------|------|------|---------|
| 登录表单 | `/api/v1/auth/login` | POST | `{username, password}` | `access_token, token_type, user{...}` |
| 注册表单 | `/api/v1/auth/register` | POST | `{username, email, password}` | `user{id, username}` |
| 获取当前用户 | `/api/v1/auth/me` | GET | - | `user{id, username, role, balance, ai_enabled, bet_mode}` |

### 4.2 工作台 `/`

| UI 区域 | API 端点 | 方法 | 参数 | 响应字段 |
|---------|---------|------|------|---------|
| 网站余额卡片 | `/api/v1/bookmakers/balances` | GET | - | `sites[{code, name, balance, status}], total_balance` |
| 总资产卡片 | `/api/v1/bets/portfolio` | GET | - | `total_assets` |
| 盈亏卡片 | `/api/v1/bets/portfolio` | GET | - | `daily_pnl, baseline` |
| AI 运行状态徽章 | `/api/v1/ai/status` | GET | - | `running, bet_mode` |
| 进行中赛事列表 | `/api/v1/matches/live/now` | GET | - | `[{id, home_team, away_team, home_score, away_score, league}]` |
| 最近完场赛事列表 | `/api/v1/matches` | GET | `?status=finished&page=1&page_size=5` | `items[{id, home_team, away_team, start_time}]` |
| WebSocket 实时推送 | `ws://host/ws` | WS | - | odds_update / match_status / bet_placed / ai_update |

**轮询策略**：WebSocket 连接时 20s，离线 10s。

### 4.3 赛事列表 `/matches`

| UI 区域 | API 端点 | 方法 | 参数 | 响应字段 |
|---------|---------|------|------|---------|
| 赛事列表 | `/api/v1/matches` | GET | `?status=&sport=&page=&page_size=` | `items[{id, home_team, away_team, league, sport, status, start_time}]` |
| 按运动分组 | `/api/v1/matches/sports/grouped` | GET | - | `{sport: [{...}], ...}` |
| 赛事搜索 | `/api/v1/matches/search` | GET | `?q=` | `[{id, home_team, away_team, league}]` |
| 进行中赛事 | `/api/v1/matches/live/now` | GET | - | `[{id, home_team, away_team, home_score, away_score}]` |
| 联赛列表 | `/api/v1/matches/leagues` | GET | - | `[{league, count}]` |
| WebSocket 推送 | `ws://host/ws` | WS | - | odds_update / match_status |

### 4.4 赛事详情 `/matches/:id`

| UI 区域 | API 端点 | 方法 | 参数 | 响应字段 |
|---------|---------|------|------|---------|
| 赛事基本信息 | `/api/v1/matches/{id}` | GET | - | `id, home_team, away_team, league, status, home_score, away_score, start_time` |
| 赔率面板 | `/api/v1/matches/{id}/odds` | GET | - | `[{provider, market, line, value, is_live, opening}]` |
| 赔率历史 | `/api/v1/odds/history/{id}` | GET | - | `[{timestamp, market, value}]` |
| AI 推荐 | `/api/v1/ai/recommend/{id}` | GET | - | `recommendation{market, line, value, confidence, reasoning}` |
| WebSocket 推送 | `ws://host/ws` | WS | - | odds_update / match_status |

### 4.5 投注记录 `/portfolio`

| UI 区域 | API 端点 | 方法 | 参数 | 响应字段 |
|---------|---------|------|------|---------|
| 今日投注卡片 | `/api/v1/bets/portfolio` | GET | - | `today_bets`（UTC 0点清零） |
| 总投注卡片 | `/api/v1/bets/portfolio` | GET | - | `total_bets`（自然月，月初清零） |
| 投注记录表格 | `/api/v1/bets/history` | GET | `?status=&provider=&page=&page_size=` | `items[{id, match_info, created_at, status, provider_code}]` |

**投注记录表格列**：

| 列名 | 数据来源 | 格式 |
|------|---------|------|
| 投注比赛 | `match_info` | `联赛名 / 主队 vs 客队` |
| 时间 | `created_at` | `MM-dd HH:mm` |
| 状态 | `status` | `成功` / `失败` |
| 站点 | `provider_code` | `OB` / `平博` |

**筛选器**：站点（全部/OB/平博）+ 状态（全部/成功/失败）

> 注：投注记录仅记录 AI/手动投注的订单，不包含赔率、金额、输赢等字段。

### 4.6 AI 投注 `/ai`

| UI 区域 | API 端点 | 方法 | 参数 | 响应字段 |
|---------|---------|------|------|---------|
| 状态横幅 | `/api/v1/ai/status` | GET | - | `running, bet_mode` |
| 策略配置面板 | `/api/v1/ai/config` | GET | - | `strategy, max_bet_amount, max_daily_bets, min_confidence, stop_loss, take_profit, min_odds, max_odds, use_llm_analysis` |
| 更新配置 | `/api/v1/ai/config` | PUT | `{strategy?, max_bet_amount?, ...}` | `ok` |
| 启动 AI 引擎 | `/api/v1/ai/start` | POST | - | `ok, message` |
| 停止 AI 引擎 | `/api/v1/ai/stop` | POST | - | `ok` |
| 预设策略列表 | `/api/v1/ai/strategies` | GET | - | `[{name, label, ...}]` |
| AI 推荐列表 | `/api/v1/ai/recommendations` | GET | `?provider=&sport=` | `recommendations[{...}], count, progress, total, job_status` |
| 开始后台分析 | `/api/v1/ai/recommendations/start` | POST | - | `ok` |
| 停止后台分析 | `/api/v1/ai/recommendations/stop` | POST | - | `ok` |
| 一键投注 | `/api/v1/ai/one-click-bet/{match_id}` | POST | `{market, line, value, stake, provider}` | `bets[], failed[], total_stake, success_count` |
| AI 投注历史 | `/api/v1/ai/history` | GET | `?page=&page_size=` | `items[{...}]` |
| 投注模式切换 | `/api/v1/monitoring/bet-mode` | PUT | `{bet_mode: manual\|active}` | `bet_mode` |
| WebSocket 推送 | `ws://host/ws` | WS | - | ai_update / bet_placed |

**状态横幅文案**：

| 模式 | 文案 |
|------|------|
| 自动 | 自动投注 - 每 10 分钟一轮，最多 2 场不同比赛真实下单 |
| 人工 | 人工投注 - 轮询分析全部滚球，只展示高胜率供手动确认 |
| 未运行 | AI 引擎未运行 |

**轮询策略**：分析开启时 4s 轮询推荐列表。

### 4.7 站点配置 `/bookmakers`

| UI 区域 | API 端点 | 方法 | 参数 | 响应字段 |
|---------|---------|------|------|---------|
| 站点列表 | `/api/v1/bookmakers` | GET | - | `[{id, code, name, base_url, status, balance}]` |
| 站点余额 | `/api/v1/bookmakers/balances` | GET | - | `sites[{code, name, balance, status}], total_balance` |
| 站点目录 | `/api/v1/bookmakers/catalog` | GET | - | `[{code, name, ...}]` |
| 批量更新配置 | `/api/v1/bookmakers` | PUT | `[{code, base_url, session_token}]` | `ok` |
| 验证连接 | `/api/v1/bookmakers/{code}/verify` | POST | - | `ok, balance` |
| 批量验证 | `/api/v1/bookmakers/verify-batch` | POST | - | `{results[]}` |
| 断开连接 | `/api/v1/bookmakers/{code}/disconnect` | POST | - | `ok` |
| 同步赛事 | `/api/v1/bookmakers/sync` | POST | - | `ok` |
| 同步滚球 | `/api/v1/bookmakers/sync-live` | POST | - | `ok` |
| Gate 健康 | `/api/v1/bookmakers/gate-health` | GET | - | `{status}` |
| 赔率对比 | `/api/v1/bookmakers/odds-compare/{match_id}` | GET | - | `{odds[]}` |

---

## 5. 后端 API 完整清单

### 5.1 认证 `/api/v1/auth`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/register` | 注册（生产关闭开放注册） |
| POST | `/login` | 登录获取 JWT |
| POST | `/refresh` | 刷新 Token |
| GET | `/me` | 获取当前用户信息 |
| POST | `/logout` | 登出 |
| POST | `/change-password` | 修改密码 |

### 5.2 赛事 `/api/v1/matches`

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET | `/` | `?status=&sport=&page=&page_size=` | 赛事列表 |
| GET | `/sports/grouped` | - | 按运动分组 |
| GET | `/search` | `?q=` | 搜索 |
| GET | `/live/now` | - | 进行中赛事 |
| GET | `/leagues` | - | 联赛列表 |
| GET | `/{id}` | - | 赛事详情 |

### 5.3 赔率 `/api/v1`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/matches/{id}/odds` | 赛事赔率 |
| GET | `/odds/history/{id}` | 赔率历史 |

### 5.4 投注 `/api/v1/bets`

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

### 5.5 AI 投注 `/api/v1/ai`

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET | `/config` | - | 获取 AI 配置 |
| PUT | `/config` | `{strategy?, max_bet_amount?, ...}` | 更新配置（热更新） |
| POST | `/start` | - | 启动 AI 引擎 |
| POST | `/stop` | - | 停止 AI 引擎 |
| GET | `/status` | - | 引擎状态 |
| GET | `/recommend/{match_id}` | - | 单场推荐 |
| GET | `/recommendations` | `?provider=&sport=` | 批量推荐（读缓存） |
| POST | `/recommendations/start` | - | 开始后台分析 |
| POST | `/recommendations/stop` | - | 停止后台分析 |
| GET | `/strategies` | - | 预设策略列表 |
| GET | `/history` | `?page=&page_size=` | AI 投注历史 |
| POST | `/one-click-bet/{match_id}` | `{market, line, value, stake, provider}` | 一键投注 |

### 5.6 站点管理 `/api/v1/bookmakers`

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

### 5.7 监控 `/api/v1/monitoring`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/overview` | 系统总览 |
| GET | `/bet-mode` | 获取投注模式 |
| PUT | `/bet-mode` | 切换投注模式（manual / active） |

### 5.8 管理 `/api/v1/admin`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/odds/update/{match_id}` | 手动更新赔率（触发 WS 推送） |

### 5.9 其他

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/ready` | 就绪检查（DB + Redis） |
| GET | `/` | 根路径（端点目录） |

---

## 6. WebSocket 实时通信

### 连接

```
ws://host:8000/ws?token={JWT}
```

### 推送消息类型

| 类型 | 触发条件 | 数据内容 |
|------|---------|---------|
| `odds_update` | 赔率变化 | `{match_id, provider, market, line, value}` |
| `match_status` | 比分/状态变化 | `{match_id, home_score, away_score, status}` |
| `bet_placed` | 注单下单成功 | `{bet_id, stake, odds, provider}` |
| `ai_recommend` | AI 推荐更新 | `{recommendation{...}}` |
| `balance_change` | 余额变化 | `{provider, balance}` |
| `subscribed` | 订阅成功 | `{channels[]}` |

---

## 7. AI 引擎

### 7.1 六模型集成

| 模型名 | 模型标识 | 网关 |
|--------|---------|------|
| doubao | doubao-seed-2.0-lite | ark.cn-beijing.volces.com |
| gpt | gpt-5.4 | api.openai.com |
| deepseek | deepseek-v4-pro | api.deepseek.com |
| kimi | kimi-k2.6 | api.moonshot.cn |
| minimax | minimax-m3 | api.minimax.chat |
| glm | glm-5.2 | open.bigmodel.cn |

### 7.2 集成分析流程

```
单场赛事分析:
  ├─ 从模型优先级排序选取 3 个模型（ENSEMBLE_MAX_MODELS=3）
  ├─ 并行调用（超时 15s/模型）
  ├─ 收到 2 票即可早退（ENSEMBLE_QUORUM=2）
  ├─ 共识门槛：≥60% 同意 + ≥2 票
  ├─ 总超时 30s（ENSEMBLE_TIMEOUT_SEC）
  └─ 网络错误快速失败（不重试），仅临时性错误重试（8s 超时）
```

### 7.3 自动投注流程

```
每 10 分钟一轮 (AI_SCAN_INTERVAL_SEC=600):
  ├─ 1. 风控检查
  │   ├─ 计算每日盈亏（当前总资产 - 午夜基线）
  │   ├─ 盈亏 <= -stop_loss -> 触发止损，跳过本轮
  │   └─ 盈亏 >= take_profit -> 触发止盈，跳过本轮
  │
  ├─ 2. 扫描 OB/平博进行中赛事
  │
  ├─ 3. 逐场 AI 集成分析（6模型，每场≤30s）
  │
  ├─ 4. 筛选候选
  │   ├─ 置信度 ≥ min_confidence（默认 75%）
  │   ├─ 赔率 ∈ [min_odds, max_odds]（默认 1.75~10.00）
  │   └─ EV > 0（期望值正）
  │
  ├─ 5. 最多选 2 场不同比赛（AI_MAX_BETS_PER_CYCLE=2）
  │
  ├─ 6. 每单 10 元真实下单
  │   ├─ Redis 分布式锁防双发
  │   ├─ 同场比赛只允许一单
  │   ├─ 站点余额充足检查
  │   ├─ OB 下单后验证 orderNo 真实存在
  │   └─ 写库：Bet(status=SUCCESS) + Transaction(type=AI_BET)
  │
  └─ 7. 600s 后下一轮
```

### 7.4 策略预设

| 策略 | 特点 | 适用场景 |
|------|------|---------|
| conservative | 低风险，高门槛 | 资金保护 |
| balanced | 平衡风险与收益 | 日常运行 |
| aggressive | 高风险，低门槛 | 追求高收益 |

### 7.5 关键配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `AI_SCAN_INTERVAL_SEC` | 600 | 扫描间隔（秒） |
| `AI_MAX_BETS_PER_CYCLE` | 2 | 每轮最多投注数 |
| `AI_MIN_CONFIDENCE` | 0.75 | 置信度门槛 |
| `ENSEMBLE_MAX_MODELS` | 3 | 单场并行模型数 |
| `ENSEMBLE_QUORUM` | 2 | 早退票数 |
| `ENSEMBLE_TIMEOUT_SEC` | 30 | 集成总超时 |
| `LLM_CLIENT_TIMEOUT_SEC` | 15 | 单模型超时 |
| `LLM_RETRY_TIMEOUT_SEC` | 8 | 重试超时 |
| `MAX_DAILY_BETS` | 50 | 每日投注上限 |
| `FORCE_LIVE_MODE` | True | 强制真实站点（禁止模拟） |
| `MIN_BET_AMOUNT` | 100.0 | 最小投注金额 |
| `LLM_CACHE_TTL` | 600 | LLM 缓存时间 |

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
  └─ evaluate_bet() 中再次检查 daily_loss >= stop_loss -> 拒绝投注
```

> 注：系统无自动结算功能。盈亏完全基于站点余额变化，不依赖注单的输赢结算。

---

## 9. 轮询策略汇总

| 页面 | 轮询间隔 | 触发条件 |
|------|---------|---------|
| Dashboard | 20s（WS在线）/ 10s（离线） | 页面可见 |
| 赛事列表 | 15s | 页面可见 |
| 赛事详情 | 10s | 页面可见 |
| 投注记录 | 30s | 页面可见 |
| AI 面板 | 4s（分析开启时） | 分析开启 |
| 站点配置 | 不轮询 | 手动刷新 |

---

## 附录

### A. 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | - | PostgreSQL 连接串 |
| `REDIS_URL` | redis://redis:6379/0 | Redis 连接串 |
| `SECRET_KEY` | - | JWT 密钥（生产必须强密码） |
| `JWT_ALGORITHM` | HS256 | JWT 算法 |
| `FORCE_LIVE_MODE` | True | 强制真实站点投注 |
| `ALLOW_PUBLIC_REGISTER` | False | 是否开放注册 |
| `RUN_BACKGROUND_JOBS` | True | 是否运行后台任务 |

### B. Docker 容器

| 容器 | 端口 | 说明 |
|------|------|------|
| ob-frontend | 80 -> 8090 | Nginx 前端 |
| ob-backend | 8000 | FastAPI 后端 + AI 引擎 |
| ob-postgres | 5432 | PostgreSQL 数据库 |
| ob-redis | 6379 | Redis 缓存 |

### C. 已删除功能（不再存在）

| 功能 | 删除原因 |
|------|---------|
| 串关下注 (place_parlay) | 站点不支持 |
| 撤单 (cancel_bet) | 真实站点不支持本地撤单 |
| 提前兑现 (cash_out) | 真实站点不支持本地兑现 |
| 同步站点注单 (sync_site_bets) | 只记录本地投注 |
| 钱包充值/提现 (wallet.*) | 无钱包体系 |
| 全局停止 (monitoring_stop) | 投注流程不检查该标志 |
| 自动结算 (settle_poller) | 盈亏走站点余额基线，不依赖结算 |
| 站点注单同步 (bet_sync) | 只记录本地投注 |
| locked_balance | 从未使用 |
| actual_payout / settled_at / profit | 结算字段已移除 |
| TransactionType: DEPOSIT/WITHDRAWAL/BET_WIN/BET_REFUND | 无钱包/结算流水 |
| BetStatus: PENDING/WON/LOST/CANCELLED/CASHED_OUT | 简化为 SUCCESS/FAILED |
| SETTLE_POLL_INTERVAL_SEC | settle_poller 已删除 |
| PortfolioSummary / BetDetailResponse / BetHistoryRequest Schema | 未使用，已删除 |
| WSEventType.BET_SETTLED | 改为 BET_PLACED |
| dry_run 参数链 | 已废弃，强制走真实下单 |
| 测试文件 (tests/ + pytest.ini + conftest.py) | 仅保留正式环境代码 |
| init_db.py 演示种子脚本 | 仅开发环境使用 |
| browser_gate /debug/intercept 端点 | 临时调试端点 |
| browser_gate 热重载 (_GATE_HOT_RELOAD) | 开发调试特性 |
| scripts 中 demo_user/Demo1234! 默认凭据 | 改为环境变量传入 |