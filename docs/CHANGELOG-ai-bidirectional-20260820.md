# AI 双向分析（大小球）配置变更说明

**变更日期**：2026-08-20
**变更类型**：功能扩展（小球单边 → 大小球双向）+ 数据驱动调参
**验证状态**：全量单测 140/140 通过，已部署容器并经实时日志验证

---

## 一、变更背景

原系统为**小球单边分析**：LLM 只允许输出 under/skip，over 在归一化层被判无效。
本次变更为大小球**双向对称分析**，两套闸门链完全隔离（互不参与），并基于
2597 条实时分析日志进行了数据驱动的参数校准。

## 二、Prompt 规则变更（app/ai/analyzer.py）

### 2.1 判定输出格式

| 项 | 变更前 | 变更后 |
|----|--------|--------|
| prediction 允许值 | `under / skip` | `under / over / skip` |
| value_bets.selection | 仅 `under` | `under 或 over` |
| 归一化词表 | 小/小球/under | + 大/大球/over |

### 2.2 分析指令（Prompt 主体）

```
变更前：仅分析全场小球与上下半场小球的 under 方向
变更后：分析全场大小球和上下半场大小球的 under 与 over 两个方向
        over 与 under 是对称的独立分析：哪边信号强判哪边，都不足则 skip
```

### 2.3 严格分析规则（四套：足球/篮球 × 小球/大球）

#### 核心结构变更

```
变更前（篮球）：必须满足至少3项，且必须包含盘口或基本面支持
变更后（篮球）：同向证据链（盘口/节奏/基本面至少2项同向，含盘口或基本面）
变更前（足球）：必须满足至少2项
变更后（足球）：同向证据链（至少2项同向，其中1项须为盘口或基本面）
```

#### 盘口方向判定阈值

| 信号 | 变更前 | 变更后 |
|------|--------|--------|
| 篮球强盘口信号 | 降/升盘≥5分（"无需水位确认"，但 LLM 实际仍查水位） | 降/升盘≥5分（**明确**：水位无反向大幅上升即成立） |
| 篮球中盘口信号 | 降/升盘2-5分 且 水位下降>3% | 降/升盘2-5分 且 水位涨幅<2%即算中性偏支持 |
| 水位有效阈值（篮球） | >3% | **>2%**（滚球水位波动天然小） |
| 水位有效阈值（足球） | >5% | **>2%** |
| 足球盘口信号 | 降/升盘 或 水位下降>5% | 降/升盘≥0.25球 或 水位下降>2% |
| 节奏偏差阈值 | >25% | **>20%** |
| 篮球基本面 over 参照 | 交锋场均>175 | >170 |

#### 新增证据条目

```
足球小球：当前比分远低于盘口线（余量≥2球）构成有利证据
足球大球：当前比分已接近盘口线（line-当前球数≤1）构成有利证据
篮球（双向）：盘口强信号（降/升盘≥5分）+任一其他同向证据 → 可给 conf 0.55-0.65
```

#### 保留的时段风控（未变）

```
足球：75分钟后 under 更谨慎；over 需 line-X≤1，差2球及以上必须 skip
篮球：44分钟后（Q4末段）双向默认更谨慎，弱信号必须 skip
```

### 2.4 score_analysis（实时比分视角，双向化）

```
变更前：仅"小球剩余容错 X 分"
变更后：小球视角（剩余容错）+ 大球视角（还需 X 分大球赢）
        + 大球节奏可达性（当前节奏×全场预计 vs 盘口线，剩余时间还需分数）
```

### 2.5 统计信号 over 分支（新增）

| 信号函数 | 新增 over 逻辑 |
|----------|----------------|
| _h2h_signal | 交锋均值≥line+margin 判支持；over_2_5_rate≥0.7 判支持 |
| _standings_signal | 攻防推导总分≥line+margin 判支持 |
| _stage_signal | 足球 65'后进球高发期支持 over；篮球 44'后罚球刷分利大分 |

### 2.6 pace_analysis 信号（纯盘口模式）

```
变更前：慢→小球 / 快→"不支持小球"
变更后：慢→利小球 / 快→利大球（对称双向表述）
```

## 三、数据供给层修复（分析严谨性）

| # | 问题 | 修复 |
|---|------|------|
| 1 | market_recommend.sel_allow 过滤 over——LLM 读不到 over 即时水位 | total 放开 ("under","over") |
| 2 | odds_delta 是 dict 但 float(dict) 必 TypeError 被吞——水位变动信号从未生效（死代码） | 两处改 dict 分支，输出双向水位差 |
| 3 | 升盘映射为 "against_under"，无法表达支持大球 | 统一映射 "over" |
| 4 | nowscore 无大球率 | h2h/form summary 补 over_2_5_rate |
| 5 | Prompt 硬编码"仅分析小球" | 双向化 |

## 四、策略闸门变更（app/ai/strategy.py）

### 4.1 双闸门架构

```
A1 分派：under → under 闸门链（原有保留）
         over  → AI_ENABLE_OVER 开关（默认 false=影子模式）
                 开启后走 over 独立闸门链
```

### 4.2 SPORT_RISK 参数（足球）

| 参数 | 变更前 | 变更后 | 依据 |
|------|--------|--------|------|
| margin_avg_goals | 2.75 | **2.55** | 联赛均值对滚球中段偏高估 |
| margin_factor | 1.30 | **1.05** | 实时日志：252单 46'/49' 余量1.50 被线性折算误拦（期望1.34×1.30=1.74） |
| over_pace_factor | 1.20 | **1.15** | 与 under 余量放宽对称 |

### 4.3 over 独立闸门参数（新增）

| 参数 | 值 | 说明 |
|------|-----|------|
| over_min_conf | 0.62 | 严于 under 0.55（大球时间风险天然更高） |
| over_min_conf_no_fund | 0.65 | 无基本面再加严 |
| over_min_line / over_max_line | 2.0 / 4.5 | 低线=市场极度看小；高线残余空间不足 |
| over_min_played_mins | 20.0 | 早段样本小 |
| over_late_block_mins | 85.0 | 85'后追大球时间不够 |
| over_pace_factor | 1.15 | D1-over 进球速率闸门安全系数 |
| over_min_remaining_goals | 2.0 | 还差≥2球基本无解，直接拒 |

### 4.4 D1 闸门分方向实现

```
under（原有）：余量闸门 margin = line - 当前总分，不足剩余期望×factor → 拒
over（新增）：速率闸门 needed = line - 当前总分
             needed ≥ 2 → 拒（基本无解）
             预期产出(当前节奏×剩余时间) < needed × pace_factor → 拒（防末段差一脚）
```

### 4.5 胜率自适应按方向隔离

```
变更前：recent_betting_stats 整体胜率决定 A3 门槛加成
变更后：by_selection[under] 只影响 under 链；by_selection[over] 只影响 over 链
        （under 连败不污染 over 门槛；over 上线初期样本<5 按硬地板运行）
```

### 4.6 仓位

```
under：conf_scale × 1.10 加成（封顶 0.95）——原有
over ：无加成，且整体 ×0.6 折（观察期小额验证）——新增
```

## 五、下单层变更

| 文件 | 变更 |
|------|------|
| config.py | 新增 AI_ENABLE_OVER=false（影子模式开关） |
| auto_better.py | 5 处方向白名单双方向化；影子模式双重拦截；前端摘要保留 over 展示 |
| pinnacle/bet_ui.py | side_words/anti 双向（over 用 大/over/高于，防误点小）；ouCell 反向词参数化 |
| ob/bet.py | playOptions 补 Over |

## 六、实时日志验证记录（2026-08-20 05:00-05:15）

```
数据源恢复后（OB 9 场滚球），日志证实：
✅ 双向水位进入 LLM："大小球赔率维持大1.71/小1.89"、"Over 1.95/Under 1.65"
✅ 大球信号被评估："升盘10.0分；盘口方向明显偏向大分"（match=6012）
✅ 第一条 over 判定：match=6017 码头工人联 vs 联合克雷明 pred=over conf=0.30
✅ 分析循环持续（每 120s，足球+篮球）
```

## 七、当前运行状态与恢复清单

### 临时设置（任务观察期）

```sql
-- 当前生效（临时）
stop_loss = 100000    -- 原值 100
bet_mode = 'manual'   -- 只分析不下单
ai_enabled = false    -- 手动分析模式
盈亏基线 = 139.26     -- 已重置
```

Redis 状态（同样需恢复时清理）：

```
ai:recs:watch:1:football = 1    -- 后台分析循环标记（持续触发每120s重跑）
ai:recs:watch:1:basketball = 1
```

### 任务完成后恢复

```sql
UPDATE ai_configs SET stop_loss = 100 WHERE user_id = 1;
-- 模式二选一：
UPDATE users SET bet_mode = 'active', ai_enabled = true WHERE id = 1;  -- 自动下单
UPDATE users SET bet_mode = 'manual' WHERE id = 1;                     -- 保持手动分析
```

```bash
# 停止后台分析循环（任一方式）
curl -X POST "http://localhost:8000/api/v1/ai/recommendations/stop?sport=football" -H "Authorization: Bearer <token>"
curl -X POST "http://localhost:8000/api/v1/ai/recommendations/stop?sport=basketball" -H "Authorization: Bearer <token>"
```

### over 下单放开（观察期结束后）

```yaml
# docker-compose.yml backend 环境变量（当前文件中未声明，仅靠 config.py 默认值 false；
# 放开时必须在此声明，否则容器重建后会回落到默认 false）
AI_ENABLE_OVER: "true"
```

## 八、相关测试

| 测试文件 | 覆盖 | 结果 |
|----------|------|------|
| tests/ai/test_over_gates.py | 双闸门链 21 项（影子模式/隔离地板/线区间/B3镜像/C1镜像/D1速率/统计隔离/仓位折扣） | 21/21 |
| tests/ai/test_data_bidirectional.py | 数据供给严谨性 17 项（odds_delta修复/sel_allow/大球率/信号分支/Prompt双向） | 17/17 |
| tests/ 全量 | 回归保护 | 140/140 |

## 九、已知限制

1. **篮球 over 样本不足**：闸门参数（over_min_line=130 等）为保守估计，待实盘数据校准
2. **半场 over 结构性缺失**：OB/平博半场盘口"under 必须、over 可选"，半场大球覆盖率天然不全
3. **大球判定样本稀少**：凌晨时段仅 6-9 场滚球，over 判定 1 条（conf=0.30 未达下单门槛）——需白天赛事高峰积累样本后再评估 over_min_conf 是否合理
