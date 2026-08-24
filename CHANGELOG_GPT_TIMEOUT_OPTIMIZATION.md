# GPT 超时优化变更说明

> 变更日期: 2026-08-24
> 变更范围: AI 分析引擎 GPT 超时处理逻辑
> 触发原因: 30 分钟生产监控显示 GPT 超时率 27%（35/131 次），超时后直接丢弃结果

---

## 一、问题分析

### 现状
- GPT 超时率 27%（35/131 次调用超时）
- 超时后直接返回 fallback 结果（盘口启发式），浪费分析机会
- `_call_gpt` 内部超时直接 raise，不重试
- `analyze_match` 外层 `asyncio.TimeoutError` 直接 fallback，不尝试精简 prompt 重试

### 根因
生产配置存在参数倒挂问题：
```
GPT_TIMEOUT_SEC=75        （外层 asyncio.wait_for 超时）
LLM_CLIENT_TIMEOUT_SEC=80 （httpx 单次调用超时）
```
外层超时(75s) < 内层超时(80s)，导致 httpx 客户端永远不会自己超时触发重试，而是被外层 `asyncio.wait_for` 直接取消。

---

## 二、变更内容

### 2.1 代码变更：三级超时重试机制

**文件**: `app/ai/analyzer.py`

| 变更点 | 行号 | 变更内容 |
|--------|------|----------|
| `_call_gpt` 超时重试 | ~730-762 | 首次超时后 max_tokens 减半重试 1 次（原：直接 raise） |
| `_call_gpt` 降配数组 | ~733 | 新增 `tokens_per_attempt = [LLM_MAX_TOKENS, max(512, LLM_MAX_TOKENS//2)]` |
| `analyze_match` 超时重试 | ~565-632 | `except asyncio.TimeoutError` 中调用 `_retry_with_minimal_prompt`，成功后走完整校准链返回 |
| `_retry_with_minimal_prompt` | ~649 | 新增 `system_extra` 参数，重试时也注入历史胜率反馈 |

### 2.2 超时重试链流程

```
正常调用 (timeout=40s)
  │
  ├─ 成功 → 返回结果
  │
  └─ 超时 (40s)
      │
      ├─ 第1级：_call_gpt 内部降配重试
      │   max_tokens 减半 (2458→1229)，0.5s 后重试
      │   ├─ 成功 → 返回结果
      │   └─ 超时 (40s) → raise
      │
      └─ 第2级：analyze_match 外层超时 (90s)
          调用 _retry_with_minimal_prompt
          ├─ 极简 prompt (核心数据 only)
          ├─ max_tokens=512
          ├─ 超时 30s
          ├─ 成功 → 走完整校准链返回结果
          └─ 失败 → _fallback_result (盘口启发式)

最坏情况总耗时: 40+0.5+40+30 = 110.5s
预期平均耗时: ~25-35s (大多数调用首次即成功)
```

### 2.3 配置变更

**文件**: `.env` + `docker-compose.yml`（backend + ai-engine）

| 参数 | 旧值 | 新值 | 说明 |
|------|------|------|------|
| `GPT_TIMEOUT_SEC` | 75 | 90 | 外层 asyncio.wait_for 超时，须 > 2×LLM_CLIENT_TIMEOUT_SEC |
| `LLM_CLIENT_TIMEOUT_SEC` | 80 | 40 | httpx 单次 API 调用超时，2次+sleep=80.5s < 90s 外层 |
| `LLM_MAX_TOKENS` | 2458 | 2458（不变） | 首次调用 tokens；超时重试自动减半到 1229 |
| `LLM_TEMPERATURE` | 0.2 | 0.2（不变） | — |
| `AI_ANALYZE_CONCURRENCY` | 12 | 12（不变） | — |

### 2.4 参数关系约束

```
GPT_TIMEOUT_SEC (90) > 2 × LLM_CLIENT_TIMEOUT_SEC (40) + sleep (0.5)
                     = 80.5s

精简重试超时 = min(GPT_TIMEOUT_SEC, 30) = 30s

最坏总耗时 = 40 + 0.5 + 40 + 30 = 110.5s
```

---

## 三、预期效果

| 指标 | 优化前 | 优化后（预估） |
|------|--------|----------------|
| GPT 超时率 | 27% | < 10%（降配重试恢复 ~50%） |
| 有效分析率 | 73% | ~90-93% |
| 超时后丢弃 | 100% fallback | ~50% 通过精简重试恢复 |
| 单次最坏耗时 | 75s → fallback | 110.5s（但概率 < 3%） |
| 平均分析耗时 | ~40s | ~30s（client 超时从 80s 降到 40s，快速失败） |

---

## 四、部署步骤

```bash
# 1. 重建 Docker 镜像（包含 analyzer.py 代码变更）
docker compose build backend

# 2. 重启 backend 和 ai-engine（应用新 .env 配置）
docker compose up -d backend ai-engine

# 3. 验证配置生效
docker exec ob-ai-engine env | grep -E "GPT_TIMEOUT|LLM_CLIENT"
# 预期输出:
# GPT_TIMEOUT_SEC=90
# LLM_CLIENT_TIMEOUT_SEC=40

# 4. 监控日志验证重试机制
docker logs -f ob-ai-engine 2>&1 | grep -E "超时|降配|精简.*重试|timeout"
```

---

## 五、回滚方案

如需回滚，恢复 `.env` 中超时参数并重建镜像：

```bash
# .env 恢复
GPT_TIMEOUT_SEC=75
LLM_CLIENT_TIMEOUT_SEC=80

# 重建重启
docker compose build backend && docker compose up -d backend ai-engine
```

代码变更无需回滚 — 新逻辑在旧参数下仅表现为：外层先超时 → 精简重试，仍比原方案多一次机会。

---

## 六、涉及文件清单

| 文件 | 变更类型 |
|------|----------|
| `app/ai/analyzer.py` | 代码：三级超时重试逻辑 |
| `.env` | 配置：GPT_TIMEOUT_SEC 75→90, LLM_CLIENT_TIMEOUT_SEC 80→40 |
| `docker-compose.yml` | 配置：backend + ai-engine 默认值同步 |
