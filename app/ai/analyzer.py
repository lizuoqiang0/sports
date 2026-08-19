"""
AI 赛事分析引擎 - GPT 单模型分析

仅做小球(total/under)分析。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional, Any

from openai import AsyncOpenAI

from app.config import settings
from app.core.cache import cache

logger = logging.getLogger(__name__)

VALID_PREDICTIONS = {"under", "skip"}

_PRED_ALIASES = {
    "under": "under",
    "u": "under",
    "小": "under",
    "小球": "under",
}

_BT_ALIASES = {
    "total": "total",
    "ou": "total",
    "totals": "total",
    "大小": "total",
}


def normalize_bet_type(raw) -> str:
    s = str(raw or "").strip().lower()
    if s in _BT_ALIASES:
        return _BT_ALIASES[s]
    if "大小" in s or "total" in s:
        return "total"
    return ""


def normalize_prediction(raw, *, bet_type: str = "") -> str:
    s = str(raw or "").strip().lower()
    if s in _PRED_ALIASES:
        pred = _PRED_ALIASES[s]
    elif "小球" in s or s == "小" or "under" in s:
        pred = "under"
    elif s == "skip":
        pred = "skip"
    else:
        pred = ""

    bt = normalize_bet_type(bet_type)
    if bt == "total" and pred not in ("under", "skip"):
        return ""
    if pred not in VALID_PREDICTIONS:
        return ""
    return pred


def _flatten_market_odds(market_odds: Optional[dict]) -> dict[str, float]:
    """把嵌套 markets 或扁平 odds 合成 selection->odds 映射。"""
    if not market_odds:
        return {}
    if "under" in market_odds:
        out = {}
        for k, v in market_odds.items():
            if str(k).startswith("_") or k in ("markets", "line", "total", "spread"):
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 1.0:
                out[str(k)] = f
        return out
    markets = market_odds.get("markets") if isinstance(market_odds.get("markets"), dict) else market_odds
    flat: dict[str, float] = {}
    if isinstance(markets, dict):
        for _bt, entry in markets.items():
            odds = entry.get("odds") if isinstance(entry, dict) else entry
            if not isinstance(odds, dict):
                continue
            for sel, od in odds.items():
                try:
                    f = float(od)
                except (TypeError, ValueError):
                    continue
                if f > 1.0:
                    flat[str(sel)] = f
    return flat


def _odds_for_pick(market_odds: Optional[dict], bet_type: str, prediction: str) -> float:
    if not market_odds:
        return 0.0
    markets = market_odds.get("markets") if isinstance(market_odds, dict) else None
    if isinstance(markets, dict) and bet_type in markets:
        entry = markets[bet_type] or {}
        odds = entry.get("odds") if isinstance(entry, dict) else entry
        if isinstance(odds, dict):
            try:
                return float(odds.get(prediction) or 0)
            except (TypeError, ValueError):
                return 0.0
    flat = _flatten_market_odds(market_odds)
    try:
        return float(flat.get(prediction) or 0)
    except (TypeError, ValueError):
        return 0.0


def _line_for_pick(market_odds: Optional[dict], match_info: Optional[dict], bet_type: str):
    if isinstance(market_odds, dict):
        markets = market_odds.get("markets") if isinstance(market_odds.get("markets"), dict) else None
        if markets and bet_type in markets:
            entry = markets[bet_type]
            if isinstance(entry, dict) and entry.get("line") is not None:
                try:
                    return float(entry["line"])
                except (TypeError, ValueError):
                    pass
    info = match_info or {}
    if bet_type == "total":
        return info.get("total_line") or info.get("line")
    if bet_type == "spread":
        return info.get("spread_line") or info.get("handicap_line")
    return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _recent_matches(bucket: Optional[dict], limit: int = 6) -> list[dict]:
    if not isinstance(bucket, dict):
        return []
    rows = bucket.get("matches") or []
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows[:limit]:
        if isinstance(row, dict):
            out.append(row)
    return out


class MatchAnalyzer:
    """GPT 单模型分析引擎。"""

    def __init__(self):
        self.client: Optional[AsyncOpenAI] = None
        self.model: str = ""
        self._init_client()

    def _init_client(self):
        api_key = (settings.GPT_API_KEY or "").strip()
        base_url = (settings.GPT_BASE_URL or "").strip()
        model = (settings.GPT_MODEL or "").strip()
        if not api_key or not model:
            logger.warning("GPT 未配置：缺少 API_KEY 或 MODEL")
            self.client = None
            self.model = ""
            return
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=float(settings.LLM_CLIENT_TIMEOUT_SEC),
            max_retries=0,
        )
        self.model = model
        logger.info("GPT model ready: %s (%s)", model, base_url)

    async def analyze_match(
        self,
        match_info: dict,
        historical_data: Optional[dict] = None,
        market_odds: Optional[dict] = None,
    ) -> dict:
        from app.services.fixture_key import fixture_key as _fixture_key

        fk = (match_info.get("fixture_key") or "").strip()
        if not fk:
            st = None
            raw_st = match_info.get("start_time")
            if raw_st:
                try:
                    st = (
                        datetime.fromisoformat(str(raw_st).replace("Z", "+00:00"))
                        if not isinstance(raw_st, datetime)
                        else raw_st
                    )
                except Exception:
                    st = None
            fk = _fixture_key(
                str(match_info.get("sport") or "football"),
                str(match_info.get("home_team") or ""),
                str(match_info.get("away_team") or ""),
                start_time=st,
            )
        sport = str(match_info.get("sport") or "football").lower()
        # 盘口线量化到 0.5 档进缓存 key：滚球 2.5→2.75 的小幅漂移不换 key，避免缓存全失效重打 LLM
        raw_line = (
            match_info.get("total_line")
            or match_info.get("spread_line")
            or match_info.get("line")
            or 0
        )
        try:
            line_tag = round(float(raw_line) * 2) / 2 if raw_line else ""
        except (TypeError, ValueError):
            line_tag = str(raw_line)
        cache_key = f"ai:gpt:v1:{fk}:{sport}:{line_tag}"
        try:
            cached = await cache.get_json(cache_key)
            if cached and cached.get("models_used") and not cached.get("error"):
                if cached.get("consensus_reached") and str(cached.get("prediction") or "") in VALID_PREDICTIONS:
                    logger.info(
                        "[AI分析] 缓存命中 match=%s %s vs %s | pred=%s conf=%.2f",
                        match_info.get("id"), match_info.get("home_team"), match_info.get("away_team"),
                        cached.get("prediction"), float(cached.get("confidence") or 0),
                    )
                    return cached
                if cached.get("neg_cached"):
                    logger.info(
                        "[AI分析] 负缓存命中（跳过 LLM）match=%s %s vs %s",
                        match_info.get("id"), match_info.get("home_team"), match_info.get("away_team"),
                    )
                    return cached
        except Exception as e:
            logger.debug("AI cache unavailable: %s", e)

        logger.info(
            "[AI分析] 开始分析 match=%s %s vs %s | sport=%s line=%s | 有基本面=%s 有盘口=%s",
            match_info.get("id"), match_info.get("home_team"), match_info.get("away_team"),
            sport, line_tag or "无",
            bool(historical_data), bool(market_odds),
        )

        prompt = self._build_analysis_prompt(match_info, historical_data, market_odds)
        logger.info(
            "[AI分析] Prompt 构建完成 match=%s | 长度=%d 字符 | 含analysis=%s 含trend=%s",
            match_info.get("id"), len(prompt),
            bool(isinstance(historical_data, dict) and historical_data.get("analysis")),
            bool(isinstance(historical_data, dict) and historical_data.get("trend")),
        )

        try:
            timeout = float(settings.GPT_TIMEOUT_SEC)
            raw = await asyncio.wait_for(self._call_gpt(prompt), timeout=timeout)
            content = raw.get("content", "")
            parsed = self._parse_analysis_result(content)

            # 单市场模式：仅小球，bet_type 恒 total。
            pred = normalize_prediction(parsed.get("prediction"), bet_type="total")
            bt = "total"

            if pred not in ("under", "skip"):
                logger.warning(
                    "[AI分析] GPT 返回无效结果 match=%s | bt=%s pred=%s",
                    match_info.get("id"), parsed.get("bet_type"), parsed.get("prediction"),
                )
                return self._fallback_result(
                    f"GPT返回无效结果: bet_type={parsed.get('bet_type')!r} pred={parsed.get('prediction')!r}",
                    error="invalid_result",
                )

            if pred == "skip":
                logger.info(
                    "[AI分析] GPT 判定跳过 match=%s | reasoning=%s",
                    match_info.get("id"), (parsed.get("reasoning") or "")[:120],
                )
                skip_result = {
                    "prediction": "skip",
                    "bet_type": "total",
                    "line": _line_for_pick(market_odds, match_info, "total"),
                    "confidence": 0.0,
                    "reasoning": (parsed.get("reasoning") or "数据不足，无法判断")[:500],
                    "key_factors": parsed.get("key_factors") or ["数据不足"],
                    "value_bets": [],
                    "risk_level": "high",
                    "consensus_reached": False,
                    "models_used": ["gpt"],
                    "models_failed": [],
                    "neg_cached": True,
                }
                # skip 也写负缓存：滚球晚段大量 skip，不缓存则每轮 120s 重复打 LLM
                try:
                    await cache.set_json(
                        cache_key, skip_result, ttl=settings.AI_SKIP_CACHE_TTL
                    )
                except Exception:
                    pass
                return skip_result

            try:
                conf = float(parsed.get("confidence", settings.LLM_DEFAULT_CONFIDENCE))
            except (TypeError, ValueError):
                conf = settings.LLM_DEFAULT_CONFIDENCE
            conf = max(0.0, min(conf, 1.0))

            line = parsed.get("line")
            try:
                line_f = float(line) if line is not None and line != "" else None
            except (TypeError, ValueError):
                line_f = None
            if line_f is None:
                line_f = _line_for_pick(market_odds, match_info, bt)

            latency_ms = float((raw.get("_meta") or {}).get("latency_ms") or 0)

            # 单模型模式：GPT 返回小球即视为共识达成。
            consensus_reached = pred == "under"

            analysis = {
                "prediction": pred,
                "bet_type": bt,
                "line": line_f,
                "confidence": round(conf, 4),
                "reasoning": (parsed.get("reasoning") or "")[:800],
                "market_analysis": (parsed.get("market_analysis") or "")[:800],
                "fundamental_analysis": (parsed.get("fundamental_analysis") or "")[:800],
                "core_analysis": parsed.get("core_analysis") or {},
                "fundamental_summary": (parsed.get("fundamental_summary") or "")[:600],
                "key_factors": (parsed.get("key_factors") or [])[:8],
                "value_bets": (parsed.get("value_bets") or [])[:5],
                "risk_level": parsed.get("risk_level", "medium"),
                "consensus_reached": consensus_reached,
                "models_used": ["gpt"],
                "models_failed": [],
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            }

            od = _odds_for_pick(market_odds, bt, pred)
            if od > 1:
                analysis["odds"] = od

            analysis = self._apply_context_quality_cap(analysis, historical_data)
            analysis = self._apply_signal_review(
                analysis,
                match_info=match_info,
                historical_data=historical_data,
                market_odds=market_odds,
            )
            # 单模型模式：GPT 返回小球即为最终共识。

            # 维度分析日志：核心维度逐项 + 盘口解读
            _hd = historical_data if isinstance(historical_data, dict) else {}
            _core_n = sum(1 for k in ("h2h", "home_form", "away_form", "standings", "trend") if _hd.get(k))
            _aux_n = sum(1 for k in ("analysis",) if _hd.get(k))
            _aux_n += 2 if isinstance(market_odds, dict) and market_odds.get("markets") else 1 if isinstance(market_odds, dict) and market_odds else 0
            _aux_n += 1 if isinstance(market_odds, dict) and market_odds.get("line_movements") else 0
            _ca = analysis.get("core_analysis") or {}
            logger.info(
                "[AI分析] 维度解读 match=%s %s vs %s | pred=%s conf=%.2f | 核心%d/5+辅助%d/4\n"
                "  [历史交锋] %s\n"
                "  [主队近况] %s\n"
                "  [客队近况] %s\n"
                "  [积分排名] %s\n"
                "  [走势页] %s\n"
                "  [盘口解读] %s\n"
                "  [综合结论] %s",
                match_info.get("id"),
                match_info.get("home_team", "?"),
                match_info.get("away_team", "?"),
                analysis.get("prediction"),
                float(analysis.get("confidence") or 0),
                _core_n,
                min(_aux_n, 4),
                str(_ca.get("h2h", ""))[:150],
                str(_ca.get("home_form", ""))[:150],
                str(_ca.get("away_form", ""))[:150],
                str(_ca.get("standings", ""))[:150],
                str(_ca.get("trend", ""))[:150],
                str(analysis.get("market_analysis", ""))[:200],
                str(analysis.get("fundamental_summary", ""))[:200],
            )

            if analysis.get("models_used"):
                try:
                    if analysis.get("consensus_reached"):
                        # 滚球场景比分变化快，正缓存缩短到 3 分钟（跨 1 轮 120s 轮询）
                        await cache.set_json(cache_key, analysis, ttl=180)
                    else:
                        analysis["neg_cached"] = True
                        await cache.set_json(cache_key, analysis, ttl=settings.LLM_NEG_CACHE_TTL)
                except Exception as e:
                    logger.warning("[AI分析] 缓存写入失败 match=%s: %s", match_info.get("id"), e)

            # 记录预测到 Redis
            mid = 0
            try:
                mid = int(match_info.get("id") or 0)
            except (TypeError, ValueError):
                mid = 0
            if mid > 0:
                self._record_prediction(
                    mid, pred, bt,
                    float(analysis.get("confidence") or 0),
                    float(analysis.get("odds") or 0),
                    [{"model": "gpt", "ok": True}],
                )

            return analysis

        except asyncio.TimeoutError:
            logger.error(
                "[AI分析] GPT 超时 match=%s timeout=%.0fs",
                match_info.get("id"), timeout,
            )
            return self._fallback_result("AI分析超时，改用盘口启发式", error="gpt_timeout")
        except Exception as e:
            logger.error("[AI分析] GPT 失败 match=%s: %s", match_info.get("id"), e)
            return self._fallback_result(f"AI分析暂不可用: {e}", error=str(e))

    async def _call_gpt(self, prompt: str) -> dict:
        """调用 GPT 模型，返回 content 和 _meta 信息。429 指数退避，超时不重试。"""
        if not self.client or not self.model:
            raise RuntimeError("GPT 模型未配置")
        messages = [
            {"role": "system", "content": "你是专业体育赛事分析师。只输出JSON。"},
            {"role": "user", "content": prompt},
        ]
        started = time.perf_counter()
        max_retries = 2  # 最多 2 次重试（共 3 次调用），确保总时长可控
        for attempt in range(max_retries + 1):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=settings.LLM_TEMPERATURE,
                    max_tokens=settings.LLM_MAX_TOKENS,
                )
                if not response.choices:
                    raise RuntimeError("GPT 返回空 choices（内容可能被安全过滤）")
                break
            except Exception as e:
                err_str = str(e).lower()
                # 超时：不重试（GPT 已耗时长，重试只会更长）
                if "timeout" in err_str or "timed out" in err_str or "apitimeout" in type(e).__name__.lower():
                    raise
                # 429/限流：指数退避（1s -> 2s），仅重试 2 次
                if "429" in err_str or "rate" in err_str or "ratelimit" in type(e).__name__.lower():
                    if attempt < max_retries:
                        backoff = 2 ** attempt  # 1, 2
                        logger.warning("[GPT] 429 限流，%ds 后重试 (attempt %d/%d)", backoff, attempt + 1, max_retries)
                        await asyncio.sleep(backoff)
                        continue
                    raise
                # 其他错误：最多重试 1 次
                if attempt == 0:
                    logger.debug("GPT first call failed (%s), retry", e)
                    await asyncio.sleep(0.5)
                    continue
                raise
        content = response.choices[0].message.content or ""
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info("[GPT] 调用完成 latency=%dms content_len=%d", int(elapsed_ms), len(content))
        return {"content": content, "_meta": {"latency_ms": elapsed_ms, "model": "gpt"}}

    def _apply_context_quality_cap(
        self, analysis: dict, historical_data: Optional[dict]
    ) -> dict:
        """赛前数据不足时硬性压低置信度，禁止无数据虚高。"""
        from app.services.sports_data import confidence_cap_for_quality, compute_quality

        ctx = historical_data if isinstance(historical_data, dict) else {}
        quality = ctx.get("quality") if isinstance(ctx.get("quality"), dict) else compute_quality(ctx)
        analysis["context_quality"] = quality
        analysis["context_source"] = str(quality.get("source") or ctx.get("source") or "none")
        cap = confidence_cap_for_quality(quality)
        if cap is None:
            return analysis
        try:
            conf = float(analysis.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf > cap:
            analysis["confidence_before_quality_cap"] = conf
            analysis["confidence"] = round(cap, 4)
            analysis["quality_cap"] = cap
            analysis["reasoning"] = (
                f"[赛前数据不足 completeness={quality.get('completeness')} "
                f"source={quality.get('source')} 置信度封顶≤{cap}] "
                + str(analysis.get("reasoning") or "")
            )
        else:
            analysis["quality_cap"] = cap
        return analysis

    def _apply_signal_review(
        self,
        analysis: dict,
        *,
        match_info: Optional[dict],
        historical_data: Optional[dict],
        market_odds: Optional[dict],
    ) -> dict:
        """用盘口 + 基本面的结构化信号二次校准置信度。"""
        review = self._build_signal_review(
            match_info=match_info,
            historical_data=historical_data,
            market_odds=market_odds,
            analysis=analysis,
        )
        analysis["signal_review"] = review
        if not review:
            return analysis

        conf_before = _to_float(analysis.get("confidence"), 0.0)
        adjusted = conf_before + _to_float(review.get("confidence_delta"), 0.0)
        cap = review.get("confidence_cap")
        floor = review.get("confidence_floor")
        if cap is not None:
            adjusted = min(adjusted, _to_float(cap, adjusted))
        if floor is not None:
            adjusted = max(adjusted, _to_float(floor, adjusted))
        adjusted = max(0.0, min(0.99, adjusted))
        analysis["confidence_before_signal_review"] = round(conf_before, 4)
        analysis["confidence"] = round(adjusted, 4)

        summary = str(review.get("summary") or "").strip()
        if summary:
            analysis["reasoning"] = f"[结构化复核] {summary} | {str(analysis.get('reasoning') or '')}"[:900]
        return analysis

    @staticmethod
    def _elapsed_minutes(match_info: dict) -> Optional[float]:
        """获取已进行分钟数（篮球倒计时自动转换为已进行时间）。"""
        from app.services.bookmakers.match_live import match_elapsed_seconds
        sport = str(match_info.get("sport") or "").strip().lower()
        period = str(match_info.get("period") or "").strip()
        clock = str(match_info.get("clock") or "").strip()
        secs = match_elapsed_seconds(sport=sport, period=period, clock=clock)
        if secs is not None and secs > 0:
            return round(secs / 60.0, 2)
        # 足球回退到直接解析
        from app.services.bookmakers.match_live import parse_match_clock_minutes
        return parse_match_clock_minutes(clock)

    @staticmethod
    def _analysis_page_signal(analysis: Any) -> dict[str, Any]:
        if not isinstance(analysis, dict):
            return {"supportive": False, "points": 0, "reason": ""}
        tables = analysis.get("analysis_tables") if isinstance(analysis.get("analysis_tables"), list) else []
        injuries = analysis.get("injuries") if isinstance(analysis.get("injuries"), list) else []
        features = analysis.get("features") if isinstance(analysis.get("features"), list) else []
        compare = analysis.get("compare") if isinstance(analysis.get("compare"), list) else []
        table_count = len(tables)
        section_count = sum(1 for x in (injuries, features, compare) if x)
        if table_count <= 0 and section_count <= 0:
            return {"supportive": False, "points": 0, "reason": ""}
        points = 0
        reasons: list[str] = []
        if table_count >= 4:
            points += 2
            reasons.append("分析页结构化表格较完整")
        elif table_count >= 1:
            points += 1
            reasons.append("已读取分析页结构化表")
        if section_count >= 2:
            points += 1
            reasons.append("伤停/特征/对比维度覆盖较好")
        return {"supportive": True, "points": points, "reason": "；".join(reasons)}

    @staticmethod
    def _trend_page_signal(trend: Any) -> dict[str, Any]:
        if not isinstance(trend, dict):
            return {"supportive": False, "points": 0, "reason": ""}
        tables = trend.get("tables") if isinstance(trend.get("tables"), list) else []
        initial_odds = trend.get("initial_odds") if isinstance(trend.get("initial_odds"), list) else []
        if not tables and not initial_odds:
            return {"supportive": False, "points": 0, "reason": ""}
        points = 1
        if len(tables) >= 2 or len(initial_odds) >= 2:
            points += 1
        return {
            "supportive": True,
            "points": points,
            "reason": "走势页已抓取初指/赛前指数",
        }

    @staticmethod
    def _market_triad_status(
        market_odds: Optional[dict],
        historical_data: Optional[dict],
    ) -> dict[str, Any]:
        markets = market_odds.get("markets") if isinstance(market_odds, dict) and isinstance(market_odds.get("markets"), dict) else {}
        total_market = markets.get("total") if isinstance(markets.get("total"), dict) else {}
        opening = total_market.get("opening") if isinstance(total_market.get("opening"), dict) else {}
        current_odds = total_market.get("odds") if isinstance(total_market.get("odds"), dict) else {}
        current_line = total_market.get("line")
        opening_line = opening.get("line")
        has_opening = opening_line not in (None, "")
        has_live_market = bool(current_odds) and current_line not in (None, "")

        ctx = historical_data if isinstance(historical_data, dict) else {}
        quality = ctx.get("quality") if isinstance(ctx.get("quality"), dict) else {}
        completeness = _to_float(quality.get("completeness"), 0.0)
        has_fundamentals = (
            str(quality.get("source") or ctx.get("source") or "none").strip().lower() != "none"
            and completeness >= 0.55
            and bool((ctx.get("trend") or {}).get("initial_odds") or (ctx.get("trend") or {}).get("tables"))
            and bool((ctx.get("home_form") or {}).get("matches") or (ctx.get("away_form") or {}).get("matches"))
            and bool((ctx.get("standings") or {}).get("home") or (ctx.get("standings") or {}).get("away"))
        )
        return {
            "has_opening": has_opening,
            "has_live_market": has_live_market,
            "has_fundamentals": has_fundamentals,
            "triad_ready": bool(has_opening and has_live_market and has_fundamentals),
            "opening_line": _to_float(opening_line, None),
            "current_line": _to_float(current_line, None),
            "completeness": completeness,
        }

    @staticmethod
    def _opening_live_signal(
        market_odds: Optional[dict],
        *,
        selection: str,
        bet_type: str,
    ) -> dict[str, Any]:
        if bet_type != "total":
            return {"supportive": False, "conflict": False, "points": 0, "reason": ""}
        markets = market_odds.get("markets") if isinstance(market_odds, dict) and isinstance(market_odds.get("markets"), dict) else {}
        total_market = markets.get("total") if isinstance(markets.get("total"), dict) else {}
        opening = total_market.get("opening") if isinstance(total_market.get("opening"), dict) else {}
        current_odds = total_market.get("odds") if isinstance(total_market.get("odds"), dict) else {}

        try:
            open_line = float(opening.get("line")) if opening.get("line") not in (None, "") else None
        except (TypeError, ValueError):
            open_line = None
        try:
            cur_line = float(total_market.get("line")) if total_market.get("line") not in (None, "") else None
        except (TypeError, ValueError):
            cur_line = None
        if open_line is None or cur_line is None or not current_odds:
            return {"supportive": False, "conflict": False, "points": 0, "reason": ""}

        line_delta = cur_line - open_line
        under_odds = _to_float(current_odds.get("under"), 0.0)

        supportive = False
        conflict = False
        points = 0
        reasons: list[str] = []
        if selection == "under":
            if line_delta <= -0.25:
                supportive = True
                points += 2
                reasons.append(f"初指{open_line:.2f}降至即时{cur_line:.2f}")
            elif line_delta >= 0.25:
                conflict = True
                reasons.append(f"盘口从{open_line:.2f}升到{cur_line:.2f}")

        if selection == "under" and under_odds <= 1.0:
            conflict = True
            reasons.append("小球赔率无效")

        return {
            "supportive": supportive,
            "conflict": conflict,
            "points": points,
            "reason": "；".join(reasons),
        }

    @staticmethod
    def _live_pace_signal(
        match_info: Optional[dict],
        *,
        selection: str,
        bet_type: str,
        line: Optional[float],
    ) -> dict[str, Any]:
        if bet_type != "total" or line is None:
            return {"supportive": False, "conflict": False, "points": 0, "reason": ""}
        info = match_info if isinstance(match_info, dict) else {}
        hs = info.get("home_score")
        aws = info.get("away_score")
        if hs is None or aws is None:
            return {"supportive": False, "conflict": False, "points": 0, "reason": ""}

        mins = MatchAnalyzer._elapsed_minutes(info)
        if mins is None or mins <= 0:
            return {"supportive": False, "conflict": False, "points": 0, "reason": ""}
        current_total = _to_float(hs, 0.0) + _to_float(aws, 0.0)
        sport = str(info.get("sport") or "").strip().lower()
        full_minutes = 48.0 if sport == "basketball" else 90.0
        expected_total = float(line) * min(mins, full_minutes) / full_minutes
        delta = current_total - expected_total
        if sport == "basketball":
            support_delta = max(6.0, float(line) * 0.04)
            conflict_delta = max(8.0, float(line) * 0.05)
        else:
            support_delta = 0.45
            conflict_delta = 0.55
        if selection == "under" and delta <= -support_delta:
            return {"supportive": True, "conflict": False, "points": 2, "reason": f"实时节奏低于盘口预期 {abs(delta):.2f} 球"}
        if selection == "under" and delta >= conflict_delta:
            return {"supportive": False, "conflict": True, "points": 0, "reason": f"实时节奏快于盘口预期 {delta:.2f} 球"}
        return {"supportive": False, "conflict": False, "points": 0, "reason": ""}

    @staticmethod
    def _anti_chase_signal(
        match_info: Optional[dict],
        market_odds: Optional[dict],
        historical_data: Optional[dict],
        *,
        selection: str,
        bet_type: str,
    ) -> dict[str, Any]:
        if bet_type != "total":
            return {"supportive": False, "conflict": False, "points": 0, "reason": ""}
        markets = market_odds.get("markets") if isinstance(market_odds, dict) and isinstance(market_odds.get("markets"), dict) else {}
        total_market = markets.get("total") if isinstance(markets.get("total"), dict) else {}
        opening = total_market.get("opening") if isinstance(total_market.get("opening"), dict) else {}
        try:
            open_line = float(opening.get("line")) if opening.get("line") not in (None, "") else None
        except (TypeError, ValueError):
            open_line = None
        try:
            cur_line = float(total_market.get("line")) if total_market.get("line") not in (None, "") else None
        except (TypeError, ValueError):
            cur_line = None
        if open_line is None or cur_line is None:
            return {"supportive": False, "conflict": False, "points": 0, "reason": ""}
        delta = cur_line - open_line
        ctx = historical_data if isinstance(historical_data, dict) else {}
        quality = ctx.get("quality") if isinstance(ctx.get("quality"), dict) else {}
        completeness = _to_float(quality.get("completeness"), 0.0)

        info = match_info if isinstance(match_info, dict) else {}
        mins = MatchAnalyzer._elapsed_minutes(info)

        if selection == "under" and delta <= -0.5:
            if (mins is not None and mins <= 60) or completeness < 0.75:
                return {
                    "supportive": False,
                    "conflict": True,
                    "points": 0,
                    "reason": f"盘口已从{open_line:.2f}大幅降到{cur_line:.2f}，不追低位小球",
                }
        return {"supportive": False, "conflict": False, "points": 0, "reason": ""}

    def _build_signal_review(
        self,
        *,
        match_info: Optional[dict],
        historical_data: Optional[dict],
        market_odds: Optional[dict],
        analysis: Optional[dict],
    ) -> dict[str, Any]:
        info = match_info if isinstance(match_info, dict) else {}
        ctx = historical_data if isinstance(historical_data, dict) else {}
        ana = analysis if isinstance(analysis, dict) else {}
        bet_type = str(ana.get("bet_type") or info.get("bet_type") or "total").strip().lower()
        selection = str(ana.get("prediction") or "").strip().lower()
        sport = str(info.get("sport") or "").strip().lower()
        confidence = _to_float(ana.get("confidence"), 0.0)
        line = _line_for_pick(market_odds, info, bet_type)
        quality = ana.get("context_quality") if isinstance(ana.get("context_quality"), dict) else {}
        fields_present = {
            str(x).strip().lower()
            for x in (quality.get("fields_present") or [])
            if str(x).strip()
        }
        completeness = _to_float(quality.get("completeness"), 0.0)
        source = str(quality.get("source") or ctx.get("source") or "none").strip().lower()
        stat_signals = self._build_statistical_signals(ctx, market_odds, info, ctx.get("h2h") if isinstance(ctx.get("h2h"), dict) else None)
        analysis_signal = self._analysis_page_signal(ctx.get("analysis"))
        trend_signal = self._trend_page_signal(ctx.get("trend"))
        triad_status = self._market_triad_status(market_odds, ctx)
        opening_live_signal = self._opening_live_signal(market_odds, selection=selection, bet_type=bet_type)
        pace_signal = self._live_pace_signal(info, selection=selection, bet_type=bet_type, line=line)
        anti_chase_signal = self._anti_chase_signal(info, market_odds, ctx, selection=selection, bet_type=bet_type)

        market_points = 0
        fundamental_points = 0
        conflict_points = 0
        support_reasons: list[str] = []
        conflict_reasons: list[str] = []

        move = None
        if isinstance(market_odds, dict):
            moves = market_odds.get("line_movements") if isinstance(market_odds.get("line_movements"), dict) else {}
            move = moves.get(bet_type) if isinstance(moves.get(bet_type), dict) else None
            if move is None:
                markets = market_odds.get("markets") if isinstance(market_odds.get("markets"), dict) else {}
                mkt = markets.get(bet_type) if isinstance(markets.get(bet_type), dict) else {}
                if isinstance(mkt.get("line_movement"), dict):
                    move = mkt.get("line_movement")
        move_alignment = self._movement_alignment(bet_type=bet_type, selection=selection, movement=move)
        if move_alignment == "supportive":
            market_points += 2
            support_reasons.append("盘口变动支持当前方向")
        elif move_alignment == "neutral":
            market_points += 1
        elif move_alignment in ("adverse", "conflict"):
            conflict_points += 2 if move_alignment == "conflict" else 1
            conflict_reasons.append("盘口变动与当前方向不一致")

        if move and isinstance(move.get("change_count"), (int, float)):
            market_points += 1

        if opening_live_signal["supportive"]:
            market_points += int(opening_live_signal.get("points") or 0)
            if opening_live_signal.get("reason"):
                support_reasons.append(opening_live_signal["reason"])
        elif opening_live_signal["conflict"]:
            conflict_points += 2
            if opening_live_signal.get("reason"):
                conflict_reasons.append(opening_live_signal["reason"])

        if source not in ("", "none"):
            if completeness >= 0.65:
                fundamental_points += 3
                support_reasons.append("基本面维度较完整")
            elif completeness >= 0.50:
                fundamental_points += 2
            elif completeness >= 0.30:
                fundamental_points += 1

        form_signal = self._recent_form_signal(ctx, sport=sport, selection=selection, bet_type=bet_type, line=line)
        if form_signal["supportive"]:
            fundamental_points += 2
            support_reasons.append(form_signal["reason"])
        elif form_signal["conflict"]:
            conflict_points += 1
            conflict_reasons.append(form_signal["reason"])

        h2h_signal = self._h2h_signal(ctx.get("h2h"), sport=sport, selection=selection, bet_type=bet_type, line=line)
        if h2h_signal["supportive"]:
            fundamental_points += 1
            support_reasons.append(h2h_signal["reason"])
        elif h2h_signal["conflict"]:
            conflict_points += 1
            conflict_reasons.append(h2h_signal["reason"])

        standings_signal = self._standings_signal(ctx.get("standings"), sport=sport, selection=selection, bet_type=bet_type, line=line)
        if standings_signal["supportive"]:
            fundamental_points += 1
            support_reasons.append(standings_signal["reason"])
        elif standings_signal["conflict"]:
            conflict_points += 1
            conflict_reasons.append(standings_signal["reason"])

        stage_signal = self._stage_signal(info, selection=selection, bet_type=bet_type)
        if stage_signal["supportive"]:
            market_points += 1
            support_reasons.append(stage_signal["reason"])
        elif stage_signal["conflict"]:
            conflict_points += 1
            conflict_reasons.append(stage_signal["reason"])

        if pace_signal["supportive"]:
            market_points += int(pace_signal.get("points") or 0)
            if pace_signal.get("reason"):
                support_reasons.append(pace_signal["reason"])
        elif pace_signal["conflict"]:
            conflict_points += 2
            if pace_signal.get("reason"):
                conflict_reasons.append(pace_signal["reason"])

        if anti_chase_signal["conflict"]:
            conflict_points += 2
            if anti_chase_signal.get("reason"):
                conflict_reasons.append(anti_chase_signal["reason"])

        if analysis_signal["supportive"]:
            fundamental_points += int(analysis_signal.get("points") or 0)
            if analysis_signal.get("reason"):
                support_reasons.append(analysis_signal["reason"])
        if trend_signal["supportive"]:
            market_points += int(trend_signal.get("points") or 0)
            if trend_signal.get("reason"):
                support_reasons.append(trend_signal["reason"])

        if {"home_form", "away_form"}.issubset(fields_present):
            fundamental_points += 1
        if fields_present.intersection({"h2h", "standings"}):
            fundamental_points += 1

        confidence_delta = 0.0
        confidence_cap = None
        confidence_floor = None
        triad_ready = bool(triad_status.get("triad_ready"))
        edge_score = market_points + fundamental_points - conflict_points
        if sport == "basketball" and selection == "under":
            if not triad_ready:
                confidence_delta -= 0.22
                confidence_cap = 0.44
                missing_bits = []
                if not triad_status.get("has_opening"):
                    missing_bits.append("初指")
                if not triad_status.get("has_live_market"):
                    missing_bits.append("实时盘口")
                if not triad_status.get("has_fundamentals"):
                    missing_bits.append("基本面")
                if missing_bits:
                    conflict_reasons.append("篮球三重门禁缺失:" + "/".join(missing_bits))
            elif conflict_points >= 2:
                confidence_delta -= 0.14
                confidence_cap = 0.50
            elif market_points < 3 or fundamental_points < 3:
                confidence_delta -= 0.08
                confidence_cap = 0.56
            elif market_points >= 5 and fundamental_points >= 4 and conflict_points == 0 and edge_score >= 8:
                confidence_delta += 0.03
                confidence_floor = max(confidence, 0.60)
        elif not triad_ready:
            confidence_delta -= 0.18
            confidence_cap = 0.49
            missing_bits = []
            if not triad_status.get("has_opening"):
                missing_bits.append("初指")
            if not triad_status.get("has_live_market"):
                missing_bits.append("实时盘口")
            if not triad_status.get("has_fundamentals"):
                missing_bits.append("基本面")
            if missing_bits:
                conflict_reasons.append("三重门禁缺失:" + "/".join(missing_bits))
        elif market_points >= 5 and fundamental_points >= 5 and conflict_points == 0 and edge_score >= 10:
            confidence_delta += 0.05
            confidence_floor = max(confidence, 0.62)
        elif market_points >= 4 and fundamental_points >= 4 and conflict_points <= 1 and edge_score >= 7:
            confidence_delta += 0.02
        elif conflict_points >= 3:
            confidence_delta -= 0.12
            confidence_cap = 0.52
        elif conflict_points == 2:
            confidence_delta -= 0.08
            confidence_cap = 0.58
        elif market_points <= 1 or fundamental_points <= 1:
            confidence_delta -= 0.05
            confidence_cap = 0.60

        if completeness < 0.40 and confidence_cap is None:
            confidence_cap = 0.62
        if source in ("", "none"):
            no_source_cap = 0.54 if sport == "basketball" and selection == "under" else 0.58
            confidence_cap = min(no_source_cap, confidence_cap) if confidence_cap is not None else no_source_cap

        verdict = "supportive"
        if not triad_ready:
            verdict = "weak"
        elif conflict_points >= 3:
            verdict = "conflict"
        elif conflict_points > market_points:
            verdict = "mixed"
        elif edge_score < 6:
            verdict = "weak"

        summary_bits = []
        if support_reasons:
            summary_bits.append("支持:" + " / ".join(support_reasons[:3]))
        if conflict_reasons:
            summary_bits.append("冲突:" + " / ".join(conflict_reasons[:3]))
        summary = "；".join(summary_bits)

        return {
            "verdict": verdict,
            "market_points": int(market_points),
            "fundamental_points": int(fundamental_points),
            "conflict_points": int(conflict_points),
            "move_alignment": move_alignment,
            "support_reasons": support_reasons[:6],
            "conflict_reasons": conflict_reasons[:6],
            "triad_ready": triad_ready,
            "triad_status": triad_status,
            "edge_score": int(edge_score),
            "confidence_delta": round(confidence_delta, 4),
            "confidence_cap": confidence_cap,
            "confidence_floor": confidence_floor,
            "summary": summary,
        }

    @staticmethod
    def _movement_alignment(*, bet_type: str, selection: str, movement: Optional[dict]) -> str:
        if not isinstance(movement, dict):
            return "unknown"
        signals: list[str] = []
        direction = str(movement.get("direction") or "").strip().lower()
        odds_delta = movement.get("odds_delta") if isinstance(movement.get("odds_delta"), dict) else {}
        if bet_type == "total" and selection == "under":
            if direction == "line_down":
                signals.append("supportive")
            elif direction == "line_up":
                signals.append("adverse")
        try:
            sel_delta = float(odds_delta.get(selection)) if selection in odds_delta else None
        except (TypeError, ValueError):
            sel_delta = None
        if sel_delta is not None:
            if sel_delta <= -0.01:
                signals.append("supportive")
            elif sel_delta >= 0.01:
                signals.append("adverse")
        if "supportive" in signals and "adverse" in signals:
            return "conflict"
        if "adverse" in signals:
            return "adverse"
        if "supportive" in signals:
            return "supportive"
        return "neutral"

    @staticmethod
    def _recent_form_signal(
        ctx: dict[str, Any],
        *,
        sport: str,
        selection: str,
        bet_type: str,
        line: Optional[float],
    ) -> dict[str, Any]:
        home_rows = _recent_matches(ctx.get("home_form"))
        away_rows = _recent_matches(ctx.get("away_form"))
        home_totals = [
            _to_float(r.get("home_goals"), -1) + _to_float(r.get("away_goals"), -1)
            for r in home_rows
            if r.get("home_goals") is not None and r.get("away_goals") is not None
        ]
        away_totals = [
            _to_float(r.get("home_goals"), -1) + _to_float(r.get("away_goals"), -1)
            for r in away_rows
            if r.get("home_goals") is not None and r.get("away_goals") is not None
        ]
        avg_total = 0.0
        if home_totals or away_totals:
            vals = [v for v in home_totals + away_totals if v >= 0]
            if vals:
                avg_total = sum(vals) / len(vals)

        supportive = False
        conflict = False
        reason = ""
        if bet_type == "total" and line is not None and avg_total > 0:
            sport_l = str(sport or "").strip().lower()
            if sport_l == "basketball":
                support_gap = 6.0
                conflict_gap = 4.5
            else:
                support_gap = 0.35
                conflict_gap = 0.25
            if selection == "under" and avg_total <= float(line) - support_gap:
                supportive = True
                reason = f"近况总进球均值 {avg_total:.2f} 低于盘口 {float(line):.2f}"
            elif selection == "under" and avg_total >= float(line) + conflict_gap:
                conflict = True
                reason = f"近况总进球均值 {avg_total:.2f} 偏高"
        return {"supportive": supportive, "conflict": conflict, "reason": reason}

    @staticmethod
    def _h2h_signal(
        h2h: Any,
        *,
        sport: str,
        selection: str,
        bet_type: str,
        line: Optional[float],
    ) -> dict[str, Any]:
        if not isinstance(h2h, dict):
            return {"supportive": False, "conflict": False, "reason": ""}
        summary = h2h.get("summary") or {}
        played = int(_to_float(summary.get("played"), 0))
        if played <= 0:
            return {"supportive": False, "conflict": False, "reason": ""}
        supportive = False
        conflict = False
        reason = ""
        if bet_type == "total" and line is not None:
            avg_total = _to_float(summary.get("avg_total_goals"), 0.0)
            if avg_total > 0:
                margin = 5.0 if str(sport or "").strip().lower() == "basketball" else 0.25
                if selection == "under" and avg_total <= float(line) - margin:
                    supportive = True
                    reason = f"交锋总进球均值 {avg_total:.2f} 偏小"
        return {"supportive": supportive, "conflict": conflict, "reason": reason}

    @staticmethod
    def _standings_signal(
        standings: Any,
        *,
        sport: str,
        selection: str,
        bet_type: str,
        line: Optional[float],
    ) -> dict[str, Any]:
        if not isinstance(standings, dict):
            return {"supportive": False, "conflict": False, "reason": ""}
        home = standings.get("home") if isinstance(standings.get("home"), dict) else {}
        away = standings.get("away") if isinstance(standings.get("away"), dict) else {}
        supportive = False
        conflict = False
        reason = ""
        if bet_type == "total":
            home_played = max(_to_float(home.get("played"), 0.0), 1.0)
            away_played = max(_to_float(away.get("played"), 0.0), 1.0)
            home_gf = _to_float(home.get("goals_for"), 0.0) / home_played
            away_gf = _to_float(away.get("goals_for"), 0.0) / away_played
            home_ga = _to_float(home.get("goals_against"), 0.0) / home_played
            away_ga = _to_float(away.get("goals_against"), 0.0) / away_played
            expected_total = (home_gf + away_ga + away_gf + home_ga) / 2.0
            if line is not None and expected_total > 0:
                margin = 5.0 if str(sport or "").strip().lower() == "basketball" else 0.25
                if selection == "under" and expected_total <= float(line) - margin:
                    supportive = True
                    reason = f"联赛攻防推导总分 {expected_total:.2f} 偏小"
        return {"supportive": supportive, "conflict": conflict, "reason": reason}

    @staticmethod
    def _stage_signal(
        match_info: dict[str, Any],
        *,
        selection: str,
        bet_type: str,
    ) -> dict[str, Any]:
        if bet_type != "total":
            return {"supportive": False, "conflict": False, "reason": ""}

        sport = str(match_info.get("sport") or "").strip().lower()
        mins = MatchAnalyzer._elapsed_minutes(match_info)
        supportive = False
        conflict = False
        reason = ""
        if mins is None:
            return {"supportive": False, "conflict": False, "reason": ""}
        if sport in ("football", "soccer"):
            if selection == "under" and mins < 25:
                supportive = True
                reason = "比赛早段节奏通常更谨慎"
        elif sport == "basketball":
            if selection == "under" and mins >= 44:
                conflict = True
                reason = "篮球最后4分钟犯规与罚球波动大，不利于小分"
        return {"supportive": supportive, "conflict": conflict, "reason": reason}

    def _compact_historical_data(self, data: Optional[dict]) -> Optional[dict]:
        """压缩全量数据：截断 h2h/form 只保留最近 N 场，trend 只保留 text，analysis 截断，prompt 控制 8KB。"""
        if not isinstance(data, dict):
            return data
        try:
            result = copy.deepcopy(data)
        except Exception:
            result = dict(data)

        h2h_max = int(settings.H2H_MAX_MATCHES)
        form_max = int(settings.FORM_MAX_MATCHES)

        # h2h: 只保留最近 N 场，保留进球数据
        h2h = result.get("h2h")
        if isinstance(h2h, dict):
            # summary 摘要由统计信号派生（h2h_baseline），原始块只留 matches 明细
            h2h.pop("summary", None)
            matches = h2h.get("matches")
            if isinstance(matches, list) and len(matches) > h2h_max:
                h2h["matches"] = matches[:h2h_max]
            for m in (h2h.get("matches") or []):
                if isinstance(m, dict):
                    for k in list(m.keys()):
                        if k not in ("date", "home", "away", "score", "result", "home_goals", "away_goals", "competition"):
                            m.pop(k, None)

        # home_form / away_form: 只保留最近 N 场，保留进球和对手数据
        for fk in ("home_form", "away_form"):
            fd = result.get(fk)
            if isinstance(fd, dict):
                # summary 摘要由统计信号派生（home/away_form_stats），原始块只留明细
                fd.pop("summary", None)
                matches = fd.get("matches")
                if isinstance(matches, list) and len(matches) > form_max:
                    fd["matches"] = matches[:form_max]
                for m in (fd.get("matches") or []):
                    if isinstance(m, dict):
                        for k in list(m.keys()):
                            if k not in ("date", "home", "away", "result", "score", "home_goals", "away_goals", "competition"):
                                m.pop(k, None)

        # trend: 保留前几组表格的结构化摘要
        trend = result.get("trend")
        if isinstance(trend, dict):
            tables = trend.get("tables")
            if isinstance(tables, list) and tables:
                compact_tables = []
                for t in tables[:3]:
                    if not isinstance(t, dict):
                        continue
                    compact_tables.append({
                        "header": (t.get("header") or [])[:8],
                        "rows": (t.get("rows") or [])[:6],
                        "text": str(t.get("text") or "")[:600],
                    })
                trend["tables"] = compact_tables
            initial_odds = trend.get("initial_odds")
            if isinstance(initial_odds, list) and initial_odds:
                compact_initial = []
                for t in initial_odds[:2]:
                    if not isinstance(t, dict):
                        continue
                    compact_initial.append({
                        "header": (t.get("header") or [])[:8],
                        "rows": (t.get("rows") or [])[:5],
                        "text": str(t.get("text") or "")[:400],
                    })
                trend["initial_odds"] = compact_initial
            trend.pop("raw", None)

        # analysis: 保留结构化摘要而非压成字符串
        analysis = result.get("analysis")
        if isinstance(analysis, dict):
            compact_analysis: dict[str, Any] = {}
            for key in ("injuries", "features", "compare", "analysis_tables"):
                value = analysis.get(key)
                if isinstance(value, list) and value:
                    compact_list = []
                    for item in value[:3]:
                        if not isinstance(item, dict):
                            continue
                        compact_list.append({
                            "header": (item.get("header") or [])[:8],
                            "rows": (item.get("rows") or [])[:6],
                            "text": str(item.get("text") or "")[:500],
                        })
                    if compact_list:
                        compact_analysis[key] = compact_list
            compact_analysis["summary"] = {
                "injuries_tables": len(analysis.get("injuries") or []),
                "features_tables": len(analysis.get("features") or []),
                "compare_tables": len(analysis.get("compare") or []),
            }
            result["analysis"] = compact_analysis

        # standings: 保留解析器返回的所有有效字段
        standings = result.get("standings")
        if isinstance(standings, dict):
            for sk in ("home", "away"):
                sd = standings.get(sk)
                if isinstance(sd, dict):
                    for k in list(sd.keys()):
                        if k not in ("team", "scope", "played", "win", "draw", "lose", "goals_for", "goals_against", "points", "win_rate", "rank"):
                            sd.pop(k, None)

        return result

    def _build_analysis_prompt(
        self,
        match_info: dict,
        historical_data: Optional[dict],
        market_odds: Optional[dict],
    ) -> str:
        # 压缩全量数据，控制 prompt 在 8KB 以内
        historical_data = self._compact_historical_data(historical_data)
        sport = str(match_info.get("sport") or "football").lower()

        prompt = f"""你是一位顶级体育赛事分析师，拥有20年从业经验。请分析以下赛事并给出专业预测。

## 赛事信息
- 运动类型: {match_info.get('sport', '未知')}
- 联赛: {match_info.get('league', '未知')}
- 主队: {match_info.get('home_team', '未知')}
- 客队: {match_info.get('away_team', '未知')}
- 比赛时间: {match_info.get('start_time', '未知')}
- 场地: {match_info.get('venue', '未知')}
"""
        h2h_block = None
        home_form = None
        away_form = None
        ctx_source = ""
        # 核心维度数据收集
        dim_data: dict[str, Any] = {}
        if isinstance(historical_data, dict):
            ctx_source = str(historical_data.get("source") or match_info.get("context_source") or "")
            if "h2h" in historical_data or "home_form" in historical_data:
                h2h_block = historical_data.get("h2h")
                home_form = historical_data.get("home_form")
                away_form = historical_data.get("away_form")
            else:
                h2h_block = historical_data
            if match_info.get("recent_form") and isinstance(match_info.get("recent_form"), dict):
                rf = match_info["recent_form"]
                home_form = home_form or rf.get("home")
                away_form = away_form or rf.get("away")

            # 收集核心分析维度
            dim_data["历史交锋"] = h2h_block
            dim_data["球队近期状态"] = {"home": home_form, "away": away_form} if (home_form or away_form) else None
            dim_data["联赛积分排名"] = historical_data.get("standings") or None
            dim_data["分析页"] = historical_data.get("analysis") or None
            dim_data["走势页"] = historical_data.get("trend") or None

        # 盘口维度
        has_markets = isinstance(market_odds, dict) and bool(market_odds.get("markets") or any(
            k in (market_odds or {}) for k in ("moneyline", "spread", "total")
        ))
        has_line_moves = isinstance(market_odds, dict) and bool(market_odds.get("line_movements"))
        dim_data["亚洲盘"] = has_markets
        dim_data["盘口变化"] = has_line_moves

        # 构建维度分析框架（始终列出核心维度，标注有无数据）
        dim_lines = []
        dim_available = 0
        dim_names = [
            "历史交锋", "球队近期状态", "联赛积分排名", "分析页", "走势页", "亚洲盘", "盘口变化",
        ]
        for dn in dim_names:
            dv = dim_data.get(dn)
            if dv:
                dim_available += 1
                dim_lines.append(f"  [{dn}] 有数据")
            else:
                dim_lines.append(f"  [{dn}] 数据缺失")
        dim_total = len(dim_names)
        dim_summary = f"（{dim_available}/{dim_total} 维度有数据）"

        if h2h_block:
            prompt += f"\n## 历史交锋记录\n{json.dumps(h2h_block, ensure_ascii=False, separators=(',', ':'))}\n"
        if home_form:
            prompt += (
                f"\n## 主队近况明细（{match_info.get('home_team', '主队')}）\n"
                f"{json.dumps(home_form, ensure_ascii=False, separators=(',', ':'))}\n"
            )
        if away_form:
            prompt += (
                f"\n## 客队近况明细（{match_info.get('away_team', '客队')}）\n"
                f"{json.dumps(away_form, ensure_ascii=False, separators=(',', ':'))}\n"
            )
        if isinstance(historical_data, dict):
            standings = historical_data.get("standings") or {}
            quality = historical_data.get("quality") or {}
            dims_present = historical_data.get("dimensions_present") or []
            dims_missing = historical_data.get("dimensions_missing") or []
            if standings.get("home") or standings.get("away"):
                prompt += f"\n## 联赛积分排名\n{json.dumps(standings, ensure_ascii=False, separators=(',', ':'))}\n"

            if quality or dims_present or dims_missing:
                prompt += (
                    f"\n## 数据维度覆盖\nsource={quality.get('source') if isinstance(quality, dict) else ''} "
                    f"completeness={(quality or {}).get('completeness') if isinstance(quality, dict) else ''} "
                    f"present={dims_present or (quality or {}).get('fields_present')} "
                    f"missing={dims_missing}\n"
                )

        # 分析页 / 走势页额外数据
        if isinstance(historical_data, dict):
            analysis_data = historical_data.get("analysis")
            if analysis_data:
                prompt += f"\n## 分析页额外数据\n{json.dumps(analysis_data, ensure_ascii=False, separators=(',', ':'))}\n"
            trend_data = historical_data.get("trend")
            if trend_data:
                prompt += f"\n## 走势页数据（各公司初指）\n{json.dumps(trend_data, ensure_ascii=False, separators=(',', ':'))}\n"

        # 统计信号汇总：预计算量化指标注入 prompt，减少 LLM 主观偏差
        stat_signals = self._build_statistical_signals(
            historical_data, market_odds, match_info, h2h_block
        )
        if stat_signals:
            prompt += f"\n## 统计信号（预计算量化指标，须结合分析）\n{json.dumps(stat_signals, ensure_ascii=False, separators=(',', ':'))}\n"

        score_hint = ""
        if match_info.get("home_score") is not None and match_info.get("away_score") is not None:
            score_hint = f"\n- 当前比分: {match_info.get('home_score')}-{match_info.get('away_score')}"
        if match_info.get("period") or match_info.get("clock"):
            score_hint += f"\n- 比赛进程: {match_info.get('period') or ''} {match_info.get('clock') or ''}".rstrip()

        markets_block = None
        if isinstance(market_odds, dict) and isinstance(market_odds.get("markets"), dict):
            markets_block = market_odds["markets"]
        elif isinstance(market_odds, dict) and any(
            k in market_odds for k in ("moneyline", "spread", "total")
        ):
            markets_block = {
                k: market_odds[k]
                for k in ("moneyline", "spread", "total")
                if k in market_odds
            }

        total_line = match_info.get("total_line") or match_info.get("line")
        prompt += (
            "\n## 投注市场（全场小球 / 上下半场小球）\n"
            "- 仅分析全场小球(total)和上下半场小球(first_half_total/second_half_total)的 under 方向\n"
            "- 其他玩法(胜负/让球/特殊盘/串关)一律不分析不下注\n"
            f"- 盘口线 total_line: {total_line if total_line is not None else '未知'}"
            f"{score_hint}\n"
        )
        flat = _flatten_market_odds(market_odds)
        if markets_block and "total" in markets_block:
            # markets_block['total'] 已含 opening 初指与变盘明细，不再重复展开独立摘要
            prompt += f"- 当前小球盘口（含 opening/变盘）: {json.dumps(markets_block['total'], ensure_ascii=False, separators=(',', ':'))}\n"
        elif flat:
            prompt += f"- 当前小球赔率: {json.dumps(flat, ensure_ascii=False, separators=(',', ':'))}\n"
        # 实时比分分析：计算进球节奏和剩余需求
        score_analysis = ""
        if match_info.get("home_score") is not None and match_info.get("away_score") is not None:
            hs = int(match_info.get("home_score") or 0)
            aws = int(match_info.get("away_score") or 0)
            current_goals = hs + aws
            if total_line:
                remaining_small_margin = max(0, total_line - current_goals + 0.5)
                score_analysis = f"""当前总得分 {current_goals}，盘口线 {total_line}。
- 小球剩余容错 {remaining_small_margin} 分

"""
            clock_str = str(match_info.get("clock") or "")
            mins_match = re.search(r"(\d+)", clock_str)
            if mins_match:
                elapsed = int(mins_match.group(1))
                if elapsed > 0 and current_goals > 0:
                    pace = round(current_goals / elapsed, 3)
                    full_mins = 48 if sport == "basketball" else 90
                    score_analysis += f"得分节奏: {current_goals}分/{elapsed}分钟 = {pace}分/分钟。若维持此节奏，全场预计 {round(pace * full_mins, 1)} 分。\n"

        # 确定分析模式
        if dim_available >= 6:
            analysis_mode = "完整上下文分析"
            if sport == "basketball":
                mode_guide = f"""### 完整上下文分析模式（{dim_available}/{dim_total} 维度有数据）- 篮球

#### 信号评估
1. **交锋数据**：场均总分显著低于盘口才支持小球
2. **近期状态**：两队近5场得分偏低才支持小球
3. **排名差距**：实力悬殊->可能刷分，势均力敌->谨慎
4. **伤停/阵容**：核心后卫缺阵降分，中锋缺阵影响篮板和二次进攻
5. **盘路走势**：低分走势占优才支持小球
6. **盘口变化**：初盘下调才是小球支持信号
7. **水位变化**：变化<3%视为噪音（篮球水位波动天然小，阈值低于足球）

#### 置信度
- 5-6/6维度+强一致->0.60-0.72
- 3-4/6维度+中等一致->0.52-0.62
- 任一核心维度明显冲突/末节高波动->≤0.40或skip
- 篮球小分必须更保守，禁止把弱信号抬成高置信度"""
            else:
                mode_guide = f"""### 完整上下文分析模式（{dim_available}/{dim_total} 维度有数据）- 足球

#### 信号评估
1. **交锋数据**：场均进球显著低于盘口才支持小球
2. **近期状态**：两队近5场进球偏低才支持小球
3. **排名差距**：实力悬殊->可能刷分，势均力敌->谨慎
4. **伤停/阵容**：前锋缺阵降进球，后卫缺阵可能更保守
5. **走势页**：初指与即时盘一致时增强信号；大幅背离时谨慎
6. **盘口变化**：初盘->即时盘变化反映市场预期，但不能单独决定方向
7. **水位变化**：变化<5%视为噪音

#### 置信度
- 5-6/6维度+信号一致->0.62-0.78
- 3-4/6维度+信号一致->0.50-0.66
- 盘口与基本面矛盾->≤0.42或skip"""
        else:
            analysis_mode = "盘口优先分析"
            # 计算纯盘口信号
            odds_signals = self._build_odds_only_signals(market_odds, match_info, total_line)
            mode_guide = f"""### 盘口优先分析模式（{dim_available}/{dim_total} 维度，基本面不足）

> 没有交锋/近况/排名数据，只能基于盘口和实时比分分析。必须严格量化。

#### 盘口信号解读
{json.dumps(odds_signals, ensure_ascii=False, separators=(',', ':')) if odds_signals else '无可用盘口信号'}

#### 分析规则
1. **盘口变化幅度**：初盘→即时盘变化>0.5球→市场共识信号强。变化≤0.25球→信号弱，应skip
2. **赔率方向**：小球赔率下降才是市场支持；变化<8%视为噪音
3. **实时比分节奏**（仅滚球）：计算当前进球节奏vs盘口线隐含节奏。节奏偏差>30%→有预测价值
   - 当前节奏明显慢于盘口预期(>30%偏差)才支持小球
   - 节奏偏差15-30%→弱信号；节奏偏差<15%→噪音，应skip
4. **比赛阶段**：75分钟+进球概率↑；上半场进球率较低
5. **反向思维**：盘口已大幅变化(>0.75球)后，市场可能过度反应，reverse方向反而有价值

#### 置信度（纯盘口模式严格限制）
- 盘口信号+节奏信号方向一致→0.42-0.48
- 仅单一信号→0.35-0.42
- 信号矛盾或弱→skip
- 无盘口变化数据且无实时比分→必须skip

#### Skip条件
- 盘口变化≤0.25球且无实时节奏信号→skip
- 赔率变化<8%且无节奏信号→skip
- 节奏偏差<15%且盘口信号弱→skip
- 无基本面数据时：小球需盘口与节奏双信号一致，否则 skip
- skip时confidence=0.0, key_factors=["数据不足，无法判断"]"""

        prompt += f"""
## 分析框架：{analysis_mode} {dim_summary}
{chr(10).join(dim_lines)}

{score_analysis}{mode_guide}

## 输出格式（严格JSON）
{{
    "bet_type": "total | first_half_total | second_half_total",
    "prediction": "under 或 skip",
    "line": null,
    "confidence": 0.0-1.0,
    "reasoning": "1.盘口:初盘X->即时X,水位变化X% 2.实时比分:当前X球,节奏X球/分钟,盘口预期X球/分钟 3.综合:量化信号+置信度理由",
    "key_factors": ["因素1", "因素2", "因素3"],
    "risk_level": "low/medium/high",
    "value_bets": [
        {{"selection": "under", "bet_type": "total | first_half_total | second_half_total", "reason": "为什么有价值"}}
    ]
}}

注意：prediction只能是under/skip；skip时confidence=0.0；bet_type只能是total/first_half_total/second_half_total；reasoning必须包含具体数字；只输出JSON。
"""
        # 小球严格分析规则（按运动类型分离）
        if sport == "basketball":
            prompt += (
                "\n## 小球严格分析规则 - 篮球\n"
                "### 小球方向\n"
                "选 under 必须满足以下至少3项，且其中必须包含 盘口方向 或 基本面 支持：\n"
                "1) 盘口方向支持 under，满足其一即算支持：\n"
                "   - 降盘≥5分（强烈小分信号，无需水位确认）\n"
                "   - 降盘2-5分 且 小分水位下降>3%\n"
                "2) 实时得分节奏慢于盘口预期（偏差>25%）\n"
                "3) 基本面支持 under（交锋场均<160/近况得分低/防守型球队）\n"
                "4) 低分走势占优\n"
                "仅满足2项且缺少盘口/基本面核心支持 -> skip\n"
                "5) 注意：Q4后段犯规战术+罚球易刷分，44分钟后 under 默认更谨慎，弱信号必须 skip\n\n"
                "### 通用规则\n"
                "- 无基本面数据时，篮球小球不能给高置信度；弱信号直接 skip\n"
                "- 若初指、实时盘口、基本面三者未形成同向支持，under 优先 skip\n"
                "- 三类信号全矛盾 -> 必须 skip\n"
                "- confidence 必须与信号强度匹配，不得虚高\n"
            )
        else:
            prompt += (
                "\n## 小球严格分析规则 - 足球\n"
                "### 小球方向\n"
                "选 under 必须满足以下至少2项：\n"
                "1) 盘口方向支持 under（降盘或小球水位下降>5%）\n"
                "2) 实时进球节奏慢于盘口预期（偏差>25%）\n"
                "3) 基本面支持 under（交锋场均<1.5球/近况进球少/防守型球队）\n"
                "仅满足1项或0项 -> skip\n"
                "4) 注意：75分钟后进球概率上升，under 需更谨慎\n\n"
                "### 通用规则\n"
                "- 无基本面数据时，小球需 conf>=0.40 且双信号一致\n"
                "- 三类信号（初指/实时盘口/基本面）全矛盾 -> 必须 skip\n"
                "- confidence 必须与信号强度匹配，不得虚高\n"
            )

        try:
            from app.services.sports_data import confidence_cap_for_quality, compute_quality

            q = {}
            if isinstance(historical_data, dict):
                q = historical_data.get("quality") or compute_quality(historical_data)
            else:
                q = {"source": ctx_source or "none", "completeness": 0.0}
            cap = confidence_cap_for_quality(q)
            if cap is not None:
                prompt += (
                    f"\n> 强制：赛前真实数据不足（source={q.get('source')} "
                    f"completeness={q.get('completeness')}），"
                    f"不得编造 H2H/近况/伤病，confidence 必须 ≤ {cap}，信息不足不得抬高信心。\n"
                )
            elif ctx_source == "none":
                prompt += (
                    f"\n> 注意：无真实交锋/近况/伤病数据，请仅基于盘口赔率分析，"
                    f"confidence 必须低于 {0.55}。\n"
                )
        except Exception:
            if ctx_source == "none":
                prompt += (
                    f"\n> 注意：无真实交锋/近况/伤病数据，请仅基于盘口赔率分析，"
                    f"confidence 必须低于 {0.55}。\n"
                )
        return prompt

    def _parse_analysis_result(self, raw: str) -> dict:
        try:
            text = (raw or "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
                if text.lower().startswith("json"):
                    text = text[4:].strip()
            if not text.startswith("{"):
                start = text.find("{")
                end = text.rfind("}")
                if start >= 0 and end > start:
                    text = text[start : end + 1]
            return json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning(f"LLM返回非JSON: {(raw or '')[:200]}")
            return {}  # 返回空 dict，调用方会标记为无效

    def _fallback_result(self, reason: str, error: Optional[str] = None) -> dict:
        result = {
            "prediction": "skip",
            "bet_type": "total",
            "confidence": 0.0,
            "reasoning": reason,
            "key_factors": ["数据不足，无法判断"],
            "value_bets": [],
            "risk_level": "high",
            "consensus_reached": False,
            "models_used": [],
            "models_failed": [],
        }
        if error:
            result["error"] = error
        return result

    @staticmethod
    def _build_statistical_signals(
        historical_data: Optional[dict],
        market_odds: Optional[dict],
        match_info: dict,
        h2h_block: Optional[dict],
    ) -> dict:
        """预计算 5 类统计信号，注入 prompt 减少 LLM 主观偏差。

        1. 积分榜期望进球差 (xG diff)
        2. 交锋胜率统计基线
        3. 球员攻防效率比
        4. 盘口水位变化信号
        5. 比赛阶段权重
        """
        signals: dict[str, Any] = {}
        sport = str(match_info.get("sport") or "football").strip().lower()
        # --- 1. 积分榜期望进球差 ---
        if isinstance(historical_data, dict):
            standings = historical_data.get("standings") or {}
            st_h = standings.get("home") or {}
            st_a = standings.get("away") or {}
            if isinstance(st_h, dict) and isinstance(st_a, dict):
                try:
                    h_played = float(st_h.get("played") or 0)
                    a_played = float(st_a.get("played") or 0)
                    if h_played > 0 and a_played > 0:
                        h_gf = float(st_h.get("goals_for") or st_h.get("gf") or 0) / h_played
                        h_ga = float(st_h.get("goals_against") or st_h.get("ga") or 0) / h_played
                        a_gf = float(st_a.get("goals_for") or st_a.get("gf") or 0) / a_played
                        a_ga = float(st_a.get("goals_against") or st_a.get("ga") or 0) / a_played
                        h_xg_diff = round(h_gf - h_ga, 2)
                        a_xg_diff = round(a_gf - a_ga, 2)
                        signals["standings_xg_diff"] = {
                            "home": h_xg_diff,
                            "away": a_xg_diff,
                            "edge": round(h_xg_diff - a_xg_diff, 2),
                        }
                except (TypeError, ValueError):
                    pass

        # --- 2. 交锋胜率统计基线 + 进球统计 ---
        if isinstance(h2h_block, dict):
            summary = h2h_block.get("summary") or {}
            played = summary.get("played") or 0
            if played and played > 0:
                hw = summary.get("home_wins") or 0
                d = summary.get("draws") or 0
                aw = summary.get("away_wins") or 0
                signals["h2h_baseline"] = {
                    "played": played,
                    "home_win_rate": round(hw / played, 3),
                    "draw_rate": round(d / played, 3),
                    "away_win_rate": round(aw / played, 3),
                    "avg_total_goals": summary.get("avg_total_goals", 0),
                    "under_2_5_rate": summary.get("under_2_5_rate", 0),
                }

        # --- 2b. 近期状态进球统计 ---
        if isinstance(historical_data, dict):
            for side, key in (("home", "home_form"), ("away", "away_form")):
                form = historical_data.get(key)
                if isinstance(form, dict):
                    fs = form.get("summary") or {}
                    if fs:
                        signals[f"{side}_form_stats"] = {
                            "played": fs.get("played", 0),
                            "win_rate": fs.get("win_rate", 0),
                            "avg_total_goals": fs.get("avg_total_goals", 0),
                            "under_2_5_rate": fs.get("under_2_5_rate", 0),
                        }
            # 综合两队近期进球预期
            hf = (signals.get("home_form_stats") or {}).get("avg_total_goals", 0)
            af = (signals.get("away_form_stats") or {}).get("avg_total_goals", 0)
            if hf and af:
                signals["form_combined_expected_goals"] = round((hf + af) / 2, 2)

        # --- 3. 盘口变化信号（line_movements 是 dict，key=bet_type） ---
        if isinstance(market_odds, dict):
            line_moves = market_odds.get("line_movements")
            # 方式1：从 line_movements dict 中取 total 的变化数据
            total_move = None
            if isinstance(line_moves, dict):
                total_move = line_moves.get("total") or {}
            elif isinstance(line_moves, list) and line_moves:
                total_move = line_moves[-1] if isinstance(line_moves[-1], dict) else {}

            if isinstance(total_move, dict) and total_move:
                line_delta = total_move.get("line_delta")
                odds_delta = total_move.get("odds_delta")
                direction = str(total_move.get("direction") or "").lower()
                change_count = total_move.get("change_count") or 0
                opening = total_move.get("opening") or {}
                open_line = opening.get("line") if isinstance(opening, dict) else None
                open_odds = opening.get("odds") if isinstance(opening, dict) else None

                mkt_signal: dict[str, Any] = {
                    "change_count": change_count,
                    "direction": direction,
                    "line_delta": line_delta,
                    "odds_delta": odds_delta,
                    "opening_line": open_line,
                    "opening_odds": open_odds,
                }

                # 市场方向判断
                market_support = "neutral"
                signal_strength = "weak"
                if line_delta is not None:
                    try:
                        ld = float(line_delta)
                        if ld <= -0.25:
                            market_support = "under"
                            signal_strength = "strong" if abs(ld) >= 0.5 else "medium"
                        elif ld >= 0.25:
                            market_support = "against_under"
                            signal_strength = "strong" if abs(ld) >= 0.5 else "medium"
                    except (TypeError, ValueError):
                        pass

                # 赔率变化增强信号
                if odds_delta is not None:
                    try:
                        od = float(odds_delta)
                        if market_support == "neutral" and abs(od) >= 0.08:
                            market_support = "under" if od < 0 else "against_under"
                            signal_strength = "medium"
                    except (TypeError, ValueError):
                        pass

                mkt_signal["market_support"] = market_support
                mkt_signal["signal_strength"] = signal_strength
                signals["market_movement"] = mkt_signal

        # --- 4. 比赛阶段权重 ---
        period = str(match_info.get("period") or "").lower()
        clock = str(match_info.get("clock") or "")
        if period or clock:
            stage_weight = "unknown"
            if sport == "basketball":
                # 篮球：Q1 节奏偏慢, Q2-Q3 中段, Q4 得分爆发期
                if "q1" in period or "1q" in period:
                    stage_weight = "Q1(节奏偏慢,得分率低)"
                elif "q2" in period or "2q" in period:
                    stage_weight = "Q2(进入状态,得分稳定)"
                elif "q3" in period or "3q" in period:
                    stage_weight = "Q3(中段,战术调整期)"
                elif "q4" in period or "4q" in period:
                    stage_weight = "Q4(得分爆发期,犯规多)"
            else:
                # 足球：0-15' 开局, 15-45' 中段, 60-75' 高发期, 75+' 冲刺
                if period in ("1h", "first_half", "ht"):
                    stage_weight = "上半场(进球率较低)"
                elif period in ("2h", "second_half", "ft"):
                    stage_weight = "下半场(进球率高发)"
                try:
                    mins = re.search(r"(\d+)", clock)
                    if mins:
                        m = int(mins.group(1))
                        if m >= 60 and m <= 75:
                            stage_weight = "60-75分钟(足球进球高发期)"
                        elif m >= 75:
                            stage_weight = "75分钟+(冲刺期，盘口突变)"
                except Exception:
                    pass
            signals["match_stage"] = {
                "period": period,
                "clock": clock,
                "stage_weight": stage_weight,
            }

        return signals

    @staticmethod
    def _build_odds_only_signals(
        market_odds: Optional[dict],
        match_info: dict,
        total_line: Optional[float],
    ) -> dict:
        """纯盘口模式：从赔率和比分中提取量化信号"""
        signals: dict[str, Any] = {}

        # 1. 盘口变化幅度（line_movements 是 dict，key=bet_type）
        if isinstance(market_odds, dict):
            line_moves = market_odds.get("line_movements")
            total_move = None
            if isinstance(line_moves, dict):
                total_move = line_moves.get("total") or {}
            elif isinstance(line_moves, list) and line_moves:
                total_move = line_moves[-1] if isinstance(line_moves[-1], dict) else {}

            if isinstance(total_move, dict) and total_move:
                line_delta = total_move.get("line_delta")
                odds_delta = total_move.get("odds_delta")
                direction = str(total_move.get("direction") or "").lower()
                opening = total_move.get("opening") or {}
                open_line = opening.get("line") if isinstance(opening, dict) else None
                open_odds = opening.get("odds") if isinstance(opening, dict) else None

                if line_delta is not None:
                    try:
                        ld = float(line_delta)
                        market_support = "under" if ld <= -0.25 else "against_under" if ld >= 0.25 else "neutral"
                        signals["line_change"] = {
                            "initial": open_line,
                            "current": total_line,
                            "delta": ld,
                            "magnitude": abs(ld),
                            "direction": "line_up(不利小球)" if ld > 0 else "line_down(支持小球)" if ld < 0 else "stable",
                            "market_support": market_support,
                            "signal_strength": "strong" if abs(ld) >= 0.5 else "medium" if abs(ld) >= 0.25 else "weak",
                        }
                    except (TypeError, ValueError):
                        pass

                if odds_delta is not None:
                    try:
                        od = float(odds_delta)
                        if abs(od) >= 0.05:
                            signals["odds_change"] = {
                                "odds_delta": od,
                                "signal": "小球赔率下降(市场支持小球)" if od < -0.05 else "小球赔率上升(不利小球)" if od > 0.05 else "赔率变化不大(弱信号)",
                            }
                    except (TypeError, ValueError):
                        pass

        # 3. 实时比分节奏分析
        hs = match_info.get("home_score")
        aws = match_info.get("away_score")
        clock_str = str(match_info.get("clock") or "")
        if hs is not None and aws is not None and clock_str:
            try:
                current_goals = int(hs) + int(aws)
                mins_match = re.search(r"(\d+)", clock_str)
                if mins_match and total_line:
                    elapsed = int(mins_match.group(1))
                    if elapsed > 0:
                        actual_pace = round(current_goals / elapsed, 3)
                        # 盘口隐含全场预期进球 ≈ total_line
                        full_mins = 48 if str(match_info.get("sport") or "").strip().lower() == "basketball" else 90
                        expected_pace = round(total_line / full_mins, 3)
                        pace_deviation = round((actual_pace - expected_pace) / expected_pace * 100, 1) if expected_pace > 0 else 0
                        remaining_mins = max(0, full_mins - elapsed)
                        goals_needed = max(0, total_line - current_goals + 0.5)
                        needed_pace = round(goals_needed / remaining_mins, 3) if remaining_mins > 0 else 999

                        signals["pace_analysis"] = {
                            "current_goals": current_goals,
                            "elapsed_min": elapsed,
                            "remaining_min": remaining_mins,
                            "actual_pace": actual_pace,
                            "expected_pace": expected_pace,
                            "pace_deviation_pct": pace_deviation,
                            "points_remaining_to_exceed_line": goals_needed,
                            "pace_needed_to_exceed_line": needed_pace,
                            "signal": "节奏明显慢于预期(>30%偏差)→小球" if pace_deviation < -30 else "节奏偏快，不支持小球" if pace_deviation > 30 else "节奏偏差15-30%(弱信号)" if abs(pace_deviation) > 15 else "节奏接近预期(偏差<15%，噪音)",
                        }
            except Exception:
                pass

        return signals

    _pending_tasks: set = set()

    @staticmethod
    def _record_prediction(
        match_id: int,
        prediction: str,
        bet_type: str,
        confidence: float,
        odds: float,
        model_votes: list[dict],
    ) -> None:
        """记录 AI 预测到 Redis，供历史校准和模型权重更新使用。

        Redis key: ai:prediction:{match_id} -> {prediction, bet_type, confidence, odds, models, timestamp}
        """
        try:
            record = {
                "match_id": match_id,
                "prediction": prediction,
                "bet_type": bet_type,
                "confidence": confidence,
                "odds": odds,
                "models": [v.get("model", "") for v in model_votes if v.get("ok")],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "result": None,
            }

            async def _save():
                import redis.asyncio as aioredis
                r = aioredis.Redis(host="ob-redis", port=6379, socket_timeout=1.0)
                try:
                    await r.setex(
                        f"ai:prediction:{match_id}",
                        86400 * 7,
                        json.dumps(record),
                    )
                except Exception:
                    pass
                finally:
                    await r.aclose()

            try:
                asyncio.get_running_loop()
                task = asyncio.create_task(_save())
                MatchAnalyzer._pending_tasks.add(task)
                task.add_done_callback(MatchAnalyzer._pending_tasks.discard)
            except RuntimeError:
                pass
        except Exception:
            pass

analyzer = MatchAnalyzer()
