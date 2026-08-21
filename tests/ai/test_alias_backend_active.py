"""验证前端隐藏别名管理后，后端别名自动生效链路仍完整可用。

模拟场景：
  1. Redis 中无任何别名 → 首次匹配失败 → _record_alias_candidate 自动写入正式别名
  2. _get_runtime_alias_index 从 Redis 加载正式别名
  3. _team_alias_variants 使用 runtime 别名扩展队名变体
  4. 第二次匹配通过 runtime 别名命中
  5. 审计日志记录 auto_approve 动作

运行方式：
  python3 tests/ai/test_alias_backend_active.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 预注入 mock 模块
for _mod in ("redis", "redis.asyncio", "websockets", "websockets.legacy", "socksio"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import openai
openai.AsyncOpenAI = MagicMock()

PASS = "\033[32m✅\033[0m"
FAIL = "\033[31m❌\033[0m"
INFO = "\033[36m📋\033[0m"


def _print_result(name: str, ok: bool, detail: str = ""):
    status = PASS if ok else FAIL
    print(f"  {status} {name}" + (f" | {detail}" if detail else ""))


async def run_all():
    from app.services.nowscore_scraper import (
        _ALIAS_OVERRIDE_HASH_KEY,
        _ALIAS_CANDIDATE_HASH_KEY,
        _ALIAS_AUDIT_LOG_KEY,
        _ALIAS_OVERRIDE_CACHE_TTL,
        _team_alias_variants,
        _get_runtime_alias_index,
        _build_alias_override_record,
        _record_alias_candidate,
        list_alias_overrides,
        list_alias_audit_logs,
    )

    results = []

    # ═══════════════════════════════════════════════════════════════
    # 模拟 Redis：用内存 dict 代替
    # ═══════════════════════════════════════════════════════════════

    redis_hash_store: dict[str, dict[str, str]] = {}  # key → {field: json_str}
    redis_audit_logs: list[str] = []

    class MockCache:
        def __init__(self):
            self._store: dict[str, str] = {}
            self._hash: dict[str, dict[str, str]] = {}

        async def hset(self, key, field, value):
            import json
            if key not in self._hash:
                self._hash[key] = {}
            self._hash[key][field] = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            return 1

        async def hget(self, key, field):
            import json
            if key not in self._hash or field not in self._hash[key]:
                return None
            raw = self._hash[key][field]
            try:
                return json.loads(raw)
            except Exception:
                return raw

        async def hgetall(self, key):
            import json
            if key not in self._hash:
                return {}
            result = {}
            for k, v in self._hash[key].items():
                try:
                    result[k] = json.loads(v)
                except Exception:
                    result[k] = v
            return result

        async def hdel(self, key, *fields):
            if key not in self._hash:
                return 0
            deleted = 0
            for f in fields:
                if f in self._hash[key]:
                    del self._hash[key][f]
                    deleted += 1
            return deleted

        async def delete(self, key):
            deleted = 0
            if key in self._hash:
                del self._hash[key]
                deleted += 1
            if key in self._store:
                del self._store[key]
                deleted += 1
            return deleted

        async def get(self, key):
            return self._store.get(key)

        async def set(self, key, value, ttl=None):
            self._store[key] = value
            return True

        async def get_json(self, key):
            import json
            raw = self._store.get(key)
            if raw is None:
                return None
            return json.loads(raw)

        async def set_json(self, key, value, ttl=None):
            import json
            self._store[key] = json.dumps(value)
            return True

        async def lrange(self, key, start=0, end=-1):
            return redis_audit_logs[start:end+1] if start >= 0 else redis_audit_logs[end:]

        async def lpush(self, key, *values):
            for v in values:
                redis_audit_logs.insert(0, v)
            return len(values)

        async def exists(self, key):
            return key in self._store or key in self._hash

    # _emit_alias_audit_log 通过 cache.client.lpush/ltrim 写入
    # list_alias_audit_logs 通过 cache.lrange 读取
    class MockRedisClient:
        async def lpush(self, key, *values):
            for v in values:
                redis_audit_logs.insert(0, v)
            return len(values)

        async def ltrim(self, key, start, end):
            if end >= 0:
                del redis_audit_logs[end + 1:]
            else:
                del redis_audit_logs[:len(redis_audit_logs) + end + 1]
            return True

    mock_cache = MockCache()
    mock_cache.client = MockRedisClient()

    print(f"\n{INFO} 前置: 确认别名系统常量")
    print("  " + "─" * 60)
    results.append((
        "ALIAS_OVERRIDE_HASH_KEY 正确",
        _ALIAS_OVERRIDE_HASH_KEY == "nowscore:alias_overrides:data",
        f"key={_ALIAS_OVERRIDE_HASH_KEY}",
    ))
    _print_result(*results[-1])
    results.append((
        "ALIAS_CANDIDATE_HASH_KEY 正确",
        _ALIAS_CANDIDATE_HASH_KEY == "nowscore:alias_candidates:data",
        f"key={_ALIAS_CANDIDATE_HASH_KEY}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 1: 初始状态 — Redis 无别名，runtime index 为空")
    print("  " + "─" * 60)

    with patch("app.core.cache.cache", mock_cache):
        # 强制刷新缓存
        from app.services.nowscore_scraper import _invalidate_alias_override_cache
        _invalidate_alias_override_cache()
        index_empty = await _get_runtime_alias_index("football", force_refresh=True)

    results.append((
        "初始 runtime alias index 为空 dict",
        index_empty == {},
        f"entries={len(index_empty)}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 2: 模拟匹配失败 → _record_alias_candidate 自动写入正式别名")
    print("  " + "─" * 60)

    # 模拟场景：源站队名 "曼联" vs "切尔西"，捷报标题 "曼彻斯特联 vs 切尔西"
    # best_score=0.76（≥ _ALIAS_CANDIDATE_MIN_SCORE），候选会被自动写入
    sport = "football"
    home = "曼联"
    away = "切尔西"
    home_variants = ["曼联", "曼彻斯特联"]
    away_variants = ["切尔西", "车路士"]
    best_sid = 12345
    best_score = 0.76
    best_title = "曼彻斯特联 vs 切尔西"

    with patch("app.core.cache.cache", mock_cache):
        await _record_alias_candidate(
            sport=sport,
            home=home,
            away=away,
            home_variants=home_variants,
            away_variants=away_variants,
            best_sid=best_sid,
            best_score=best_score,
            best_title=best_title,
        )

    # 验证正式别名已写入 Redis
    with patch("app.core.cache.cache", mock_cache):
        overrides = await list_alias_overrides(sport="all", limit=100)

    results.append((
        "正式别名已写入 Redis（list_alias_overrides 返回 1 条）",
        len(overrides) == 1,
        f"count={len(overrides)}",
    ))
    _print_result(*results[-1])

    if overrides:
        ov = overrides[0]
        results.append((
            "正式别名 home_alias_group 包含源队名 + 候选队名",
            home in ov.get("home_alias_group", []) and "曼彻斯特联" in ov.get("home_alias_group", []),
            f"home_group={ov.get('home_alias_group')}",
        ))
        _print_result(*results[-1])
        results.append((
            "正式别名 approved_by == 'auto'",
            ov.get("approved_by") == "auto",
            f"approved_by={ov.get('approved_by')}",
        ))
        _print_result(*results[-1])
        results.append((
            "正式别名 best_score 正确",
            abs(float(ov.get("best_score", 0)) - 0.76) < 0.01,
            f"score={ov.get('best_score')}",
        ))
        _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 3: _get_runtime_alias_index 加载正式别名")
    print("  " + "─" * 60)

    with patch("app.core.cache.cache", mock_cache):
        _invalidate_alias_override_cache()
        index = await _get_runtime_alias_index("football", force_refresh=True)

    # index 是 {norm_team: [alias_list]} 的映射
    results.append((
        "runtime alias index 非空（已加载别名）",
        len(index) > 0,
        f"entries={len(index)}",
    ))
    _print_result(*results[-1])

    # 检查 "曼联" 的规范化形式是否在 index 中
    from app.services.nowscore_scraper import _norm_team
    norm_home = _norm_team(home)
    norm_away = _norm_team(away)
    norm_candidate_home = _norm_team("曼彻斯特联")
    norm_candidate_away = _norm_team("切尔西")

    results.append((
        "index 包含源队名规范化键",
        norm_home in index or norm_candidate_home in index,
        f"norm_home={norm_home!r}, in_index={norm_home in index}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 4: _team_alias_variants 使用 runtime 别名扩展队名")
    print("  " + "─" * 60)

    with patch("app.core.cache.cache", mock_cache):
        variants_without_runtime = _team_alias_variants("曼联", "football", None)
        variants_with_runtime = _team_alias_variants("曼联", "football", index)

    has_new = any(v not in variants_without_runtime for v in variants_with_runtime)
    results.append((
        "无 runtime index 时变体列表不含候选别名",
        "曼彻斯特联" not in variants_without_runtime,
        f"variants={variants_without_runtime}",
    ))
    _print_result(*results[-1])

    results.append((
        "有 runtime index 时变体列表包含候选别名",
        "曼彻斯特联" in variants_with_runtime,
        f"runtime_added={[v for v in variants_with_runtime if v not in variants_without_runtime]}",
    ))
    _print_result(*results[-1])

    results.append((
        "runtime 变体数量 > 无 runtime 变体数量",
        len(variants_with_runtime) > len(variants_without_runtime),
        f"without={len(variants_without_runtime)}, with={len(variants_with_runtime)}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 5: 审计日志记录 auto_approve 动作")
    print("  " + "─" * 60)

    with patch("app.core.cache.cache", mock_cache):
        audit_logs = await list_alias_audit_logs(limit=10)

    results.append((
        "审计日志非空",
        len(audit_logs) > 0,
        f"count={len(audit_logs)}",
    ))
    _print_result(*results[-1])

    if audit_logs:
        log = audit_logs[0]
        results.append((
            "审计日志 action == 'auto_approve'",
            log.get("action") == "auto_approve",
            f"action={log.get('action')}",
        ))
        _print_result(*results[-1])
        results.append((
            "审计日志 actor == 'auto'",
            log.get("actor") == "auto",
            f"actor={log.get('actor')}",
        ))
        _print_result(*results[-1])
        results.append((
            "审计日志 payload 包含 source_home",
            log.get("payload", {}).get("source_home") == "曼联",
            f"source_home={log.get('payload', {}).get('source_home')}",
        ))
        _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 6: 缓存失效 + TTL 过期后重新加载")
    print("  " + "─" * 60)

    # 验证缓存失效机制
    from app.services.nowscore_scraper import _alias_override_cache
    _invalidate_alias_override_cache()
    results.append((
        "缓存失效后 _alias_override_cache 被清空",
        not _alias_override_cache.get("data"),
        f"data={_alias_override_cache.get('data')}",
    ))
    _print_result(*results[-1])

    # 重新加载
    with patch("app.core.cache.cache", mock_cache):
        index_reloaded = await _get_runtime_alias_index("football", force_refresh=True)

    results.append((
        "重新加载后 index 仍然包含别名",
        len(index_reloaded) > 0,
        f"entries={len(index_reloaded)}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 7: 端到端模拟 — 源队名经别名扩展后匹配捷报标题")
    print("  " + "─" * 60)

    # 模拟 _find_schedule_id 中的匹配逻辑
    # 捷报标题包含 "曼彻斯特联"，源队名是 "曼联"
    title = "曼彻斯特联 vs 切尔西"
    title_lower = title.lower()

    # 无 runtime 别名时的变体
    home_variants_no_rt = _team_alias_variants("曼联", "football", None)
    home_lowers_no_rt = [v.lower().strip() for v in home_variants_no_rt if v.strip()]
    match_without_alias = any(v and v in title_lower for v in home_lowers_no_rt)

    # 有 runtime 别名时的变体
    home_variants_with_rt = _team_alias_variants("曼联", "football", index_reloaded)
    home_lowers_with_rt = [v.lower().strip() for v in home_variants_with_rt if v.strip()]
    match_with_alias = any(v and v in title_lower for v in home_lowers_with_rt)

    results.append((
        "无 runtime 别名时：'曼联' 无法匹配 '曼彻斯特联 vs 切尔西'",
        not match_without_alias,
        f"match={match_without_alias}",
    ))
    _print_result(*results[-1])

    results.append((
        "有 runtime 别名时：'曼联'→'曼彻斯特联' 成功匹配标题",
        match_with_alias,
        f"match={match_with_alias}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    # 汇总
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "═" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)
    print(f"  总计: {total} 项 | {PASS} 通过 {passed} | {FAIL} 失败 {failed}")
    print("═" * 60)

    return failed == 0


def main():
    ok = asyncio.run(run_all())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
