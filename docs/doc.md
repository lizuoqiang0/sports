# OB-Sports AI 投注系统技术文档

## 目录

1. [系统架构总览](#1-系统架构总览)
2. [策略引擎 (StrategyEngine)](#2-策略引擎-strategyengine)
3. [策略阀门 (Gates)](#3-策略阀门-gates)
4. [GPT 分析引擎 (MatchAnalyzer)](#4-gpt-分析引擎-matchanalyzer)
5. [候选过滤 (analysis_filters)](#5-候选过滤-analysis_filters)
6. [自动投注引擎 (AIBettingEngine)](#6-自动投注引擎-aibettingengine)
7. [API 接口](#7-api-接口)
8. [数据模型](#8-数据模型)
9. [盘口解析 (OB/平博)](#9-盘口解析-ob平博)
10. [实时监控 (live_monitor)](#10-实时监控-live_monitor)
11. [配置项参考](#11-配置项参考)
12. [投注决策全链路](#12-投注决策全链路)
13. [测试体系](#13-测试体系)

---

## 1. 系统架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                        守护进程                                      │
│  scripts/ai_betting_engine.py                                       │
│  └── AIBettingEngine._main_loop()  每 120s 循环                      │
│      ├── Phase 1: 扫描候选 (_scan_candidates)                        │
│      ├── Phase 2: GPT 分析 (MatchAnalyzer.analyze_match)             │
│      └── Phase 3: 策略评估 + 下单 (StrategyEngine + _execute_bet)     │
├─────────────────────────────────────────────────────────────────────┤
│                         分析层                                       │
│  app/ai/analyzer.py        GPT 单模型分析 + 信号复核                   │
│  app/ai/analysis_filters.py  候选过滤（黑名单/赔率区间/即将结束）        │
├─────────────────────────────────────────────────────────────────────┤
│                         策略层                                       │
│  app/ai/strategy.py        六阶段闸门链 (A0-E2) + 仓位计算            │
│  app/ai/strategy_gates.py  日风控 + 下单前阀门校验                      │
├─────────────────────────────────────────────────────────────────────┤
│                         数据层                                       │
│  app/models/user.py        ORM: Match/Odds/Bet/AIConfig/User         │
│  app/config.py             Settings (50+ AI 配置项)                  │
├─────────────────────────────────────────────────────────────────────┤
│                         盘口解析层                                    │
│  app/services/bookmakers/venue_live.py  OB DOM 解析（全场+半场）       │
│  app/services/bookmakers/plugins/ob/odds.py  OB API 解析（全场+半场）  │
│  app/services/bookmakers/plugins/pinnacle/odds.py  平博 API（全场+半场）│
├─────────────────────────────────────────────────────────────────────┤
│                         API 层                                       │
│  app/api/ai_bets.py        /api/v1/ai/* (推荐/配置/一键投注)          │
├─────────────────────────────────────────────────────────────────────┤
│                         监控层                                       │
│  scripts/live_monitor.py   实时监控 + WebSocket 推送                   │
├─────────────────────────────────────────────────────────────────────┤
│                         测试层                                       │
│  tests/ai/                 119 项自动化测试（6 个文件）                │
└─────────────────────────────────────────────────────────────────────┘
```

### 核心文件清单

| 文件 | 职责 |
|------|------|
| `app/ai/strategy.py` | 策略引擎：六阶段闸门评估 (A0-E2) + 动态仓位计算 |
| `app/ai/strategy_gates.py` | 策略阀门：日止损/止盈/注数限制 + 球队排除 + 下单前校验 |
| `app/ai/analyzer.py` | GPT 分析引擎：prompt 构建 + 11 种信号复核 + 缓存 |
| `app/ai/analysis_filters.py` | 候选过滤：中国赛事/联赛黑名单/赔率区间（仅 TOTAL） |
| `app/ai/auto_better.py` | 自动投注引擎主调度器 + match 级锁 + 引擎锁 |
| `app/api/ai_bets.py` | AI 投注 API 端点 |
| `app/models/user.py` | 数据模型定义（含 BetType 枚举） |
| `app/services/bookmakers/venue_live.py` | OB 盘口 DOM 解析（全场+上下半场大小球） |
| `app/services/bookmakers/plugins/ob/odds.py` | OB API 盘口解析（全场+上下半场大小球） |
| `app/services/bookmakers/plugins/pinnacle/odds.py` | 平博 API 盘口解析（全场+上下半场大小球） |
| `app/config.py` | 全局配置（Settings） |
| `scripts/live_monitor.py` | 实时监控 + WebSocket AI 日志推送 |
| `tests/ai/` | 自动化测试套件（119 项） |

---

## 2. 策略引擎 (StrategyEngine)

**文件**: `app/ai/strategy.py`

### 2.1 核心数据结构

#### StrategyConfig - 策略配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | str | "simple" | 策略名称 |
| `max_bet_amount` | float | 100.0 | 单笔最大金额 |
| `max_daily_bets` | int | 10 | 每日注数上限 |
| `stop_loss` | float | 500.0 | 日止损金额 |
| `take_profit` | float | 1000.0 | 日止盈金额 |
| `use_llm_analysis` | bool | True | 是否使用 LLM 分析 |
| `min_confidence` | float | 0.47 | 最低置信度 |
| `min_odds` | float | 1.65 | 最低赔率 |
| `max_odds` | float | 5.0 | 最高赔率 |

#### BetDecision - 投注决策

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `match_id` | int | (必填) | 比赛ID |
| `selection` | str | (必填) | 投注方向（仅 "under"） |
| `confidence` | float | (必填) | 置信度 0-1 |
| `suggested_stake` | Decimal | (必填) | 建议仓位 |
| `reasoning` | str | (必填) | 决策理由 |
| `risk_score` | float | (必填) | 风险评分（参与仓位计算） |
| `should_bet` | bool | (必填) | 是否投注 |
| `bet_type` | str | "total" | 盘口类型 |
| `provider_code` | str | "" | 站点代码 |
| `odds` | float | 0.0 | 赔率 |
| `line` | float | None | 盘口线 |
| `sport` | str | "" | 球类 |
| `period` | str | "" | 节次 |
| `clock` | str | "" | 比赛时间 |
| `home_score` | int | 0 | 主队比分 |
| `away_score` | int | 0 | 客队比分 |

### 2.2 运动类型风控参数 (SPORT_RISK)

```python
SPORT_RISK = {
    "basketball": {
        "under_min_conf": 0.58,            # 篮球 under 最低置信度（有基本面）
        "under_min_conf_no_fund": 0.62,    # 无基本面时加严
        "under_min_line": 120.0,           # 篮球小球盘线区间下限
        "under_max_line": 208.0,           # 篮球小球盘线区间上限
        "under_min_played_mins": 14.0,     # 首节早段样本太小
        "under_late_block_mins": 44.0,     # 末节最后4分钟波动大
        "margin_min_mins": 24.0,           # 中盘后才看余量
        "margin_full_mins": 48.0,          # 全场48分钟
        "margin_avg_goals": None,          # None=按盘口线折算
        "margin_factor": 1.45,             # 余量因子
        "late_margin_floor": 4.0,          # 末节余量底线
        "ev_conf_edge": 0.04,              # EV 安全垫
    },
    "football": {
        "under_min_conf": 0.55,            # 足球 under 最低置信度
        "under_min_conf_no_fund": 0.58,    # 无基本面加严
        "under_min_line": 2.0,             # 足球小球盘线区间下限
        "under_max_line": 6.5,             # 足球小球盘线区间上限
        "under_min_played_mins": 20.0,     # 开赛20分钟后才分析
        "under_late_block_mins": 90.0,     # 全场90分钟
        "margin_min_mins": 40.0,           # 中盘后才看余量
        "margin_full_mins": 90.0,          # 全场90分钟
        "margin_avg_goals": 2.75,          # 联赛均值
        "margin_factor": 1.3,
        "late_margin_floor": 0.5,
        "ev_conf_edge": 0.0,               # 足球无额外 edge
    },
}
# default = 深拷贝 football
```

### 2.3 联赛黑名单

```python
LEAGUE_BLACKLIST_KEYWORDS = (
    "u19", "u21", "u18", "u20", "u17", "u16",
    "青年", "青少年", "后备队", "女子", "(女)", "women", "女篮",
    "友谊赛", "表演赛",
)
```

### 2.4 六阶段闸门链 (evaluate_bet)

**调用**: `StrategyEngine.evaluate_bet(match_info, analysis, user_balance, daily_loss, active_bets_count) -> BetDecision`

```
阶段 A: 信号有效性
  ├── A0 玩法白名单：bet_type ∈ {total, first_half_total, second_half_total}
  ├── A1 方向合法：prediction 必须为 "under"
  ├── A2 模型共识：consensus_reached=True，reasoning 不含 [不投注]/不可下单
  ├── A3 置信度达标：≥ under_min_conf（按方向基本面分级 + 胜率自适应）
  │   ├── 有基本面：under_min_conf（篮球0.58/足球0.55）
  │   ├── 无基本面：max(base_req, under_min_conf_no_fund)（篮球0.62/足球0.58）
  │   └── 胜率自适应：近7天≥5单时，胜率<0.35 加+0.10，<0.45 加+0.05
  └── A4 篮球三重门禁：triad_ready（初指+实时盘口+基本面齐备）
      ├── triad_ready=False -> 拒绝
      ├── verdict=="conflict" 或 conflict_points>1 -> 拒绝
      └── market_points<3 或 fundamental_points<3 -> 拒绝

阶段 B: 结构性风控
  ├── B1 小球盘线区间 + 比赛时段
  │   ├── 足球：played_mins < 20 -> 拒绝
  │   ├── 足球：total_line <= 2.0 -> 拒绝
  │   ├── 篮球：played_mins < 14 -> 拒绝
  │   ├── 篮球：total_line >= 208 -> 拒绝
  │   └── 篮球：played_mins >= 44 -> 拒绝（末节波动大）
  ├── B2 联赛黑名单：league_is_blacklisted()
  └── B3 高赔率 under 风险：odds >= 2.0 -> 拒绝

阶段 C: 市场一致性
  ├── C1 盘口变化方向与预测相反 -> 拒绝
  └── C2 篮球 under 要求市场同步支持（mkt_support=="under"）

阶段 D: 滚球余量
  ├── D1 中后段余量不足覆盖剩余期望 -> 拒绝
  │   └── margin < expected_remaining × margin_factor
  └── D2 补时/加时余量 <= late_margin_floor -> 拒绝

阶段 E: 赔率有效性
  ├── E1 区间检查：min_odds ≤ odds ≤ max_odds
  └── E2 EV 盈亏平衡：confidence ≥ 1/odds + ev_conf_edge
```

### 2.5 动态仓位计算

```
suggested_stake = max_bet_amount
    × confidence_scale(confidence)        # 置信度缩放（conf≥0.65->0.90，conf≤阈值->0.5）
    × risk_discount(risk_score)           # 风险折扣（1 - risk×0.30）
    × site_factor(provider)               # 站点因子
    × balance_anchor(user_balance)        # 余额锚定（min 截断，不超过余额25%）
    × daily_loss_decay(daily_loss)         # 日亏递减（taper = 1 - 0.5×loss_ratio）
```

余额锚定修复：`bal_cap < min_stake` 时用 `bal_cap` 而非 `min_stake` 兜底，防止击穿 25% 锚定。

### 2.6 风险评分 (_calc_risk_score)

```python
def _calc_risk_score(self, confidence, odds, active_count) -> float:
    risk = (1 - confidence) * 0.4            # 低置信度权重
    if odds > 1.90:                          # 高赔率阈值（适配 under 区间）
        risk += 0.3                          # 高赔率惩罚
    elif odds > 1.80:                        # 中赔率阈值
        risk += 0.15                         # 中赔率惩罚
    risk += min(active_count * 0.02, 0.1)    # 持仓惩罚
    return round(min(risk, 1.0), 2)
```

### 2.7 配置热更新

```python
def load_fresh_strategy(user_id) -> (AIConfig|None, StrategyConfig):
    """每次从 DB 读取最新 AIConfig，绕过会话缓存"""

def decision_passes_strategy(decision, strat) -> tuple[bool, str]:
    """下单前再拦一道：配置热更新后 decision 可能已过期"""
```

---

## 3. 策略阀门 (Gates)

**文件**: `app/ai/strategy_gates.py`

### 3.1 阀门函数一览

| 函数 | 用途 | 关键逻辑 |
|------|------|----------|
| `min_stake_floor(strat)` | 最低仓位 | AI 路径允许下到 1 元 |
| `stake_bounds(strat)` | 仓位区间 | (1, max_bet_amount) |
| `cap_stake(stake, strat)` | 仓位截断 | 夹在 [1, max_bet_amount] |
| `resolve_site_minimum_stake(...)` | 站点最低额调整 | 保留策略上限和余额两道硬门禁 |
| `team_is_excluded(home, away, excluded)` | 球队排除 | 双向子串匹配 |
| `sport_is_preferred(sport, preferred)` | 球类偏好 | soccer->football 归一 |
| `calc_daily_pnl(db, user_id)` | 日盈亏 | 站点余额 + pending stake（避免未结算误计为亏损） |
| `count_today_bets(db, user_id)` | 今日注数 | is_ai_bet=True |
| **`check_daily_risk(db, user_id, strat)`** | **日风控** | 止损/止盈/注数三合一 |
| **`gate_recommendation_for_place(...)`** | **下单前完整校验** | 策略加载 + should_bet + 球队排除 + 球类偏好 + 日风控 + 仓位截断 |

### 3.2 check_daily_risk 触发条件

```python
async def check_daily_risk(db, user_id, strat) -> tuple[bool, str]:
    pnl = await calc_daily_pnl(db, user_id)  # 含 pending stake 修正

    # 1. 日止损
    if pnl <= -stop_loss:
        return True, f"触发止损线: 日亏损 {abs(float(pnl))} >= 止损额 {stop_loss}"

    # 2. 日止盈
    if pnl >= take_profit:
        return True, f"触发止盈线: 日收益 {float(pnl)} >= 止盈额 {take_profit}"

    # 3. 每日注数
    if today_bets >= max_daily_bets:
        return True, f"已达每日投注上限: {today_bets}/{max_daily_bets}"

    return False, ""
```

### 3.3 calc_daily_pnl（含 pending stake 修正）

```python
async def calc_daily_pnl(db, user_id) -> Decimal:
    """日盈亏 = 当日总资产变化（加回未结算注单 stake）"""
    site_balances = await load_site_balances(db, user_id)
    total_assets = sum(float(s.balance) for s in site_balances)

    # 加回未结算注单的 stake（站点余额已扣除 pending stake，但未结算≠亏损）
    pending_stake = await db.execute(
        select(func.sum(Bet.stake)).where(
            Bet.user_id == user_id,
            Bet.status == BetStatus.SUCCESS,
            Bet.actual_payout.is_(None),  # 未结算
        )
    )
    adjusted_total = total_assets + float(pending_stake.scalar() or 0)

    pnl_info = await get_daily_pnl(user_id, adjusted_total)
    return Decimal(str(pnl_info["daily_pnl"]))
```

### 3.4 gate_recommendation_for_place 完整流程

```python
async def gate_recommendation_for_place(*, user_id, rec, stake, db):
    # 1. 加载最新策略配置（热更新）
    ai_config, strat = await load_fresh_strategy(user_id)

    # 2. 推荐有效性
    if not r.get("should_bet"):
        return False, "推荐已过期或不可投注", 0, strat

    # 3. 球队排除检查
    if team_is_excluded(home, away, ai_config.excluded_teams):
        return False, "球队在排除名单中", 0, strat

    # 4. 球类偏好检查
    if not sport_is_preferred(sport, ai_config.preferred_sports):
        return False, f"球类 {sport} 不在偏好列表中", 0, strat

    # 5. 日风控检查
    triggered, reason = await check_daily_risk(db, user_id, strat)
    if triggered:
        return False, reason, 0, strat

    # 6. 仓位截断
    capped = cap_stake(stake, strat)
    return True, "", capped, strat
```

---

## 4. GPT 分析引擎 (MatchAnalyzer)

**文件**: `app/ai/analyzer.py`

### 4.1 分析流程

```python
class MatchAnalyzer:
    async def analyze_match(match_info, historical_data, market_odds) -> dict:
        # 1. 缓存查询（正缓存 180s / 负缓存 150s / skip 缓存 180s）
        # 2. 构建 prompt（全场小球 + 上下半场小球）
        # 3. 调用 GPT（max_retries=2，超时不重试，429 退避 1s+2s）
        # 4. 解析结果 + 归一化 prediction（under/skip）
        # 5. 赛前数据不足时硬性压低置信度
        # 6. 盘口+基本面结构化信号二次校准（11 种信号）
        # 7. 写缓存 + 记录预测到 Redis（7 天）
```

### 4.2 信号复核 (_build_signal_review)

| 信号 | 方法 | 说明 |
|------|------|------|
| 分析页信号 | `_analysis_page_signal` | GPT 分析结果中的市场观点 |
| 趋势页信号 | `_trend_page_signal` | 历史趋势信号 |
| 三重门禁状态 | `_market_triad_status` | 初指+实时盘口+基本面三者齐备检查 |
| 初指 vs 即时盘 | `_opening_live_signal` | 初指与即时盘变化方向 |
| 实时节奏 | `_live_pace_signal` | 比赛节奏（用 `_elapsed_minutes` 正确换算篮球倒计时） |
| 反追低位小球 | `_anti_chase_signal` | 防止追低位小球 |
| 盘口变化一致性 | `_movement_alignment` | 盘口变化方向与预测一致性 |
| 近况 | `_recent_form_signal` | 近 N 场表现 |
| 交锋 | `_h2h_signal` | 历史交锋记录 |
| 排名 | `_standings_signal` | 联赛排名 |
| 比赛阶段 | `_stage_signal` | 比赛阶段（早段/末段，用 `_elapsed_minutes`） |

**输出**: `verdict`, `confidence_delta`, `confidence_cap`, `confidence_floor`, `triad_ready`

### 4.3 _elapsed_minutes（篮球倒计时转换）

```python
@staticmethod
def _elapsed_minutes(match_info: dict) -> Optional[float]:
    """获取已进行分钟数（篮球倒计时自动转换为已进行时间）。"""
    secs = match_elapsed_seconds(sport=sport, period=period, clock=clock)
    if secs is not None and secs > 0:
        return round(secs / 60.0, 2)
    return parse_match_clock_minutes(clock)  # 足球回退
```

Q4 剩 8:30 -> 返回 ~39.5 分钟（非 8.5）。

### 4.4 缓存策略

| 类型 | TTL | 说明 |
|------|-----|------|
| 正缓存 | 180s (3min) | consensus_reached=True 时缓存 |
| 负缓存 | 150s (2.5min) | 无共识结果缓存 |
| skip 缓存 | 180s (3min) | prediction=skip 时缓存 |
| Redis 预测记录 | 604800s (7天) | ai:prediction:{match_id} |

### 4.5 GPT 调用错误处理

| 错误类型 | 处理 |
|----------|------|
| 超时 | 不重试，直接 raise |
| 429 限流 | 指数退避 1s+2s，最多重试 2 次 |
| 其他错误 | 最多重试 1 次，sleep 0.5s |
| 空 choices | raise RuntimeError |

### 4.6 Prompt 中的玩法限制

```
## 投注市场（全场小球 / 上下半场小球）
- 仅分析全场小球(total)和上下半场小球(first_half_total/second_half_total)的 under 方向
- 其他玩法(胜负/让球/特殊盘/串关)一律不分析不下注

## 输出格式（严格JSON）
{
    "bet_type": "total | first_half_total | second_half_total",
    "prediction": "under 或 skip",
    ...
}

注意：bet_type 只能是 total/first_half_total/second_half_total；
      prediction 只能是 under/skip；skip 时 confidence=0.0
```

---

## 5. 候选过滤 (analysis_filters)

**文件**: `app/ai/analysis_filters.py`

### 5.1 前置过滤 (skip_reason_for_match)

```python
def skip_reason_for_match(m, total_line, *, odds_map, min_odds, max_odds) -> Optional[str]:
    # 1. 中国赛事过滤 -> "china_match"
    # 2. 联赛黑名单 -> "league_blacklisted"
    # 3. 赔率区间检查（强制 total_odds_meet_min）
    #    仅检查 TOTAL under 赔率，无小球赔率的比赛直接跳过
    # 4. 比赛阶段检查（即将结束 <10min / 比分超盘）
```

**关键变更**：足球和篮球都强制使用 `total_odds_meet_min`，不再允许胜负/让球赔率兜底通过扫描。

### 5.2 排序策略

```python
def sort_just_started_first(matches):
    # 排序键: (已进行秒数, 开球时间戳, match_id) 升序
    # 无时钟的赛事 elapsed = 10**8（排到最后）
```

### 5.3 常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `ENDING_SOON_MINUTES` | 10.0 | 即将结束阈值 |
| `DEFAULT_MIN_ODDS` | 1.1 | 默认最低赔率 |
| `DEFAULT_MAX_ODDS` | 10.0 | 默认最高赔率 |

---

## 6. 自动投注引擎 (AIBettingEngine)

**文件**: `app/ai/auto_better.py`

### 6.1 主循环

```python
class AIBettingEngine:
    async def _main_loop(self):
        while self.is_running:
            await self._run_cycle()           # 单次分析+投注周期
            await self._renew_lock()          # 续期跨进程锁
            await asyncio.sleep(120)           # 间隔

    async def _run_cycle(self):
        # Phase 1: 扫描候选 -> 风控检查 -> 同场分组
        # Phase 2+3: 流式分析 -> 策略闸门 -> 立即下单
        # 并发: Semaphore(8), bet_lock: asyncio.Lock()
```

### 6.2 候选扫描

```python
async def _scan_candidates(self, db, ai_config) -> list[dict]:
    # 1. 获取已连接站点 (OB/平博)
    # 2. 查询 LIVE 比赛（含 odds）
    # 3. 前置过滤: skip_reason_for_match()（仅 TOTAL under）
    # 4. 排序: sort_just_started_first()
    # 5. 截断: candidates[:120]
```

### 6.3 下单执行 (_execute_bet)

```python
async def _execute_bet(self, db, user, decision, ...) -> bool:
    # 0. 引擎锁检查：token != Redis 锁当前持有者 -> 拒绝
    # 1. 同场未结算单检查 -> 跳过
    # 2. match 级 Redis 锁（ai:bet:lock:{uid}:{mid}, TTL=10s）
    #    防止 API 一键下单与引擎并发下单
    # 3. 玩法白名单: bet_type not in {total, first_half_total, second_half_total} -> 拒绝
    # 4. 日风控: check_daily_risk()
    # 5. 仓位截断: cap_stake()
    # 6. 站点最低额: resolve_site_minimum_stake()
    # 7. 调用 place_bet()
    # 8. finally: 释放 match 级锁
```

### 6.4 跨进程互斥

| 锁 | Key | TTL | 用途 |
|----|-----|-----|------|
| 引擎锁 | `ai:engine:lock:{user_id}` | 900s (15min) | 跨进程互斥，SET NX |
| 运行心跳 | `ai:engine:running:{user_id}` | 900s | 引擎存活标记 |
| match 级锁 | `ai:bet:lock:{uid}:{mid}` | 10s | 防止 API+引擎同场重复下单 |

引擎锁续期：`_main_loop` 每轮 + 异常后都续期，防止连续异常导致锁过期。

### 6.5 风控检查 (_check_risk)

```python
async def _check_risk(self, db, user, config) -> tuple[bool, str]:
    # 1. 日风控（止损/止盈/注数）
    # 2. 余额检查：spendable < AI_MIN_BALANCE(10.0) -> 拒绝
    #    余额来源: OB/平博真实账户余额的较大者
```

---

## 7. API 接口

**文件**: `app/api/ai_bets.py`，路由前缀 `/api/v1/ai`

| 端点 | 方法 | 说明 |
|------|------|------|
| `/config` | GET | 获取 AI 投注配置 |
| `/config` | PUT | 更新配置（落库 + 热更新清缓存 + WebSocket 广播） |
| `/start` | POST | 启动 AI 自动投注（须 active 模式） |
| `/stop` | POST | 停止 AI 引擎 |
| `/status` | GET | 引擎运行状态（含人工/自动开关） |
| `/recommend/{match_id}` | GET | 单场 AI 小球投注建议 |
| `/recommendations` | GET | 批量推荐（缓存 + 过滤 + 下单模式筛选） |
| `/recommendations/start` | POST | 开始后台轮询分析（须 manual 模式） |
| `/recommendations/stop` | POST | 停止后台分析 |
| `/history` | GET | AI 投注记录（分页） |
| `/one-click-bet/{match_id}` | POST | 一键投注（gate 校验 + place_bet） |

### 一键投注流程

```python
# 1. 获取推荐
rec = await analyze_and_recommend(match_id, user_id, stake=req.stake)
# 2. 策略阀门校验（含球队排除/球类偏好/日风控/仓位截断）
ok, reason, stake, strat = await gate_recommendation_for_place(
    user_id=user_id, rec=rec, stake=req.stake, db=db
)
# 3. 真实下单
result = await place_bet(match_id, bet_type="total", selection="under", ...)
```

---

## 8. 数据模型

**文件**: `app/models/user.py`

### BetType 枚举

```python
class BetType(str, Enum):
    MONEYLINE = "moneyline"                          # 胜负
    SPREAD = "spread"                                # 让分
    TOTAL = "total"                                  # 全场小球总分
    FIRST_HALF_TOTAL = "first_half_total"            # 上半场小球
    SECOND_HALF_TOTAL = "second_half_total"          # 下半场小球
    PROPOSITION = "prop"                              # 特殊投注
    PARLAY = "parlay"                                # 串关
    LIVE = "live"                                    # 滚球
```

### 核心模型

| 模型 | 表名 | 关键字段 |
|------|------|----------|
| **User** | `users` | `username`, `role`, `balance`, `ai_enabled`, `ai_risk_level`, `bet_mode`(manual/active) |
| **AIConfig** | `ai_configs` | `user_id`(unique), `strategy`("simple"), `max_bet_amount`, `max_daily_bets`, `min_confidence`, `preferred_sports`(JSON), `excluded_teams`(JSON), `stop_loss`, `take_profit`, `min_odds`, `max_odds`, `use_llm_analysis`, `is_active` |
| **Match** | `matches` | `external_id`, `sport`, `league`, `home_team`, `away_team`, `status`, `home_score`, `away_score`, `extra_data`(JSON, 含 clock/period/site_code) |
| **Odds** | `odds` | `match_id`(FK), `bet_type`, `odds_data`(JSON), `spread`, `total`, `provider`, `is_live` |
| **Bet** | `bets` | `user_id`(FK), `match_id`(FK), `bet_type`, `selection`(under), `odds`, `stake`, `potential_payout`, `actual_payout`, `line`, `provider`, `status`, `is_ai_bet`, `ai_confidence`, `ai_reasoning`, `settled_at` |

---

## 9. 盘口解析 (OB/平博)

### 9.1 三路径覆盖

| 路径 | 文件 | 全场大小球 | 上半场 | 下半场 | 仅 under |
|------|------|------------|--------|--------|----------|
| DOM 抓取 | `venue_live.py` | ✅ | ✅ | ✅ | ✅ |
| OB API | `plugins/ob/odds.py` | ✅ | ✅ | ✅ | ✅ |
| 平博 API | `plugins/pinnacle/odds.py` | ✅ | ✅ | ✅ | ✅ |

### 9.2 venue_live.py (DOM 抓取路径)

#### 全场大小球解析

```python
# 全场大小球（DOM 正则 / 关键词兜底）
over_v = coerce_float_european(item.get("over"))
under_v = coerce_float_european(item.get("under"))
total_line = float(item.get("total_line"))

# Fallback: 从 raw 文本解析
if (not over_v or not under_v) and item.get("raw"):
    tot = _parse_total_from_raw(raw, sport=sport)

# 允许只有 under 时也创建 TOTAL
if under_v and 1.1 <= under_v <= 10 and total_line:
    odds_list.append(RemoteOdds(bet_type="total", total=total_line, odds_data=total_data))
```

#### 上下半场大小球解析

```python
# 上下半场大小球（从 raw 文本提取"上半场"/"下半场"标识的大小球）
for half_label, half_bt in (("上半场", "first_half_total"), ("下半场", "second_half_total")):
    if half_label in raw_txt:
        ht = _parse_total_from_raw(raw_txt, sport=sport)
        if ht and ht.get("under") and 1.1 <= ht["under"] <= 10 and ht.get("line"):
            odds_list.append(RemoteOdds(bet_type=half_bt, total=ht_line, odds_data=ht_data))
```

#### _parse_total_from_raw 正则

```
匹配模式: "大 line over ... 小 line under" 或反向
Fallback: 仅提取 under（"小 line under"）
盘口合理性: 足球 0.5 ≤ line ≤ 12, 篮球 100 ≤ line ≤ 280（宽松兜底 0.5-280）
```

#### 赔率去重

```python
# 同 bet_type + 同盘口线只保留最新一条
_dedup: dict[str, RemoteOdds] = {}
for o in odds_list:
    key = f"{o.bet_type}|{o.total or 0}|{o.spread or 0}"
    _dedup[key] = o
odds_list = list(_dedup.values())
```

### 9.3 OB API 插件 (plugins/ob/odds.py)

#### 全场大小球

```python
def _total_from_hps(hps, *, mid, csid, tid, match_type) -> Optional[RemoteOdds]:
    # 匹配 hpid=="2" 或 hpn 含"大小"
    # 要求 over + under 同时存在
    # 返回 RemoteOdds(bet_type="total", ...)
```

#### 上下半场大小球

```python
def _half_totals_from_hps(hps, *, mid, csid, tid, match_type) -> list[RemoteOdds]:
    # 遍历 hps，匹配 hpn 含"上半场"/"下半场" + "大小"
    # 允许只有 under（与 DOM 路径一致）
    # odds_data["_ob"]["period"] = "上半场"/"下半场"
    # 返回 RemoteOdds(bet_type="first_half_total"/"second_half_total", ...)
```

**调用**：`parse_matches_pb` 中 `odds_list.extend(half_totals)`

### 9.4 平博 API 插件 (plugins/pinnacle/odds.py)

#### 全场大小球

```python
def _odds_from_period0(p0, *, mid, sport_id) -> list[RemoteOdds]:
    # period "0" (全场): [spread, total, moneyline]
    # 大小球：要求 over + under 同时存在
    # 返回 RemoteOdds(bet_type="total", ...)
```

#### 上下半场大小球

```python
def _half_totals_from_period(pdata, *, mid, sport_id, period_key, half_bt, half_label) -> list[RemoteOdds]:
    # period "1" (上半场) / period "2" (下半场)
    # 结构同 period0: [spread, total, moneyline]
    # 仅提取 total（大小球），允许只有 under
    # odds_data["_site"]["period"] = "上半场"/"下半场"
    # 返回 RemoteOdds(bet_type="first_half_total"/"second_half_total", ...)
```

**调用**：`parse_compact_events` 中遍历 `periods["1"]` / `periods["2"]`

### 9.5 RemoteOdds 数据结构

```python
@dataclass
class RemoteOdds:
    bet_type: str          # "total" / "first_half_total" / "second_half_total"
    odds_data: dict        # {"under": 1.75, "over": 1.85, "_site": {...}}
    spread: float = 0      # 让球线（SPREAD 用）
    total: float = 0       # 大小球盘口线
```

---

## 10. 实时监控 (live_monitor)

**文件**: `scripts/live_monitor.py`

### 监控项

每 30 秒一轮，检查：

| 检查项 | 内容 |
|--------|------|
| 比赛数据 | 比分异常（足球>10/篮球<5）、时钟为空、赔率缺失/偏少、联赛黑名单 |
| 赔率结构 | TOTAL under/over 完整性、SPREAD 让球线、赔率值范围(1.01-100) |
| AI 引擎 | Redis 运行标记 + 锁持有状态 |
| 策略闸门 | SPORT_RISK 参数、黑名单完整性、A2 闸门活跃、_elapsed_minutes 存在、GPT 重试=2、缓存 TTL=180 |

### WebSocket 推送

```python
async def _broadcast_ai_log(payload: dict):
    # 1. 广播到 ai_logs 频道（前端订阅）
    await manager.broadcast_to_channel("ai_logs", {
        "type": "ai_monitor",
        "data": payload,
    })
    # 2. 广播到所有用户
    await manager.broadcast_all({
        "type": "ai_monitor",
        "data": payload,
    })
```

**payload 结构**: `timestamp`, `matches{total,ob,pinnacle}`, `odds{total,total_complete,spread_complete}`, `engine{running,lock}`, `issues[]`, `summary`

---

## 11. 配置项参考

**文件**: `app/config.py`，类 `Settings(BaseSettings)`

### GPT/LLM 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `GPT_API_KEY` | None | GPT API 密钥 |
| `GPT_BASE_URL` | https://xfastapi.ai/v1 | GPT API 地址 |
| `GPT_MODEL` | gpt-5.6-terra | GPT 模型 |
| `GPT_TIMEOUT_SEC` | 45.0 | 单次分析超时 |
| `LLM_CLIENT_TIMEOUT_SEC` | 50.0 | 客户端超时 |
| `LLM_MAX_TOKENS` | 2048 | 最大输出 tokens |
| `LLM_DEFAULT_CONFIDENCE` | 0.33 | 默认置信度 |
| `LLM_TEMPERATURE` | 0.2 | 温度 |

### 缓存配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 正缓存 | 180s (硬编码) | consensus_reached=True |
| `AI_SKIP_CACHE_TTL` | 180 | skip 负缓存 |
| `LLM_NEG_CACHE_TTL` | 150 | 无共识负缓存 |

### 策略配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `AI_MIN_CONFIDENCE` | 0.47 | 下单最低置信度 |
| `AI_MIN_ODDS` | 1.65 | 下单最低赔率 |
| `AI_MAX_ODDS` | 5.00 | 下单最高赔率 |
| `AI_STRATEGY_MAX_BET_AMOUNT` | 100.0 | 单笔最大金额 |
| `AI_STRATEGY_MAX_DAILY_BETS` | 10 | 每日注数上限 |
| `AI_STOP_LOSS` | 500.0 | 日止损 |
| `AI_TAKE_PROFIT` | 1000.0 | 日止盈 |

### 引擎配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `AI_SCAN_INTERVAL_SEC` | 120 | 引擎扫描间隔 |
| `AI_LIVE_SCAN_LIMIT` | 120 | 自动引擎扫描上限 |
| `AI_RECS_LIMIT` | 80 | 推荐页分析上限 |
| `AI_ANALYZE_CONCURRENCY` | 8 | GPT 分析并发数 |
| `AI_MIN_BALANCE` | 10.0 | 最低可用余额 |
| `AI_DEFAULT_STAKE` | 100.0 | 默认下注金额 |
| `AI_RETRY_SLEEP_SEC` | 60 | 引擎异常重试休眠 |

### 风险评分配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `AI_RISK_LOW_CONF_WEIGHT` | 0.4 | 低置信度风险权重 |
| `AI_RISK_HIGH_ODDS_PENALTY` | 0.3 | 高赔率(>1.90)惩罚 |
| `AI_RISK_MID_ODDS_PENALTY` | 0.15 | 中赔率(>1.80)惩罚 |
| `AI_RISK_ACTIVE_PENALTY` | 0.02 | 每笔持仓风险系数 |
| `AI_RISK_ACTIVE_CAP` | 0.1 | 持仓风险上限 |

---

## 12. 投注决策全链路

```
用户配置 (AIConfig)
    │
    ▼
load_fresh_strategy() ──-> StrategyConfig（每轮从 DB 重新加载）
    │
    ▼
┌─────────────────────────────────────────────────┐
│  Phase 1: 扫描候选                                │
│  _scan_candidates()                              │
│  ├── 查询 LIVE 比赛 + odds                        │
│  ├── skip_reason_for_match() 前置过滤             │
│  │   ├── 中国赛事过滤                             │
│  │   ├── 联赛黑名单（含友谊赛/表演赛）             │
│  │   ├── 赔率区间（强制 total_odds_meet_min）     │
│  │   ├── 即将结束 (< 10min)                       │
│  │   └── 比分超盘                                │
│  └── sort_just_started_first() 刚开赛优先         │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│  Phase 2: GPT 分析                                │
│  MatchAnalyzer.analyze_match()                   │
│  ├── 缓存查询 (180s 正 / 150s 负 / 180s skip)     │
│  ├── _build_analysis_prompt()                     │
│  │   └── 限制: bet_type ∈ {total, first_half_total,│
│  │       second_half_total}, prediction ∈ {under,skip}│
│  ├── _call_gpt() (max_retries=2, 超时不重试)      │
│  ├── _apply_context_quality_cap() 数据不足压低    │
│  ├── _apply_signal_review() 11 种信号二次校准     │
│  │   └── _elapsed_minutes() 篮球倒计时正确转换    │
│  └── 写缓存 + 记录预测 (7天)                      │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│  Phase 3: 策略评估                                │
│  StrategyEngine.evaluate_bet()                   │
│  ├── A0 玩法白名单 (total/first_half/second_half) │
│  ├── A1 方向合法 (under)                         │
│  ├── A2 模型共识 (consensus_reached)              │
│  ├── A3 置信度达标 (含胜率自适应)                 │
│  ├── A4 篮球三重门禁 (triad_ready)               │
│  ├── B1 盘线区间 + 时段风控                       │
│  ├── B2 联赛黑名单                               │
│  ├── B3 高赔率 under 风控 (≥2.0 拒绝)            │
│  ├── C1 盘口变化方向                             │
│  ├── C2 篮球市场同步                             │
│  ├── D1 中后段余量不足                           │
│  ├── D2 补时余量过薄                             │
│  ├── E1 赔率区间 [min_odds, max_odds]           │
│  ├── E2 EV 盈亏平衡 (conf ≥ 1/odds + edge)      │
│  └── 仓位计算 (动态仓位公式)                      │
└─────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────┐
│  Phase 4: 下单执行                                │
│  _execute_bet() / one_click_bet()                │
│  ├── 引擎锁检查（token == Redis 锁持有者）        │
│  ├── 同场未结算单检查                             │
│  ├── match 级 Redis 锁 (TTL=10s, 防竞态)         │
│  ├── 玩法白名单 ({total, first_half_total,        │
│  │   second_half_total} + under)                │
│  ├── check_daily_risk() 日风控                    │
│  │   ├── 日止损 (pnl ≤ -stop_loss)              │
│  │   ├── 日止盈 (pnl ≥ take_profit)             │
│  │   └── 每日注数 (n ≥ max_daily_bets)           │
│  ├── cap_stake() 仓位截断                        │
│  ├── resolve_site_minimum_stake() 站点最低额      │
│  └── place_bet() 真实下单                        │
└─────────────────────────────────────────────────┘
```

### 关键设计原则

1. **仅投注小球 (under)**: 系统仅支持 `selection="under"` 的小球投注
2. **玩法白名单**: 仅全场小球 (`total`) + 上半场小球 (`first_half_total`) + 下半场小球 (`second_half_total`)，其他一律不分析不下单
3. **六阶段闸门**: 玩法白名单->信号有效性->结构性风控->市场一致性->滚球余量->赔率有效性
4. **热更新**: 每轮循环从 DB 重新加载 AIConfig，配置变更即时生效
5. **跨进程互斥**: Redis SET NX 锁保证同一用户同一时间只有一个引擎实例
6. **match 级锁**: TTL=10s 的 Redis 锁防止 API 一键下单与引擎并发下单同一比赛
7. **缓存策略**: GPT 分析结果缓存 3 分钟，避免重复调用
8. **三重门禁**: 篮球 under 要求初指 + 实时盘口 + 基本面三者齐备
9. **动态仓位**: 置信度 × 风险 × 站点 × 余额 × 日亏五因子动态计算
10. **日风控**: 止损/止盈/注数三合一，pending stake 不误计为亏损
11. **篮球时间转换**: `_elapsed_minutes` 将篮球倒计时正确转换为已进行分钟
12. **实时监控**: 每 30s 检查比分/赔率/闸门/引擎状态，通过 WebSocket 推送到 AI 日志面板
13. **三路径半场解析**: DOM/OB API/平博 API 三条路径都支持全场+上下半场大小球解析，且允许仅有 under

---

## 13. 测试体系

**目录**: `tests/ai/`

### 测试文件清单

| 文件 | 测试数 | 覆盖范围 |
|------|--------|----------|
| `conftest.py` | - | mock fixtures（策略配置/比赛/分析结果/AIConfig/cache/盘口数据） |
| `test_strategy.py` | 31 | A0-E2 闸门 + SPORT_RISK + 黑名单 + 仓位 + 风险评分 |
| `test_strategy_gates.py` | 24 | 日风控 + 球队排除 + 球类偏好 + 仓位截断 + gate 完整流程 |
| `test_analyzer.py` | 12 | 篮球倒计时转换 + _stage_signal + GPT 重试 + 缓存 TTL |
| `test_analysis_filters.py` | 13 | 中国赛事 + 联赛黑名单 + 赔率区间 + 排序 |
| `test_integration.py` | 18 | 全链路 + 竞态 + 热更新 + 余额锚定 + 半场大小球 + 玩法白名单 |
| `test_odds_parsing.py` | 21 | BetType 枚举 + OB 半场解析 + 平博半场解析 + venue_live + 去重 |
| **合计** | **119** | 全部通过 |

### 运行方式

```bash
# 全量测试
python3 -m pytest tests/ai/ -v --tb=short

# 单文件测试
python3 -m pytest tests/ai/test_strategy.py -v

# 单个测试类
python3 -m pytest tests/ai/test_odds_parsing.py::TestOBHalfTotalsParsing -v
```

### 测试覆盖矩阵

| doc.md 章节 | 测试文件 | 覆盖项 |
|--------------|----------|--------|
| 第 2 章 策略引擎 | test_strategy.py | A0 玩法白名单 / A1 方向 / A2 共识 / A3 置信度 / A4 三重门禁 / B1 盘线 / B2 黑名单 / B3 高赔率 / C1-C2 市场一致性 / D1-D2 余量 / E1-E2 赔率 / 仓位计算 / 风险评分 / SPORT_RISK / 余额锚定 / 日亏递减 |
| 第 3 章 策略阀门 | test_strategy_gates.py | 球队排除 / 球类偏好 / 仓位截断 / 止损 / 止盈 / 注数上限 / 一键下单 gate / pending stake |
| 第 4 章 GPT 分析 | test_analyzer.py | _elapsed_minutes / _stage_signal / _anti_chase_signal / GPT 重试 / 空 choices / 超时不重试 / 缓存 TTL |
| 第 5 章 候选过滤 | test_analysis_filters.py | 中国赛事 / 联赛黑名单 / 赔率区间 / 即将结束 / 比分超盘 / 排序 |
| 第 6 章 自动引擎 | test_integration.py | 全链路 / A2 拒绝 / B1 拒绝 / 日风控拒绝 / 同场重复 / 跨组件竞态 / 引擎锁 |
| 第 7 章 API | test_integration.py | 热更新 / decision_passes_strategy |
| 第 8 章 数据模型 | test_odds_parsing.py | BetType 枚举 / FIRST_HALF_TOTAL / SECOND_HALF_TOTAL |
| 第 9 章 盘口解析 | test_odds_parsing.py | OB _half_totals_from_hps / 平博 _half_totals_from_period / venue_live _parse_total_from_raw / 赔率去重 / 盘口合理性 / 仅 under |
| 第 10 章 实时监控 | （生产环境验证） | 30s 轮询 / WebSocket 推送 |
| 第 12 章 全链路 | test_integration.py | 完整链路 / 半场通过 A0 / 非白名单拒绝 / OB parse_matches_pb |
