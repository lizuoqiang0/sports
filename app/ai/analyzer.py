"""
AI 赛事分析引擎 - DeepSeek 单模型分析

仅做全场大小球(total/under/over)分析。
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

from app.core.convert import to_float as _to_float

from openai import AsyncOpenAI

from app.config import settings
from app.core.cache import cache

logger = logging.getLogger(__name__)

VALID_PREDICTIONS = {"under", "over", "skip"}

_PRED_ALIASES = {
    "under": "under",
    "u": "under",
    "小": "under",
    "小球": "under",
    "over": "over",
    "o": "over",
    "大": "over",
    "大球": "over",
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
    elif "大球" in s or s == "大" or "over" in s:
        pred = "over"
    elif s == "skip":
        pred = "skip"
    else:
        pred = ""

    bt = normalize_bet_type(bet_type)
    if bt == "total" and pred not in ("under", "over", "skip"):
        return ""
    if pred not in VALID_PREDICTIONS:
        return ""
    return pred


def _flatten_market_odds(market_odds: Optional[dict]) -> dict[str, float]:
    """把嵌套 markets 或扁平 odds 合成 selection->odds 映射。"""
    if not market_odds:
        return {}
    if any(k in market_odds for k in ("under", "over")):
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
        direct_entry = market_odds.get(bet_type)
        if isinstance(direct_entry, dict) and direct_entry.get("line") is not None:
            try:
                return float(direct_entry["line"])
            except (TypeError, ValueError):
                pass
        if bet_type == "total" and market_odds.get("line") is not None:
            try:
                return float(market_odds["line"])
            except (TypeError, ValueError):
                pass
    info = match_info or {}
    if bet_type == "total":
        return info.get("total_line") or info.get("line")
    if bet_type == "spread":
        return info.get("spread_line") or info.get("handicap_line")
    return None


def _line_source_for_pick(market_odds: Optional[dict], match_info: Optional[dict]) -> str:
    if isinstance(market_odds, dict):
        markets = market_odds.get("markets") if isinstance(market_odds.get("markets"), dict) else {}
        total = markets.get("total") if isinstance(markets.get("total"), dict) else {}
        direct = market_odds.get("total") if isinstance(market_odds.get("total"), dict) else {}
        if total.get("line") is not None or direct.get("line") is not None or market_odds.get("line") is not None:
            return "bookmaker_market_total"
    info = match_info if isinstance(match_info, dict) else {}
    if info.get("total_line") is not None or info.get("line") is not None:
        return "bookmaker_match_snapshot"
    return "missing"


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
    """DeepSeek 单模型分析引擎。"""

    def __init__(self):
        self.client: Optional[AsyncOpenAI] = None
        self.model: str = ""
        self._init_client()

    def _init_client(self):
        api_key = (settings.DEEPSEEK_API_KEY or "").strip()
        base_url = (settings.DEEPSEEK_BASE_URL or "").strip()
        model = (settings.DEEPSEEK_MODEL or "").strip()
        if not api_key or not model:
            logger.warning("DeepSeek 未配置：缺少 API_KEY 或 MODEL")
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
        logger.info("DeepSeek model ready: %s (%s)", model, base_url)

    async def analyze_match(
        self,
        match_info: dict,
        historical_data: Optional[dict] = None,
        market_odds: Optional[dict] = None,
    ) -> dict:
        from app.services.fixture_key import fixture_key as _fixture_key
        from app.services.nowscore_evidence import (
            build_total_market_evidence,
            evidence_gate_reason,
        )
        from app.ai.total_features import build_total_feature_matrix

        authoritative_line = _line_for_pick(market_odds, match_info, "total")
        authoritative_line_source = _line_source_for_pick(market_odds, match_info)
        evidence = build_total_market_evidence(
            historical_data,
            line=authoritative_line,
            sport=str(match_info.get("sport") or "football"),
            line_source=authoritative_line_source,
            max_age_sec=int(getattr(settings, "NOWSCORE_MAX_CONTEXT_AGE_SEC", 21600) or 21600),
            min_form_samples=int(getattr(settings, "NOWSCORE_MIN_FORM_SAMPLES", 3) or 3),
        )
        if isinstance(historical_data, dict):
            historical_data = copy.deepcopy(historical_data)
            historical_data["total_market_evidence"] = evidence

        feature_matrix = build_total_feature_matrix(
            match_info,
            market_odds,
            historical_data,
            line=authoritative_line,
            line_source=authoritative_line_source,
        )
        if isinstance(historical_data, dict):
            historical_data["total_feature_matrix"] = feature_matrix

        if bool(getattr(settings, "AI_REQUIRE_NOWSCORE_CONTEXT", True)):
            gate_reason = evidence_gate_reason(evidence)
            if gate_reason:
                logger.warning(
                    "[AI分析] NowScore证据闸门拒绝 match=%s %s vs %s line=%s reason=%s",
                    match_info.get("id"), match_info.get("home_team"),
                    match_info.get("away_team"), authoritative_line, gate_reason,
                )
                result = self._fallback_result(
                    "NowScore数据未通过球队/球种/时效/样本/盘口校验，禁止模型猜测",
                    error=gate_reason,
                )
                result["line"] = authoritative_line
                result["line_source"] = authoritative_line_source
                result["nowscore_evidence"] = evidence
                return result
        if bool(getattr(settings, "AI_REQUIRE_STRUCTURED_TOTAL_FEATURES", True)):
            feature_gates = feature_matrix.get("gates") if isinstance(feature_matrix.get("gates"), dict) else {}
            if feature_gates.get("analysis_ready") is not True:
                failures = [str(x) for x in (feature_gates.get("hard_failures") or []) if str(x)]
                logger.warning(
                    "[AI分析] 结构化特征闸门拒绝 match=%s failures=%s",
                    match_info.get("id"), failures,
                )
                result = self._fallback_result(
                    "大小球结构化数据不完整，缺少有效比赛时间、双边赔率或基本面证据",
                    error="total_feature_gate:" + ",".join(failures),
                )
                result["line"] = authoritative_line
                result["line_source"] = authoritative_line_source
                result["nowscore_evidence"] = evidence
                result["total_feature_matrix"] = feature_matrix
                return result

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
        # 精确盘口进入缓存键；2.25/2.5/2.75 必须是不同分析快照。
        raw_line = authoritative_line
        try:
            line_tag = f"{float(raw_line):.3f}" if raw_line is not None else ""
        except (TypeError, ValueError):
            line_tag = str(raw_line)
        # 加入当前总进球数：滚球比分变化（0-0→1-0）必须触发重新分析
        _hs = match_info.get("home_score")
        _as = match_info.get("away_score")
        _total_goals = int((_hs or 0) + (_as or 0)) if _hs is not None or _as is not None else "x"
        cache_key = f"ai:deepseek:v2:{fk}:{sport}:{line_tag}:g{_total_goals}"
        # 缓存策略：滚球（有比分）禁用缓存强制实时分析，赛前（无比分）允许缓存
        # 滚球比分/盘口变化快，旧缓存的 reasoning 会过时；赛前数据稳定可复用
        is_live = _hs is not None or _as is not None
        try:
            cached = await cache.get_json(cache_key) if not is_live else None
            if cached and cached.get("models_used") and not cached.get("error"):
                # ── 缓存校验：比分/盘口/时间变化超过阈值则不使用缓存 ──
                # 比分从 0-1 变成 1-0（同 g1）时 reasoning 仍可能过时。
                cached_line = cached.get("line")
                cached_conf = cached.get("confidence", 0)
                current_line_f = None
                try:
                    current_line_f = float(raw_line) if raw_line else None
                except (TypeError, ValueError):
                    pass
                # 防御旧版本缓存：盘口线有任何有效档位变化就失效。
                line_stale = (
                    cached_line is not None
                    and current_line_f is not None
                    and abs(float(cached_line) - current_line_f) > 0.001
                )
                # 缓存中的比对快照 vs 当前实际比分
                cached_hs = cached.get("_cached_home_score")
                cached_as = cached.get("_cached_away_score")
                score_changed = (
                    cached_hs is not None and cached_hs != _hs
                    or cached_as is not None and cached_as != _as
                )
                # 半场切换 → 缓存失效（上下半场节奏分布完全不同）
                cached_period = cached.get("_cached_period", "")
                current_period = str(match_info.get("period") or "")
                period_changed = bool(cached_period) and bool(current_period) and cached_period != current_period

                if line_stale or score_changed or period_changed:
                    why = (
                        f"盘口漂移({cached_line}→{current_line_f})" if line_stale
                        else f"比分变化({cached_hs}-{cached_as}→{_hs}-{_as})" if score_changed
                        else f"半场切换({cached_period}→{current_period})"
                    )
                    logger.info(
                        "[AI分析] 缓存失效 match=%s %s vs %s | 原因: %s | 重新调 LLM",
                        match_info.get("id"), match_info.get("home_team"), match_info.get("away_team"),
                        why,
                    )
                    # 不 return，继续走 LLM 重新分析
                elif cached.get("consensus_reached") and str(cached.get("prediction") or "") in VALID_PREDICTIONS:
                    logger.info(
                        "[AI分析] 缓存命中 match=%s %s vs %s | pred=%s conf=%.2f",
                        match_info.get("id"), match_info.get("home_team"), match_info.get("away_team"),
                        cached.get("prediction"), float(cached.get("confidence") or 0),
                    )
                    return cached
                elif cached.get("neg_cached"):
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
            timeout = float(settings.DEEPSEEK_TIMEOUT_SEC)
            # 加载历史胜率反馈注入 system prompt，帮助 DeepSeek 从过去的错误中学习
            system_extra = await self._build_historical_feedback(match_info)
            raw = await asyncio.wait_for(self._call_deepseek(prompt, system_extra), timeout=timeout)
            content = raw.get("content", "")
            parsed = self._parse_analysis_result(content)

            # ── 双向置信度提取 ──
            # DeepSeek 同时输出 under_confidence 和 over_confidence，取更强方向作为最终 prediction。
            # 这确保每场比赛都分析了大小球两个方向，而非只看一个方向。
            under_conf_raw = parsed.get("under_confidence")
            over_conf_raw = parsed.get("over_confidence")

            try:
                under_conf = float(under_conf_raw) if under_conf_raw is not None else None
            except (TypeError, ValueError):
                under_conf = None
            try:
                over_conf = float(over_conf_raw) if over_conf_raw is not None else None
            except (TypeError, ValueError):
                over_conf = None

            bt = "total"

            # 如果 DeepSeek 提供了双向置信度，取更强方向
            if under_conf is not None and over_conf is not None:
                if under_conf < 0.30 and over_conf < 0.30:
                    pred = "skip"
                elif under_conf >= over_conf:
                    pred = "under"
                else:
                    pred = "over"
                # 记录双向置信度到 parsed，供后续日志和分析使用
                parsed["_under_conf"] = under_conf
                parsed["_over_conf"] = over_conf
            else:
                # 向后兼容：DeepSeek 未提供双向置信度时，回退到 prediction 字段
                pred = normalize_prediction(parsed.get("prediction"), bet_type="total")

            if pred not in ("under", "over", "skip"):
                # 精简 prompt 重试：原始输出可能因 max_tokens 截断或格式偏差导致无效
                # 用极简 prompt 重试一次，只要求核心字段，大幅降低 token 消耗
                logger.warning(
                    "[AI分析] DeepSeek 返回无效结果，尝试精简 prompt 重试 match=%s | bt=%s pred=%s",
                    match_info.get("id"), parsed.get("bet_type"), parsed.get("prediction"),
                )
                retry_parsed = await self._retry_with_minimal_prompt(
                    match_info, market_odds, system_extra,
                    evidence=evidence, feature_matrix=feature_matrix,
                )
                if retry_parsed:
                    parsed = retry_parsed
                    # 重新提取双向置信度
                    under_conf_raw = parsed.get("under_confidence")
                    over_conf_raw = parsed.get("over_confidence")
                    try:
                        under_conf = float(under_conf_raw) if under_conf_raw is not None else None
                    except (TypeError, ValueError):
                        under_conf = None
                    try:
                        over_conf = float(over_conf_raw) if over_conf_raw is not None else None
                    except (TypeError, ValueError):
                        over_conf = None
                    if under_conf is not None and over_conf is not None:
                        if under_conf < 0.30 and over_conf < 0.30:
                            pred = "skip"
                        elif under_conf >= over_conf:
                            pred = "under"
                        else:
                            pred = "over"
                    else:
                        pred = normalize_prediction(parsed.get("prediction"), bet_type="total")
                    logger.info(
                        "[AI分析] 精简 prompt 重试成功 match=%s | pred=%s conf=%s",
                        match_info.get("id"), pred, parsed.get("confidence"),
                    )

            if pred not in ("under", "over", "skip"):
                logger.warning(
                    "[AI分析] DeepSeek 最终返回无效结果 match=%s | bt=%s pred=%s",
                    match_info.get("id"), parsed.get("bet_type"), parsed.get("prediction"),
                )
                return self._fallback_result(
                    f"DeepSeek返回无效结果: bet_type={parsed.get('bet_type')!r} pred={parsed.get('prediction')!r}",
                    error="invalid_result",
                )

            if pred == "skip":
                logger.info(
                    "[AI分析] DeepSeek 判定跳过 match=%s | reasoning=%s",
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
                    "models_used": ["deepseek"],
                    "models_failed": [],
                    "neg_cached": True,
                    "line_source": authoritative_line_source,
                    "nowscore_evidence": evidence,
                    "total_feature_matrix": feature_matrix,
                }
                # skip 也写负缓存：滚球晚段大量 skip，不缓存则每轮 120s 重复打 LLM
                try:
                    await cache.set_json(
                        cache_key, skip_result, ttl=settings.AI_SKIP_CACHE_TTL
                    )
                except Exception:
                    pass
                return skip_result

            # ── 置信度提取：优先取双向置信度中更强方向的值 ──
            if pred == "under" and under_conf is not None:
                conf = under_conf
            elif pred == "over" and over_conf is not None:
                conf = over_conf
            else:
                try:
                    conf = float(parsed.get("confidence", settings.LLM_DEFAULT_CONFIDENCE))
                except (TypeError, ValueError):
                    conf = settings.LLM_DEFAULT_CONFIDENCE
            conf = max(0.0, min(conf, 1.0))

            line = parsed.get("line")
            try:
                model_line = float(line) if line is not None and line != "" else None
            except (TypeError, ValueError):
                model_line = None
            line_f = authoritative_line
            if (
                model_line is not None
                and line_f is not None
                and abs(model_line - float(line_f)) > 0.001
            ):
                logger.warning(
                    "[AI分析] 模型盘口不一致，拒绝结果 match=%s model_line=%s bookmaker_line=%s",
                    match_info.get("id"), model_line, line_f,
                )
                mismatch = self._fallback_result(
                    "DeepSeek返回盘口与投注平台当前盘口不一致，禁止使用该预测",
                    error="model_total_line_mismatch",
                )
                mismatch["line"] = line_f
                mismatch["model_line"] = model_line
                mismatch["line_source"] = authoritative_line_source
                mismatch["nowscore_evidence"] = evidence
                mismatch["total_feature_matrix"] = feature_matrix
                return mismatch

            latency_ms = float((raw.get("_meta") or {}).get("latency_ms") or 0)

            # 单模型模式：DeepSeek 返回 under/over 即视为共识达成
            consensus_reached = pred in ("under", "over")

            analysis = {
                "prediction": pred,
                "bet_type": bt,
                "line": line_f,
                "confidence": round(conf, 4),
                "under_confidence": round(float(under_conf), 4) if under_conf is not None else None,
                "over_confidence": round(float(over_conf), 4) if over_conf is not None else None,
                "under_reasoning": (parsed.get("under_reasoning") or "")[:500],
                "over_reasoning": (parsed.get("over_reasoning") or "")[:500],
                "reasoning": (parsed.get("reasoning") or "")[:800],
                "market_analysis": (parsed.get("market_analysis") or "")[:800],
                "fundamental_analysis": (parsed.get("fundamental_analysis") or "")[:800],
                "core_analysis": parsed.get("core_analysis") or {},
                "fundamental_summary": (parsed.get("fundamental_summary") or "")[:600],
                "key_factors": (parsed.get("key_factors") or [])[:8],
                "value_bets": (parsed.get("value_bets") or [])[:5],
                "risk_level": parsed.get("risk_level", "medium"),
                "consensus_reached": consensus_reached,
                "models_used": ["deepseek"],
                "models_failed": [],
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
                "line_source": authoritative_line_source,
                "nowscore_evidence": evidence,
                "total_feature_matrix": feature_matrix,
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
            # P1 市场赔率约束：在 signal_review/floor 之后生效
            analysis = self._apply_market_odds_constraint(analysis, match_info)
            # 升盘型 over 约束：数学调整型升盘 → over conf 封顶 0.62
            analysis = self._apply_line_up_constraint(analysis, match_info, market_odds)

            # ── 历史结果校准：基于实际投注胜率校准 DeepSeek 置信度 ──
            analysis = await self._apply_historical_calibration(analysis, match_info)

            # 单模型模式：DeepSeek 返回 under/over 即为最终共识。

            # 简洁单行分析日志
            _hd = historical_data if isinstance(historical_data, dict) else {}
            _core_n = sum(1 for k in ("h2h", "home_form", "away_form", "standings", "trend") if _hd.get(k))
            _aux_n = sum(1 for k in ("analysis",) if _hd.get(k))
            _aux_n += 2 if isinstance(market_odds, dict) and market_odds.get("markets") else 1 if isinstance(market_odds, dict) and market_odds else 0
            _aux_n += 1 if isinstance(market_odds, dict) and market_odds.get("line_movements") else 0
            _sport = str(match_info.get("sport") or "?")
            _line = match_info.get("total_line") or match_info.get("line") or "?"
            _hs = match_info.get("home_score")
            _as = match_info.get("away_score")
            _score = f"{_hs}:{_as}" if _hs is not None else "?"
            _clock = str(match_info.get("clock") or "?")
            _uc = float(under_conf) if under_conf is not None else 0.0
            _oc = float(over_conf) if over_conf is not None else 0.0
            _odds_str = ""
            try:
                _od = float(analysis.get("odds") or 0)
                if _od > 1:
                    _odds_str = f" odds={_od:.2f}"
            except (TypeError, ValueError):
                pass
            logger.info(
                "[AI分析] match=%s %s vs %s | %s line=%s %s %s' | pred=%s conf=%.2f | under=%.2f over=%.2f%s | 核心%d/5+辅助%d/4",
                match_info.get("id"),
                match_info.get("home_team", "?"),
                match_info.get("away_team", "?"),
                _sport,
                _line,
                _score,
                _clock,
                analysis.get("prediction"),
                float(analysis.get("confidence") or 0),
                _uc,
                _oc,
                _odds_str,
                _core_n,
                min(_aux_n, 4),
            )

            if analysis.get("models_used"):
                try:
                    if analysis.get("consensus_reached"):
                        # 滚球场景比分变化快，正缓存缩短到 3 分钟（跨 1 轮 120s 轮询）
                        # 写入缓存时保存分析时刻快照，供下次缓存校验
                        analysis["_cached_home_score"] = _hs
                        analysis["_cached_away_score"] = _as
                        analysis["_cached_period"] = str(match_info.get("period") or "")
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
                    [{"model": "deepseek", "ok": True}],
                )

            return analysis

        except asyncio.TimeoutError:
            logger.warning(
                "[AI分析] DeepSeek 超时 match=%s timeout=%.0fs，尝试精简 prompt 重试",
                match_info.get("id"), timeout,
            )
            # 超时后用精简 prompt 重试：更短 prompt + 更少 tokens + 更短超时
            # system_extra 在 wait_for 之前已构建，超时时仍可用
            _timeout_extra = locals().get("system_extra", "")
            retry_parsed = await self._retry_with_minimal_prompt(
                match_info, market_odds, _timeout_extra,
                evidence=evidence, feature_matrix=feature_matrix,
            )
            if retry_parsed:
                logger.info(
                    "[AI分析] 精简 prompt 重试成功（超时恢复）match=%s",
                    match_info.get("id"),
                )
                # 用重试结果重新走双向置信度提取流程
                under_conf_raw = retry_parsed.get("under_confidence")
                over_conf_raw = retry_parsed.get("over_confidence")
                try:
                    under_conf = float(under_conf_raw) if under_conf_raw is not None else None
                except (TypeError, ValueError):
                    under_conf = None
                try:
                    over_conf = float(over_conf_raw) if over_conf_raw is not None else None
                except (TypeError, ValueError):
                    over_conf = None

                if under_conf is not None and over_conf is not None:
                    if under_conf < 0.30 and over_conf < 0.30:
                        pred = "skip"
                    elif under_conf >= over_conf:
                        pred = "under"
                    else:
                        pred = "over"
                else:
                    pred = normalize_prediction(retry_parsed.get("prediction"), bet_type="total")

                if pred in ("under", "over"):
                    try:
                        conf = float(retry_parsed.get("confidence", 0))
                    except (TypeError, ValueError):
                        conf = 0.0
                    conf = max(0.0, min(conf, 1.0))
                    line_f = _line_for_pick(market_odds, match_info, "total")
                    od = _odds_for_pick(market_odds, "total", pred)
                    analysis = {
                        "prediction": pred,
                        "bet_type": "total",
                        "line": line_f,
                        "confidence": round(conf, 4),
                        "under_confidence": round(float(under_conf), 4) if under_conf is not None else None,
                        "over_confidence": round(float(over_conf), 4) if over_conf is not None else None,
                        "reasoning": (retry_parsed.get("reasoning") or "精简prompt重试结果")[:800],
                        "consensus_reached": True,
                        "models_used": ["deepseek"],
                        "models_failed": [],
                        "analyzed_at": datetime.now(timezone.utc).isoformat(),
                        "timeout_retry": True,
                        "line_source": authoritative_line_source,
                        "nowscore_evidence": evidence,
                        "total_feature_matrix": feature_matrix,
                    }
                    if od > 1:
                        analysis["odds"] = od
                    analysis = self._apply_context_quality_cap(analysis, historical_data)
                    analysis = self._apply_signal_review(
                        analysis, match_info=match_info,
                        historical_data=historical_data, market_odds=market_odds,
                    )
                    analysis = self._apply_market_odds_constraint(analysis, match_info)
                    analysis = self._apply_line_up_constraint(analysis, match_info, market_odds)
                    analysis = await self._apply_historical_calibration(analysis, match_info)
                    return analysis
                elif pred == "skip":
                    logger.info(
                        "[AI分析] 精简 prompt 重试判定跳过 match=%s",
                        match_info.get("id"),
                    )
                    return self._fallback_result(
                        "DeepSeek精简重试判定skip", error="deepseek_timeout_retry_skip",
                    )
            logger.warning(
                "[AI分析] 精简 prompt 重试仍失败 match=%s，改用盘口启发式",
                match_info.get("id"),
            )
            return self._fallback_result("AI分析超时，改用盘口启发式", error="deepseek_timeout")
        except Exception as e:
            logger.error("[AI分析] DeepSeek 失败 match=%s: %s", match_info.get("id"), e)
            return self._fallback_result(f"AI分析暂不可用: {e}", error=str(e))

    async def _retry_with_minimal_prompt(
        self,
        match_info: dict,
        market_odds: Optional[dict],
        system_extra: str = "",
        *,
        evidence: Optional[dict] = None,
        feature_matrix: Optional[dict] = None,
    ) -> Optional[dict]:
        """精简 prompt 重试：原始输出无效/超时时，用极简 prompt 再调一次 DeepSeek。

        只要求 under_confidence / over_confidence / reasoning 三个核心字段，
        max_tokens 降到 512，大幅降低截断和格式偏差概率。
        超时设为 30s（比正常 45s 短，避免拖长总分析时间）。

        Args:
            match_info: 比赛信息
            market_odds: 盘口数据
            system_extra: 注入到 system prompt 的额外上下文（历史胜率反馈）
        """
        if not self.client or not self.model:
            return None
        total_line = _line_for_pick(market_odds, match_info, "total")
        total_line = total_line if total_line is not None else "?"
        hs = match_info.get("home_score")
        aws = match_info.get("away_score")
        score_str = f"{hs}-{aws}" if hs is not None else "未知"
        sport = str(match_info.get("sport") or "football").lower()
        league = match_info.get("league", "")

        # 极简 prompt：只给核心数据，要求最小输出
        mini_prompt = (
            f"赛事: {match_info.get('home_team', '?')} vs {match_info.get('away_team', '?')} | "
            f"联赛: {league} | 球种: {sport} | 比分: {score_str} | 盘口线: {total_line}\n"
            f"请只输出以下JSON（不要多余字段，不要markdown）:\n"
            f"结构化特征矩阵: {json.dumps(feature_matrix or {}, ensure_ascii=False, separators=(',', ':'))[:3200]}\n"
            '{"under_confidence": 0.0-1.0, "over_confidence": 0.0-1.0, '
            '"reasoning": "一句话理由", "risk_level": "low/medium/high"}\n'
            "规则: under_confidence 和 over_confidence 都必须给出(0.0-1.0)，"
            "都低于0.30表示skip。必须以给定盘口线和特征矩阵为准；至少两个独立维度同向，"
            "存在hard_failures或方向冲突则skip。只输出JSON。"
        )
        try:
            timeout = min(float(settings.DEEPSEEK_TIMEOUT_SEC), 30.0)
            raw = await asyncio.wait_for(self._call_deepseek(mini_prompt, system_extra), timeout=timeout)
            content = raw.get("content", "")
            parsed = self._parse_analysis_result(content)
            # 只要有 under_confidence 或 over_confidence 就算成功
            if parsed.get("under_confidence") is not None or parsed.get("over_confidence") is not None:
                return parsed
            if parsed.get("prediction") in ("under", "over", "skip"):
                return parsed
            logger.warning(
                "[AI分析] 精简 prompt 重试仍无效 match=%s | content=%s",
                match_info.get("id"), (content or "")[:200],
            )
            return None
        except asyncio.TimeoutError:
            logger.warning("[AI分析] 精简 prompt 重试超时 match=%s", match_info.get("id"))
            return None
        except Exception as e:
            logger.warning("[AI分析] 精简 prompt 重试失败 match=%s: %s", match_info.get("id"), e)
            return None

    async def _call_deepseek(self, prompt: str, system_extra: str = "") -> dict:
        """调用 DeepSeek 模型，返回 content 和 _meta 信息。429/529/空响应指数退避，超时重试1次。

        Args:
            prompt: 用户 prompt
            system_extra: 注入到 system prompt 末尾的额外上下文（如历史胜率反馈）
        """
        if not self.client or not self.model:
            raise RuntimeError("DeepSeek 模型未配置")
        _system_content = (
            "你是专业体育赛事滚球大小球分析师。只输出JSON。\n"
            "## 核心原则\n"
            "1. 量化优先：每个判断必须有具体数字支撑（盘口变化幅度、进球节奏、余量）\n"
            "2. 信号区分：必须区分「资金推动型」和「数学调整型」盘口变化\n"
            "3. 节奏校准：0球时不能用pace=0推演全场，改用联赛均值（足球约2.5球/场）\n"
            "4. 反向风险：高置信度(≥0.73)存在反向相关，over/under均封顶0.72\n"
            "5. 下半场爆发：0-0或1球不代表安全，弱队对强队时下半场可能集中爆发\n\n"
            "## 常见错误（必须避免）\n"
            "- 把数学调整型升盘当资金推动型升盘（当前进球≥原盘口线时的升盘是数学调整）\n"
            "- 0-0时用pace=0线性外推全场0球（概率仅8%）\n"
            "- 忽略杯赛强弱悬殊的历史数据失真\n"
            "- 对高线(≥3.0)0-0场景给出高under置信度（市场看大但暂未爆发）\n"
            "- 对0-0+30分钟以上的比赛仍用当前0球推算（后段进球概率上升）"
        )
        if system_extra:
            _system_content += f"\n\n{system_extra}"
        messages = [
            {"role": "system", "content": _system_content},
            {"role": "user", "content": prompt},
        ]
        started = time.perf_counter()
        max_retries = 2  # 最多 2 次重试（共 3 次调用），确保总时长可控
        content = ""
        # 每轮 max_tokens：超时后降配重试，减少输出量缩短响应时间
        tokens_per_attempt = [settings.LLM_MAX_TOKENS, max(512, settings.LLM_MAX_TOKENS // 2)]
        for attempt in range(max_retries + 1):
            cur_max_tokens = tokens_per_attempt[min(attempt, len(tokens_per_attempt) - 1)]
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=settings.LLM_TEMPERATURE,
                    max_tokens=cur_max_tokens,
                    response_format={"type": "json_object"},
                )
                if not response.choices:
                    raise RuntimeError("DeepSeek 返回空 choices（内容可能被安全过滤）")
                content = response.choices[0].message.content or ""
                if content.strip():
                    break
                # 空内容（上游流被截断，latency~2s 快速返回）：与 529 同样退避重试
                if attempt < max_retries:
                    backoff = 2 ** attempt  # 1, 2
                    logger.warning("[DeepSeek] 空响应 content_len=0，%ds 后重试 (attempt %d/%d)", backoff, attempt + 1, max_retries)
                    await asyncio.sleep(backoff)
                    continue
                break  # 最后一次仍为空，交给上层无效结果兜底
            except Exception as e:
                err_str = str(e).lower()
                # 超时：首次超时降配重试 1 次（减半 max_tokens，缩短响应时间）
                if "timeout" in err_str or "timed out" in err_str or "apitimeout" in type(e).__name__.lower():
                    if attempt == 0:
                        logger.warning(
                            "[DeepSeek] 超时，降配重试 (max_tokens %d→%d) (attempt 1/%d)",
                            settings.LLM_MAX_TOKENS, cur_max_tokens, max_retries,
                        )
                        await asyncio.sleep(0.5)
                        continue
                    raise
                # 429 限流 / 529 上游过载：指数退避（1s -> 2s），仅重试 2 次
                if (
                    "429" in err_str or "rate" in err_str or "ratelimit" in type(e).__name__.lower()
                    or "529" in err_str or "upstream stream ended" in err_str or "overloaded" in err_str
                ):
                    if attempt < max_retries:
                        backoff = 2 ** attempt  # 1, 2
                        logger.warning("[DeepSeek] %s 限流/过载，%ds 后重试 (attempt %d/%d)", "429" if "429" in err_str else "529", backoff, attempt + 1, max_retries)
                        await asyncio.sleep(backoff)
                        continue
                    raise
                # 其他错误：最多重试 1 次
                if attempt == 0:
                    logger.debug("DeepSeek first call failed (%s), retry", e)
                    await asyncio.sleep(0.5)
                    continue
                raise
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info("[DeepSeek] 调用完成 latency=%dms content_len=%d", int(elapsed_ms), len(content))
        return {"content": content, "_meta": {"latency_ms": elapsed_ms, "model": "deepseek"}}

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
    def _apply_market_odds_constraint(analysis: dict, match_info: dict) -> dict:
        """P1 市场赔率约束：市场强烈看小时限制 over 置信度。

        当 over 赔率 >= 3.0 且 under 赔率 <= 1.35 时，市场强烈看小，
        over 置信度上限 0.45（在 signal_review/floor 之后生效，覆盖 floor）。
        """
        prediction = str(analysis.get("prediction") or "").lower()
        if prediction != "over":
            return analysis

        odds_data = match_info.get("odds") or {}
        try:
            over_odds = float(odds_data.get("over") or 0)
            under_odds = float(odds_data.get("under") or 0)
        except (TypeError, ValueError):
            return analysis

        if over_odds <= 0 or under_odds <= 0:
            return analysis

        if over_odds >= 3.0 and under_odds <= 1.35:
            conf = float(analysis.get("confidence") or 0)
            if conf > 0.45:
                analysis["confidence_before_market_constraint"] = round(conf, 4)
                analysis["confidence"] = 0.45
                analysis["market_odds_constraint"] = (
                    f"over赔率{over_odds:.2f}≥3.0 且 under赔率{under_odds:.2f}≤1.35，"
                    f"市场强烈看小，over置信度封顶0.45"
                )
                analysis["reasoning"] = (
                    f"[市场约束] over赔率{over_odds:.2f}≥3.0 under赔率{under_odds:.2f}≤1.35 → "
                    f"市场强烈看小，over置信度封顶0.45 | "
                    + str(analysis.get("reasoning") or "")
                )[:900]
                logger.info(
                    "[P1/市场约束] over conf %.2f → 0.45 (over_odds=%.2f under_odds=%.2f)",
                    conf, over_odds, under_odds,
                )
        return analysis

    @staticmethod
    def _apply_line_up_constraint(
        analysis: dict, match_info: dict, market_odds: Optional[dict],
    ) -> dict:
        """升盘型 over 约束：当升盘是数学调整（非资金推动）时，over 置信度封顶 0.62。

        数学调整型升盘的判定条件（满足任一即可）：
        1. 升盘 + 当前总分 >= 盘口线 - 1.0（已进球导致盘口自动上调）
        2. 升盘 + over 水位上升（市场不买 over，庄家被动调线）

        实际数据：conf 0.73 的 over 全部有"升盘"关键词，0% 胜率（1输1走）。
        封顶 0.62 确保数学调整型升盘的 over 低于 A3 门槛 0.65，被正确过滤。
        """
        prediction = str(analysis.get("prediction") or "").lower()
        if prediction != "over":
            return analysis

        # 提取盘口变化数据
        line_moves = None
        if isinstance(market_odds, dict):
            lm = market_odds.get("line_movements")
            if isinstance(lm, dict):
                line_moves = lm.get("total") or {}
            elif isinstance(lm, list) and lm and isinstance(lm[-1], dict):
                line_moves = lm[-1]

        if not isinstance(line_moves, dict) or not line_moves:
            # 从 match_info 的 odds 中提取
            odds_data = match_info.get("odds") or {}
            line_moves = odds_data.get("line_movement") or {}

        if not isinstance(line_moves, dict) or not line_moves:
            return analysis

        line_delta = line_moves.get("line_delta")
        if line_delta is None:
            return analysis

        try:
            ld = float(line_delta)
        except (TypeError, ValueError):
            return analysis

        # 只处理升盘（line_delta >= 0.25）
        if ld < 0.25:
            return analysis

        # 条件1：当前总分接近盘口线 → 数学调整
        total_line = None
        if match_info.get("total_line"):
            try:
                total_line = float(match_info["total_line"])
            except (TypeError, ValueError):
                pass
        if total_line is None and match_info.get("line"):
            try:
                total_line = float(match_info["line"])
            except (TypeError, ValueError):
                pass
        if total_line is None and analysis.get("line"):
            try:
                total_line = float(analysis["line"])
            except (TypeError, ValueError):
                pass

        hs = match_info.get("home_score")
        as_ = match_info.get("away_score")
        current_total = None
        if hs is not None and as_ is not None:
            try:
                current_total = int(hs) + int(as_)
            except (TypeError, ValueError):
                pass

        is_math_adjustment = False
        reason_parts = []

        if total_line is not None and current_total is not None:
            margin = total_line - current_total
            if margin <= 1.0:
                is_math_adjustment = True
                reason_parts.append(
                    f"数学调整型升盘（当前{current_total}球，线{total_line:.2f}，余量仅{margin:.2f}球）"
                )

        # 条件2：over 水位上升 → 市场不买 over
        odds_delta = line_moves.get("odds_delta")
        if odds_delta is not None:
            try:
                if isinstance(odds_delta, dict):
                    o_od = odds_delta.get("over")
                    o_f = float(o_od) if o_od is not None else None
                    if o_f is not None and o_f > 0:
                        is_math_adjustment = True
                        reason_parts.append(f"升盘但over水位上升{o_f:+.3f}（市场不买over）")
                else:
                    o_f = float(odds_delta)
                    if o_f > 0.08:
                        is_math_adjustment = True
                        reason_parts.append(f"升盘但水位上升{o_f:+.3f}（市场不买over）")
            except (TypeError, ValueError):
                pass

        if not is_math_adjustment:
            return analysis

        conf = float(analysis.get("confidence") or 0)
        if conf > 0.62:
            analysis["confidence_before_line_up_constraint"] = round(conf, 4)
            analysis["confidence"] = 0.62
            analysis["line_up_constraint"] = (
                f"升盘Δ{ld:+.2f} + {'; '.join(reason_parts)}，"
                f"判定为数学调整型升盘，over置信度封顶0.62"
            )
            analysis["reasoning"] = (
                f"[升盘约束] {'; '.join(reason_parts)} → "
                f"over置信度封顶0.62 | "
                + str(analysis.get("reasoning") or "")
            )[:900]
            logger.info(
                "[升盘约束] over conf %.2f → 0.62 (line_delta=%+.2f, %s)",
                conf, ld, "; ".join(reason_parts),
            )
        return analysis

    async def _build_historical_feedback(self, match_info: dict) -> str:
        """构建历史胜率反馈文本，注入 DeepSeek system prompt。

        从 recent_betting_stats 获取近7天按方向/运动/置信度/盘口线区间的实际胜率，
        让 DeepSeek 知道自己哪些预测区间准确、哪些偏差大，从而自我校正。
        """
        try:
            from app.services.bet_settlement import recent_betting_stats
            stats = await recent_betting_stats(days=7)
            parts: list[str] = []
            # 全局概览
            settled = stats.get("settled", 0)
            if settled < 3:
                return ""  # 样本太少不注入
            wr = stats.get("win_rate")
            if isinstance(wr, (int, float)):
                parts.append(f"## 历史表现反馈（近7天 {settled} 注结算）")
                parts.append(f"整体胜率: {wr*100:.1f}%")

            # 按运动+方向交叉统计（足球/篮球 under/over 胜率差异大）
            by_sport = stats.get("by_sport") or {}
            by_sel = stats.get("by_selection") or {}
            sport_l = str(match_info.get("sport") or "football").lower()
            # 当前运动的统计
            cur_sport_stats = by_sport.get(sport_l) or {}
            cur_n = cur_sport_stats.get("settled", 0)
            if cur_n >= 3:
                cur_wr = cur_sport_stats.get("win_rate")
                if isinstance(cur_wr, (int, float)):
                    parts.append(f"当前运动({sport_l}): {cur_n}注 胜率{cur_wr*100:.1f}%")

            # 按方向
            for sel in ("under", "over"):
                sd = by_sel.get(sel) or {}
                n = sd.get("settled", 0)
                if n >= 3:
                    swr = sd.get("win_rate")
                    if isinstance(swr, (int, float)):
                        tag = "偏低，需更保守" if swr < 0.45 else ("偏高，可维持" if swr >= 0.55 else "正常")
                        parts.append(f"{sel} 方向: {n}注 胜率{swr*100:.1f}% ({tag})")

            # 按置信度分桶（结构: {selection: {bucket: {settled, won, lost, win_rate}}}）
            by_conf = stats.get("by_confidence") or {}
            if by_conf:
                conf_warnings: list[str] = []
                for sel_key, conf_store in by_conf.items():
                    if not isinstance(conf_store, dict):
                        continue
                    for bucket, bd in sorted(conf_store.items()):
                        if not isinstance(bd, dict):
                            continue
                        bn = bd.get("settled", 0) or (bd.get("won", 0) + bd.get("lost", 0))
                        if bn < 3:
                            continue
                        bwr = bd.get("win_rate")
                        if isinstance(bwr, (int, float)) and bwr < 0.40:
                            conf_warnings.append(f"{sel_key} conf {bucket}: {bn}注 胜率仅{bwr*100:.0f}%")
                if conf_warnings:
                    parts.append("⚠️ 低胜率置信区间: " + "; ".join(conf_warnings))

            # 按盘口线区间高频亏损模式（结构: {sport_selection: {range: {settled, won, lost, win_rate}}}）
            by_line = stats.get("by_line_range") or {}
            if by_line:
                line_warnings: list[str] = []
                for lr_key, lr_store in by_line.items():
                    if not isinstance(lr_store, dict):
                        continue
                    for lr_range, ld in lr_store.items():
                        if not isinstance(ld, dict):
                            continue
                        ln = ld.get("settled", 0) or (ld.get("won", 0) + ld.get("lost", 0))
                        if ln < 3:
                            continue
                        lwr = ld.get("win_rate")
                        if isinstance(lwr, (int, float)) and lwr < 0.40:
                            line_warnings.append(f"{lr_key} 线{lr_range}: {ln}注 胜率{lwr*100:.0f}%")
                if line_warnings:
                    parts.append("⚠️ 高频亏损盘口区间: " + "; ".join(line_warnings[:4]))

            # 按方向连败序列（by_provider → by_selection 含 loss_streak）
            streak_warnings: list[str] = []
            by_prov = stats.get("by_provider") or {}
            for prov, pd in by_prov.items():
                if not isinstance(pd, dict):
                    continue
                for sel, sd in (pd.get("by_selection") or {}).items():
                    if not isinstance(sd, dict):
                        continue
                    streak = int(sd.get("loss_streak") or 0)
                    if streak >= 3:
                        streak_warnings.append(f"{prov} {sel} 连败{streak}注")
            if streak_warnings:
                parts.append("⚠️ 当前连败: " + "; ".join(streak_warnings))

            if len(parts) <= 1:
                return ""
            feedback = "\n".join(parts)
            logger.info("[历史反馈] 注入 system prompt | %d 注结算 | %s", settled, feedback.replace("\n", " | "))
            return feedback
        except Exception as e:
            logger.debug("[历史反馈] 构建失败(跳过): %s", e)
            return ""

    async def _apply_historical_calibration(self, analysis: dict, match_info: dict) -> dict:
        """基于历史投注结果校准 DeepSeek 置信度。

        在所有静态约束（context_cap / signal_review / market_constraint / line_up_constraint）
        之后生效，用实际胜率数据做最后一层校准。

        校准策略：
        - 从 Redis 加载近14天置信度分桶 → 实际胜率映射
        - 将当前 confidence 映射到对应桶的实际胜率
        - 限制单次校准幅度 ±0.15，避免极端样本导致跳变
        - 样本不足时保持原始值
        """
        prediction = str(analysis.get("prediction") or "").lower()
        if prediction not in ("under", "over"):
            return analysis
        try:
            conf = float(analysis.get("confidence") or 0)
        except (TypeError, ValueError):
            return analysis
        if conf <= 0:
            return analysis

        try:
            from app.ai.calibration import calibrate_confidence, load_calibration_table

            cal_table = await load_calibration_table()
            calibrated, explanation = calibrate_confidence(conf, prediction, cal_table)

            if abs(calibrated - conf) > 0.001:
                analysis["confidence_before_calibration"] = round(conf, 4)
                analysis["confidence"] = round(calibrated, 4)
                analysis["calibration_note"] = explanation
                logger.info(
                    "[历史校准] match=%s %s conf %.2f → %.2f | %s",
                    match_info.get("id"), prediction, conf, calibrated, explanation,
                )
        except Exception as e:
            logger.debug("[历史校准] 跳过(异常): %s", e)

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
        performance = trend.get("performance") if isinstance(trend.get("performance"), dict) else {}
        market_odds = trend.get("market_odds") if isinstance(trend.get("market_odds"), dict) else {}
        tables = performance.get("tables") if isinstance(performance.get("tables"), list) else []
        if not tables and isinstance(trend.get("tables"), list):
            tables = trend.get("tables") or []
        market_tables = market_odds.get("tables") if isinstance(market_odds.get("tables"), list) else []
        if not tables and not market_tables:
            return {"supportive": False, "points": 0, "reason": ""}
        points = 1
        if len(tables) >= 2 or len(market_tables) >= 2:
            points += 1
        return {
            "supportive": True,
            "points": points,
            "reason": (
                "NowScore盘路表现已采集；公司赔率表也已验证"
                if market_tables
                else "NowScore盘路表现已采集（不作为初盘）"
            ),
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
        evidence = ctx.get("total_market_evidence") if isinstance(ctx.get("total_market_evidence"), dict) else {}
        has_fundamentals = evidence.get("usable") is True
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
        selected_odds = _to_float(current_odds.get(selection), 0.0)

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
        elif selection == "over":
            if line_delta >= 0.25:
                supportive = True
                points += 2
                reasons.append(f"初指{open_line:.2f}升至即时{cur_line:.2f}")
            elif line_delta <= -0.25:
                conflict = True
                reasons.append(f"盘口从{open_line:.2f}降到{cur_line:.2f}")

        if selection in ("under", "over") and selected_odds <= 1.0:
            conflict = True
            reasons.append("当前方向赔率无效")

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
        if selection == "over" and delta >= conflict_delta:
            return {"supportive": True, "conflict": False, "points": 2, "reason": f"实时节奏高于盘口预期 {delta:.2f} 球"}
        if selection == "over" and delta <= -support_delta:
            return {"supportive": False, "conflict": True, "points": 0, "reason": f"实时节奏低于盘口预期 {abs(delta):.2f} 球"}
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
        feature_matrix = ctx.get("total_feature_matrix") if isinstance(ctx.get("total_feature_matrix"), dict) else {}
        if feature_matrix:
            matrix_market = feature_matrix.get("asian_total_market") if isinstance(feature_matrix.get("asian_total_market"), dict) else {}
            matrix_market_direction = str(matrix_market.get("direction") or "neutral").lower()
            opening_live_signal = {
                "supportive": matrix_market_direction == selection,
                "conflict": matrix_market_direction == "conflict" or matrix_market_direction in ("under", "over") and matrix_market_direction != selection,
                "points": 2,
                "reason": (
                    f"亚洲大小球方向={matrix_market_direction}，初盘{matrix_market.get('opening_line')}→"
                    f"即时{matrix_market.get('line')}，类型={matrix_market.get('line_move_type')}"
                ),
            }
            matrix_pace = feature_matrix.get("pace") if isinstance(feature_matrix.get("pace"), dict) else {}
            matrix_pace_direction = str(matrix_pace.get("direction") or "neutral").lower()
            pace_signal = {
                "supportive": matrix_pace_direction == selection,
                "conflict": matrix_pace_direction in ("under", "over") and matrix_pace_direction != selection,
                "points": 2,
                "reason": (
                    f"进球节奏方向={matrix_pace_direction}，调整后投影{matrix_pace.get('adjusted_projection')}，"
                    f"盘口{matrix_pace.get('line')}，可靠度={matrix_pace.get('reliability')}"
                ),
            }
        else:
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

        exact_evidence = ctx.get("total_market_evidence") if isinstance(ctx.get("total_market_evidence"), dict) else {}
        evidence_consensus = exact_evidence.get("consensus") if isinstance(exact_evidence.get("consensus"), dict) else {}
        evidence_direction = str(evidence_consensus.get("direction") or "neutral").lower()
        if exact_evidence.get("usable") is True:
            if evidence_direction == selection:
                fundamental_points += 3
                support_reasons.append(
                    f"NowScore历史样本按当前精确盘口{_to_float(line, 0.0):g}重算后支持{selection}"
                    if line is not None else f"NowScore精确盘口样本支持{selection}"
                )
            elif evidence_direction in ("under", "over") and evidence_direction != selection:
                conflict_points += 3
                conflict_reasons.append(
                    f"NowScore精确盘口样本方向为{evidence_direction}，与模型{selection}冲突"
                )
            if evidence_consensus.get("home_away_conflict") is True:
                conflict_points += 2
                conflict_reasons.append("NowScore主客队近期大小球样本明显冲突")

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
            # NowScore 盘路是历史基本面，不能计入投注平台盘口分。
            fundamental_points += int(trend_signal.get("points") or 0)
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
        # triad 部分就绪判定：2/3 信号齐（开盘+实时盘口）但缺基本面
        has_opening = triad_status.get("has_opening")
        has_live_market = triad_status.get("has_live_market")
        has_fundamentals = triad_status.get("has_fundamentals")
        triad_partial = (not triad_ready) and has_opening and has_live_market and (not has_fundamentals)
        if sport == "basketball" and selection == "under":
            if not triad_ready:
                if triad_partial:
                    # P1: 有开盘+实时盘口但缺基本面 → 放宽（-0.22→-0.10, cap 0.44→0.55）
                    confidence_delta -= 0.10
                    confidence_cap = 0.55
                    conflict_reasons.append("篮球三重门禁缺失:基本面（盘口双信号就绪）")
                else:
                    confidence_delta -= 0.22
                    confidence_cap = 0.44
                    missing_bits = []
                    if not has_opening:
                        missing_bits.append("初指")
                    if not has_live_market:
                        missing_bits.append("实时盘口")
                    if not has_fundamentals:
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
                # P2: 强信号 floor 从 0.60 提到 0.65，让 triad 齐全+强信号能跨过 A3 门槛
                confidence_floor = max(confidence, 0.65)
        elif not triad_ready:
            if triad_partial:
                # P1: 有开盘+实时盘口但缺基本面 → 放宽（-0.18→-0.10, cap 0.49→0.55）
                confidence_delta -= 0.10
                confidence_cap = 0.55
                conflict_reasons.append("三重门禁缺失:基本面（盘口双信号就绪）")
            else:
                confidence_delta -= 0.18
                confidence_cap = 0.49
                missing_bits = []
                if not has_opening:
                    missing_bits.append("初指")
                if not has_live_market:
                    missing_bits.append("实时盘口")
                if not has_fundamentals:
                    missing_bits.append("基本面")
                if missing_bits:
                    conflict_reasons.append("三重门禁缺失:" + "/".join(missing_bits))
        # 配置 B: edge_score 门槛从 10 降到 8（与篮球对齐），market/fund 从 5 降到 4
        elif market_points >= 4 and fundamental_points >= 4 and conflict_points == 0 and edge_score >= 8:
            confidence_delta += 0.05
            # P2: 强信号 floor 0.65，让 triad 齐全+强信号能跨过 A3 门槛
            confidence_floor = max(confidence, 0.65)
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
        elif bet_type == "total" and selection == "over":
            if direction == "line_up":
                signals.append("supportive")
            elif direction == "line_down":
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
            elif selection == "over" and avg_total >= float(line) + support_gap:
                supportive = True
                reason = f"近况总进球均值 {avg_total:.2f} 高于盘口 {float(line):.2f}"
            elif selection == "over" and avg_total <= float(line) - conflict_gap:
                conflict = True
                reason = f"近况总进球均值 {avg_total:.2f} 偏低"
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
                elif selection == "over" and avg_total >= float(line) + margin:
                    supportive = True
                    reason = f"交锋总进球均值 {avg_total:.2f} 偏大"
                elif selection == "over":
                    over_rate = _to_float(summary.get("over_2_5_rate"), None)
                    if over_rate and over_rate >= 0.7:
                        supportive = True
                        reason = f"交锋大球率 {over_rate:.0%}"
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
                elif selection == "over" and expected_total >= float(line) + margin:
                    supportive = True
                    reason = f"联赛攻防推导总分 {expected_total:.2f} 偏大"
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
            elif selection == "over" and mins >= 65:
                # 下半场后段是进球高发期：65' 后追大球时机上合理（余量由闸门 D1 把关）
                supportive = True
                reason = "65分钟后进入进球高发期"
        elif sport == "basketball":
            if selection == "under" and mins >= 44:
                conflict = True
                reason = "篮球最后4分钟犯规与罚球波动大，不利于小分"
            elif selection == "over" and mins >= 44:
                # 对称提示：Q4 末段罚球刷分利大分，但领先方压节奏利小分，双向波动
                supportive = True
                reason = "篮球末节犯规战术+罚球易刷分，利大分"
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
        # 先从完整上下文派生统计，再压缩原始明细；不能先删除 summary 后再计算。
        raw_h2h = historical_data.get("h2h") if isinstance(historical_data, dict) else None
        precomputed_stat_signals = self._build_statistical_signals(
            historical_data, market_odds, match_info, raw_h2h,
        )
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
            dim_data["历史交锋"] = bool(isinstance(h2h_block, dict) and h2h_block.get("matches"))
            dim_data["球队近期状态"] = bool(
                isinstance(home_form, dict) and home_form.get("matches")
                and isinstance(away_form, dict) and away_form.get("matches")
            )
            standings_ctx = historical_data.get("standings") if isinstance(historical_data.get("standings"), dict) else {}
            dim_data["联赛积分排名"] = bool(standings_ctx.get("home") or standings_ctx.get("away"))
            analysis_ctx = historical_data.get("analysis") if isinstance(historical_data.get("analysis"), dict) else {}
            dim_data["分析页"] = any(
                analysis_ctx.get(key) for key in ("injuries", "features", "compare", "analysis_tables")
            )
            trend_ctx = historical_data.get("trend") if isinstance(historical_data.get("trend"), dict) else {}
            dim_data["盘路表现"] = (
                trend_ctx.get("performance") or trend_ctx.get("tables") or None
            )

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
            "历史交锋", "球队近期状态", "联赛积分排名", "分析页", "盘路表现", "亚洲盘", "盘口变化",
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
                prompt += (
                    "\n## NowScore盘路表现（不是投注平台实时盘口）\n"
                    f"{json.dumps(trend_data, ensure_ascii=False, separators=(',', ':'))}\n"
                )

            feature_matrix = historical_data.get("total_feature_matrix")
            exact_evidence = historical_data.get("total_market_evidence")
            if exact_evidence and not feature_matrix:
                prompt += (
                    "\n## NowScore 历史样本对当前盘口的精确对照\n"
                    f"{json.dumps(exact_evidence, ensure_ascii=False, separators=(',', ':'))}\n"
                    "> 规则：line 仅来自投注平台当前全场大小球盘口；所有历史样本均按该精确线重新统计。"
                    "不得把 NowScore 盘路表解释成投注平台初盘或即时盘。\n"
                )

            if feature_matrix:
                prompt += (
                    "\n## 全场大小球结构化特征矩阵（最高优先级）\n"
                    f"{json.dumps(feature_matrix, ensure_ascii=False, separators=(',', ':'))}\n"
                    "> 判断顺序：先检查 gates，再看 match_state 比赛阶段，然后分别判断亚洲盘口、"
                    "实时节奏和 NowScore 基本面。至少两个独立维度同向且无硬冲突才可选择 under/over；"
                    "否则必须 skip。不得自行改写矩阵中的比赛时间、比分、盘口或赔率。\n"
                )

        # 统计信号汇总：预计算量化指标注入 prompt，减少 LLM 主观偏差
        stat_signals = precomputed_stat_signals
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

        total_line = _line_for_pick(market_odds, match_info, "total")
        prompt += (
            "\n## 投注市场（全场大小球）\n"
            "- 只分析全场大小球(total)的 under 与 over 两个方向\n"
            "- 其他玩法(胜负/让球/特殊盘/串关)一律不分析不下注\n"
            "- over 与 under 是对称的独立分析：哪边信号强判哪边，都不足则 skip\n"
            f"- 盘口线 total_line: {total_line if total_line is not None else '未知'}"
            f"{score_hint}\n"
        )
        flat = _flatten_market_odds(market_odds)
        # 分析时刻快照：明确声明当前比分和盘口线，防止 DeepSeek 使用历史中间版本
        hs_val = match_info.get("home_score")
        as_val = match_info.get("away_score")
        total_goals_now = int((hs_val or 0) + (as_val or 0)) if hs_val is not None or as_val is not None else None
        prompt += (
            f"\n> 【分析时刻快照】当前比分: {hs_val}-{as_val} (总进球{total_goals_now})，"
            f"即时盘口线: {total_line}。reasoning 中的数字必须与此快照一致。\n"
        )
        if markets_block and "total" in markets_block:
            # markets_block['total'] 已含 opening 初指与变盘明细（over/under 双向水位），不再重复展开独立摘要
            prompt += f"- 当前大小球盘口（含 opening/变盘）: {json.dumps(markets_block['total'], ensure_ascii=False, separators=(',', ':'))}\n"
        elif flat:
            prompt += f"- 当前大小球赔率: {json.dumps(flat, ensure_ascii=False, separators=(',', ':'))}\n"
        # 实时比分分析：计算进球节奏和剩余需求（大小球双向视角）
        score_analysis = ""
        if match_info.get("home_score") is not None and match_info.get("away_score") is not None:
            hs = int(match_info.get("home_score") or 0)
            aws = int(match_info.get("away_score") or 0)
            current_goals = hs + aws
            if total_line:
                remaining_small_margin = max(0, total_line - current_goals + 0.5)
                goals_needed_over = max(0, total_line - current_goals + 0.5)
                score_analysis = f"""当前总得分 {current_goals}，盘口线 {total_line}。
- 小球视角：剩余容错 {remaining_small_margin} 分（再进超过此分数小球输）
- 大球视角：还需 {goals_needed_over} 分大球赢

"""
            # 精确计算已进行分钟和剩余分钟
            played_mins_calc = None
            try:
                from app.services.bookmakers.match_live import match_elapsed_seconds
                elapsed_secs = match_elapsed_seconds(
                    sport=sport,
                    period=str(match_info.get("period") or ""),
                    clock=str(match_info.get("clock") or "").strip(),
                )
                if elapsed_secs is not None:
                    played_mins_calc = elapsed_secs / 60.0
                else:
                    from app.services.bookmakers.match_live import parse_match_clock_minutes
                    played_mins_calc = parse_match_clock_minutes(
                        str(match_info.get("clock") or "").strip(), allow_countdown=False
                    )
            except Exception:
                pass

            if played_mins_calc and played_mins_calc > 0:
                full_mins_calc = 48 if sport == "basketball" else 90
                remain_mins_calc = max(0, full_mins_calc - played_mins_calc)
                if current_goals > 0:
                    pace = current_goals / played_mins_calc
                    linear_proj = pace * full_mins_calc
                    score_analysis += (
                        f"得分节奏: {current_goals}分/{played_mins_calc:.1f}分钟 = {pace:.4f}分/分钟。"
                        f"线性外推全场 {linear_proj:.1f} 分。剩余 {remain_mins_calc:.1f} 分钟。\n"
                    )
                    # 加权投影（基于时间分布模型）
                    if total_line:
                        # 大球可达性分析
                        needed_over = max(0, total_line + 0.5 - current_goals)
                        # 足球后段衰减
                        if sport != "basketball" and played_mins_calc > 50 and (total_line - current_goals) <= 1.0:
                            late_factor = 0.7 if played_mins_calc < 60 else (0.5 if played_mins_calc < 75 else 0.3)
                            decayed_remaining = pace * remain_mins_calc * late_factor
                            score_analysis += (
                                f"- 大球衰减分析: 余量薄({total_line - current_goals:.1f})+{played_mins_calc:.0f}'后段，"
                                f"后段节奏衰减至{late_factor:.0%}，预期剩余{decayed_remaining:.1f}分，"
                                f"需{needed_over:.1f}分（{'可达' if decayed_remaining >= needed_over else '不足'}）\n"
                            )
                        else:
                            expected_remaining = pace * remain_mins_calc
                            score_analysis += (
                                f"- 大球按当前节奏预期剩余 {expected_remaining:.1f} 分，"
                                f"需 {needed_over:.1f} 分（{'可达' if expected_remaining >= needed_over else '不足'}），"
                                f"线性外推全场 {linear_proj:.1f} 分（{'达到' if linear_proj >= total_line + 0.5 else '未达到'}盘口线 {total_line}）\n"
                            )
                        # 小球余量分析
                        margin_under = total_line - current_goals
                        if sport != "basketball":
                            # 足球后段进球概率分布
                            if remain_mins_calc > 0:
                                # 60-75' 高发期进球概率更高
                                high_risk_window = max(0, min(15, 75 - played_mins_calc))  # 落在60-75'窗口的剩余分钟
                                if high_risk_window > 0:
                                    score_analysis += (
                                        f"- 小球余量风险: {margin_under:.1f}球余量，"
                                        f"60-75'进球高发期剩余{high_risk_window:.0f}分钟（进球权重22%）\n"
                                    )
                        score_analysis += (
                            f"- 小球余量: {margin_under:.1f}球，"
                            f"剩余{remain_mins_calc:.0f}分钟内进{margin_under + 0.5:.0f}球即破盘\n"
                        )
                else:
                    # 0球场景
                    league_avg = 2.5 if sport != "basketball" else 150.0
                    score_analysis += (
                        f"0球@{played_mins_calc:.0f}'，pace=0不可用于外推。"
                        f"联赛均值{league_avg}球/场，剩余{remain_mins_calc:.0f}分钟预期"
                        f"约{league_avg * remain_mins_calc / full_mins_calc:.1f}球。\n"
                    )
                    if total_line:
                        score_analysis += (
                            f"- 小球余量: {total_line:.1f}球全量，0球@{played_mins_calc:.0f}'仍较安全"
                            f"（但下半场爆发风险存在）\n"
                        )

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
    "bet_type": "total",
    "prediction": "under | over | skip",
    "line": {json.dumps(total_line)},
    "confidence": 0.0-1.0,
    "under_confidence": 0.0-1.0,
    "over_confidence": 0.0-1.0,
    "under_reasoning": "小球方向的量化分析（盘口信号+节奏+基本面）",
    "over_reasoning": "大球方向的量化分析（盘口信号+节奏+基本面）",
    "reasoning": "【强制声明】当前比分: X-X (总进球X球)，即时盘口线: X.XX，初盘: X.XX，盘口变化: 升/降X.XX → 然后写分析理由",
    "key_factors": ["因素1", "因素2", "因素3"],
    "risk_level": "low/medium/high",
    "value_bets": [
        {{"selection": "under或over", "bet_type": "total", "reason": "为什么有价值"}}
    ]
}}

注意：
- 必须同时给出 under_confidence 和 over_confidence（0.0-1.0），分别量化两个方向的信号强度
- prediction 取 under_confidence 和 over_confidence 中更高的一方（都不足0.30则 skip）
- skip 时 confidence=0.0；bet_type 只能是 total
- line 必须原样返回上方投注平台 total_line，不得改成初盘、历史盘口或四舍五入后的值
- **reasoning 必须以「当前比分: X-X (总进球X球)，即时盘口线: X.XX」开头**，数字必须与上方提供的「当前总得分」「盘口线 total_line」完全一致，禁止使用盘口历史中的中间版本值
- reasoning 必须包含具体数字；只输出JSON。
"""
        # 小球严格分析规则（按运动类型分离）
        if sport == "basketball":
            prompt += (
                "\n## 小球严格分析规则 - 篮球\n"
                "### 小球方向\n"
                "选 under 需要形成同向证据链（盘口/节奏/基本面至少2项同向，其中必须含盘口或基本面）：\n"
                "1) 盘口方向支持 under（满足其一即算强支持）：\n"
                "   - 降盘≥5分（强烈小分信号，水位无反向大幅上升即成立）\n"
                "   - 降盘2-5分 且 小分水位未大幅上升（涨幅<2%即算中性偏支持）\n"
                "   - 小分水位下降>2%（滚球水位波动小，2%即为有效信号）\n"
                "2) 实时得分节奏慢于盘口预期（偏差>20%）\n"
                "3) 基本面支持 under（交锋场均<160/近况得分低/防守型球队）\n"
                "4) 低分走势占优\n"
                "盘口强信号（降盘≥5分）+任一其他同向证据 -> 可给 conf 0.55-0.65\n"
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
                "选 under 需要形成同向证据链（盘口/节奏/基本面至少2项同向，其中必须含盘口或基本面）：\n"
                "1) 盘口方向支持 under（满足其一即算支持）：\n"
                "   - 降盘≥0.25球且 under 水位未大幅上升（涨幅<2%即算支持）\n"
                "   - 小球水位下降>2%（滚球水位波动小，2%即为有效信号）\n"
                "   注意：降盘+under水位上升=庄家平衡资金，不作为under支持信号\n"
                "2) 实时进球节奏慢于盘口预期（偏差>20%）\n"
                "   **关键规则：如果按当前进球节奏推算全场进球≥盘口线（pace×90≥line），under必须skip**\n"
                "   这表示当前比赛节奏已超过盘口线，押under是逆势操作，实盘0%胜率\n"
                "3) 基本面支持 under（交锋场均<1.5球/近况进球少/防守型球队）\n"
                "当前比分远低于盘口线（余量≥2球）也构成有利证据\n"
                "仅满足1项且无节奏配合 -> skip\n"
                "4) 注意：75分钟后进球概率上升，under 需更谨慎\n"
                "5) 0球比赛特判：前30分钟0-0不代表全场0球，足球全场0-0概率仅约8%。\n"
                "   不能用0球/N分钟推演全场0球。0球时under信号需用联赛均值进球率作为后段基准，\n"
                "   而非当前pace=0。\n"
                "   **0-0高线陷阱**：当盘口线≥3.0且当前0-0时，市场预期多进球但暂未爆发，\n"
                "   这种情况under风险极高（实盘0-0@23'/line=3.5→终场5球）。0-0+高线时under conf上限0.45。\n"
                "6) **under置信度上限0.72**：高conf under存在反向风险（conf≥0.74实盘0%胜率），\n"
                "   不得给出≥0.74的under置信度。信号再强也封顶0.72。\n"
                "7) **下半场爆发风险**：半场0-1或1球不代表安全，弱队对强队时强队可能在下半场集中爆发。\n"
                "   杯赛/联赛差异大的对阵（如德国杯韦恩vs勒沃库森），弱队防守在下半场体能下降后崩溃风险高。\n"
                "   此类对阵即使上半场节奏慢，under conf也不应超过0.65。\n\n"
                "### 进球速率衰减规则\n"
                "线性外推会高估后段进球，但仅在特定高风险场景需修正：\n"
                "- 仅当线性外推全场总球仅比盘口线高0-0.5球（余量薄）且比赛>50分钟时，才应用衰减：\n"
                "  60-75分钟：后段预期 = pace × 0.7；75-90分钟：后段预期 = pace × 0.5\n"
                "- 当线性外推全场总球比盘口线高>1球（余量充足）时，无需衰减，pace保持原值\n"
                "- 0球特判：pace=0时不能用0推演，改用联赛均值（足球约2.5球/场）\n\n"
                "### 联赛波动性分级\n"
                "- 黑名单联赛（冰岛/澳大利亚NPL/新南威尔士/威尔士/北欧低级/东欧低级/青少年/女子/友谊赛）：\n"
                "  已完全禁止下注，AI不需要分析此类联赛\n"
                "- 正常联赛（五大联赛次级/主流联赛）：正常评估\n"
                "- 低波动联赛（防守型联赛）：under可适当提升\n\n"
                "### 历史数据可靠性\n"
                "- 历史交锋来自第三方源，可能存在队名匹配偏差\n"
                "- 历史交锋仅作参考，权重低于实时盘口和比分节奏\n"
                "- 交锋均值与当前盘口线差异>30%时，数据可能不可靠，不作为方向依据\n"
                "- **低级联赛（丙级/丁级/地区联赛）历史数据样本量小、可靠性低**\n"
                "  近5场场均进球等统计可能因对手差异极大而失真，不应作为高置信度的主要依据\n"
                "  实盘教训：威尔士联赛历史交锋均4.5球→实际3球输over，波兰丙级近5场均失0.8→实际3球输under\n"
                "- **杯赛强弱悬殊对阵**：杯赛中不同级别球队对阵时，历史数据可比性极低\n"
                "  弱队近5场在低级联赛的进球/失球数据对强队比赛无参考价值\n"
                "  实盘教训：德国杯韦恩(低级)vs勒沃库森(德甲)，韦恩近5场均失0.8→实际被灌4球\n\n"
                "### 通用规则\n"
                "- 无基本面数据时，小球需 conf>=0.40 且双信号一致\n"
                "- 三类信号（初指/实时盘口/基本面）全矛盾 -> 必须 skip\n"
                "- confidence 必须与信号强度匹配，不得虚高\n"
            )

        # 大球严格分析规则（与小球规则并列，双向分析）
        if sport == "basketball":
            prompt += (
                "\n## 大球严格分析规则 - 篮球\n"
                "### 大球方向\n"
                "选 over 需要形成同向证据链（盘口/节奏/基本面至少2项同向，其中必须含盘口或基本面）：\n"
                "1) 盘口方向支持 over（满足其一即算强支持）：\n"
                "   - 升盘≥5分 且 大分水位下降或持平（资金推动型升盘）\n"
                "   - 升盘2-5分 且 大分水位下降>2%\n"
                "   - 大分水位下降>2%（滚球水位波动小，2%即为有效信号）\n"
                "   注意：升盘+大分水位上升=庄家因已得分做数学调整，不是市场看大，不作为over信号\n"
                "2) 实时得分节奏快于盘口预期（偏差>20%）\n"
                "3) 基本面支持 over（交锋场均>170/近况得分高/进攻型球队）\n"
                "4) 高分走势占优\n"
                "盘口强信号（资金推动型升盘≥5分+水位下降）+任一其他同向证据 -> 可给 conf 0.55-0.65\n"
                "5) 注意：Q4后段若落后方犯规战术+罚球更利刷分，但领先方控节奏压时间利小分；44分钟后 over 默认更谨慎，弱信号必须 skip\n\n"
                "### 进球速率衰减规则\n"
                "- 篮球Q4后段：pace波动大，领先方控节奏压时间→得分减速；落后方犯规罚球→可能加速\n"
                "- 不能线性外推Q1-Q3的pace到Q4\n\n"
                "### 通用规则\n"
                "- 无基本面数据时，篮球大球不能给高置信度；弱信号直接 skip\n"
                "- 若初指、实时盘口、基本面三者未形成同向支持，over 优先 skip\n"
                "- 三类信号全矛盾 -> 必须 skip\n"
                "- confidence 必须与信号强度匹配，不得虚高\n"
            )
        else:
            prompt += (
                "\n## 大球严格分析规则 - 足球\n"
                "### 大球方向\n"
                "选 over 需要形成同向证据链（盘口/节奏/基本面至少2项同向，其中必须含盘口或基本面）：\n"
                "1) 盘口方向支持 over（必须区分升盘类型！）：\n"
                "   ### 升盘类型判断（关键！）\n"
                "   升盘不一定代表市场看大，必须区分：\n"
                "   a) **数学调整型升盘**：盘口线跟随当前已进球数自动上调\n"
                "      - 特征：当前总球≥原盘口线，盘口随进球上调\n"
                "      - 例：原盘口3.0，当前3-0，盘口升到3.5 → 这是数学调整，不是市场看大\n"
                "      - 处理：不作为over支持信号，conf不提升\n"
                "   b) **资金推动型升盘**：盘口线上升但当前进球数远低于新盘口线\n"
                "      - 特征：当前总球<原盘口线，盘口仍然上升 且 over水位下降\n"
                "      - 例：原盘口2.5，当前0-0，盘口升到3.0，over水位下降 → 可能是资金看大\n"
                "      - 处理：可作为over支持信号\n"
                "   c) 水位验证：升盘后over水位下降才是真看大；over水位上升则是数学调整\n"
                "   - 升盘≥0.25球 且 over水位下降>2%（资金推动型）\n"
                "   - over水位下降>2%（滚球水位波动小，2%即为有效信号）\n"
                "2) 实时进球节奏快于盘口预期（偏差>20%）\n"
                "3) 基本面支持 over（交锋场均>2.8球/近况大球率高/进攻型球队）\n"
                "   注意：历史交锋来自第三方源可能匹配偏差，交锋均值与盘口线差异>30%时不作为依据\n"
                "当前比分已接近盘口线（line-当前球数≤1）也构成有利证据\n"
                "仅满足1项且无节奏配合 -> skip\n"
                "4) 注意：75分钟后时间所剩无几，需当前已进 X 球使 line-X<=1 才考虑 over；差2球及以上必须 skip\n\n"
                "### 进球速率衰减规则\n"
                "线性外推会高估后段进球，但仅在特定高风险场景需修正：\n"
                "- 仅当线性外推全场总球仅比盘口线高0-0.5球（余量薄）且比赛>50分钟时，才应用衰减：\n"
                "  60-75分钟：后段预期 = pace × 0.7；75-90分钟：后段预期 = pace × 0.5\n"
                "- 当线性外推全场总球比盘口线高>1球（余量充足）时，无需衰减，pace保持原值\n"
                "- 当前比分已接近盘口线（line-当前球数≤1）时，剩余需1球即达线，衰减影响小，正常评估\n"
                "- 例：当前2球/50分钟，pace=0.04，线3.25，外推3.6球（余量0.35薄）→ 衰减后3.1球（<线）→ over信号减弱\n"
                "  但当前3球/50分钟，pace=0.06，线3.5，外推5.4球（余量1.9充足）→ 无需衰减，over信号保留\n\n"
                "### 通用规则\n"
                "- 无基本面数据时，大球需 conf>=0.40 且双信号一致\n"
                "- 三类信号（初指/实时盘口/基本面）全矛盾 -> 必须 skip\n"
                "- confidence 必须与信号强度匹配，不得虚高\n"
                "- **over置信度上限0.72**：高conf over存在反向风险（conf≥0.73实盘仅33%胜率），\n"
                "  升盘+快节奏三项同向时容易过度自信，但足球后段进球不确定性高。\n"
                "  不得给出≥0.73的over置信度。信号再强也封顶0.72。\n\n"
            )

        # Few-shot 示例：正确分析 vs 错误分析对比
        prompt += (
            "\n## 分析示例（学习正确判断模式，避免常见错误）\n"
            "### ✅ 正确分析示例1：under pace投影超标 → skip\n"
            "场景：足球 54分钟 3-1（4球），盘口线5.75，降盘5.75→5.75无变化\n"
            "判断：pace=4/54×90=6.7>线5.75，当前节奏已超线，under逆势→skip\n"
            "关键：不用降盘信号盖过pace事实，pace投影≥线时under必须skip\n\n"
            "### ✅ 正确分析示例2：over资金推动型升盘 + 节奏快 → over conf 0.65\n"
            "场景：足球 26分钟 1-0（1球），盘口2.25→2.75升盘0.5，over水位降7.3%\n"
            "判断：当前1球<原盘口2.25，升盘非数学调整；pace=1/26×90=3.46>线2.75（+26%）；\n"
            "三项同向（升盘+水位降+节奏快）→ over conf 0.65（不超0.72）\n\n"
            "### ❌ 错误分析示例1：数学调整型升盘误判为资金推动\n"
            "场景：足球 74分钟 3-0（3球），盘口3.0→3.5升盘0.5，over水位降3%\n"
            "错误判断：升盘+水位降→over conf 0.65\n"
            "正确判断：当前3球≥原盘口3.0，升盘是数学调整非资金推动；74分钟差0.5球→over conf 0.55\n\n"
            "### ❌ 错误分析示例2：0-0高线under过度自信\n"
            "场景：足球 23分钟 0-0，盘口线3.5（市场看大），降盘4.0→3.5\n"
            "错误判断：降盘0.5+0球→under conf 0.68\n"
            "正确判断：盘口≥3.0且0-0，市场预期多进球但暂未爆发；0球时pace=0不能用，\n"
            "改用联赛均值2.5球/场推算后段预期≈2.0球，总预期≈2.0<3.5线有支持但风险高→under conf 0.45\n\n"
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
        text = (raw or "").strip()
        if not text:
            logger.warning("LLM返回空内容")
            return {}

        # 1) 去除 markdown 代码块包裹
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()

        # 2) 提取最外层 { ... }
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]

        # 3) 尝试直接解析
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # 4) 截断 JSON 修复：max_tokens 不足时 JSON 被截断，尝试补全
        try:
            fixed = self._repair_truncated_json(text)
            if fixed:
                result = json.loads(fixed)
                logger.warning(
                    "LLM返回截断JSON已修复 | 原始末尾=%s | 修复后字段=%s",
                    text[-60:],
                    list(result.keys()) if isinstance(result, dict) else "?",
                )
                return result
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # 5) 字段级正则提取兜底：从原始文本中提取关键字段
        extracted = self._extract_fields_from_text(raw or "")
        if extracted:
            logger.warning(
                "LLM返回非标准JSON，字段级提取成功 | 字段=%s | 原始前200字=%s",
                list(extracted.keys()),
                (raw or "")[:200],
            )
            return extracted

        logger.warning("LLM返回无法解析: %s", (raw or "")[:300])
        return {}

    @staticmethod
    def _repair_truncated_json(text: str) -> str | None:
        """尝试修复被 max_tokens 截断的 JSON。

        策略：找到最后一个完整的键值对，在其后补全缺失的括号。
        """
        if not text or not text.strip().startswith("{"):
            return None
        text = text.strip()
        # 已经是合法 JSON 的话不需要修复
        try:
            json.loads(text)
            return None
        except json.JSONDecodeError:
            pass

        # 找最后一个完整的字符串值结尾（"后跟,或}）
        last_complete = text.rfind('",')
        if last_complete < 0:
            # 找最后一个完整的数字/布尔值结尾
            for pattern in (',"', ",}", ",]", "true", "false", "null"):
                idx = text.rfind(pattern)
                if idx > last_complete:
                    last_complete = idx
        if last_complete < 0:
            return None

        # 截断到最后一个完整值，补全括号
        truncated = text[: last_complete + 1]
        # 计算需要补的括号
        opens = truncated.count("{") - truncated.count("}")
        brackets = truncated.count("[") - truncated.count("]")
        # 去掉末尾多余的逗号
        truncated = truncated.rstrip().rstrip(",")
        repaired = truncated + ("}" * max(0, opens)) + ("]" * max(0, brackets))
        return repaired

    @staticmethod
    def _extract_fields_from_text(text: str) -> dict:
        """从非标准 JSON 文本中正则提取关键字段。"""
        if not text:
            return {}
        result = {}

        # prediction: under/over/skip
        m = re.search(r'"prediction"\s*:\s*"?(under|over|skip)"?', text, re.IGNORECASE)
        if m:
            result["prediction"] = m.group(1).lower()

        # bet_type: total
        m = re.search(
            r'"bet_type"\s*:\s*"?(total)"?',
            text, re.IGNORECASE,
        )
        if m:
            result["bet_type"] = m.group(1).lower()

        # confidence: 浮点数
        m = re.search(r'"confidence"\s*:\s*([0-9.]+)', text)
        if m:
            try:
                result["confidence"] = float(m.group(1))
            except ValueError:
                pass

        # line: 必须保留精确四分之一盘口，供权威盘口一致性校验
        m = re.search(r'"line"\s*:\s*([0-9.]+)', text)
        if m:
            try:
                result["line"] = float(m.group(1))
            except ValueError:
                pass

        # under_confidence
        m = re.search(r'"under_confidence"\s*:\s*([0-9.]+)', text)
        if m:
            try:
                result["under_confidence"] = float(m.group(1))
            except ValueError:
                pass

        # over_confidence
        m = re.search(r'"over_confidence"\s*:\s*([0-9.]+)', text)
        if m:
            try:
                result["over_confidence"] = float(m.group(1))
            except ValueError:
                pass

        # reasoning: 提取字符串值（截断时可能没有结尾引号）
        m = re.search(r'"reasoning"\s*:\s*"([^"]*)', text)
        if m:
            result["reasoning"] = m.group(1)[:800]

        # risk_level
        m = re.search(r'"risk_level"\s*:\s*"?(low|medium|high)"?', text, re.IGNORECASE)
        if m:
            result["risk_level"] = m.group(1).lower()

        # 至少要有 prediction 或 under/over_confidence 才认为提取成功
        if "prediction" in result or "under_confidence" in result or "over_confidence" in result:
            return result
        return {}

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
                h2h_signal = {
                    "played": played,
                    "home_win_rate": round(hw / played, 3),
                    "draw_rate": round(d / played, 3),
                    "away_win_rate": round(aw / played, 3),
                    "avg_total_goals": summary.get("avg_total_goals", 0),
                }
                if sport not in ("basketball", "basket"):
                    h2h_signal["under_2_5_rate"] = summary.get("under_2_5_rate", 0)
                signals["h2h_baseline"] = h2h_signal

        # --- 2b. 近期状态进球统计 ---
        if isinstance(historical_data, dict):
            for side, key in (("home", "home_form"), ("away", "away_form")):
                form = historical_data.get(key)
                if isinstance(form, dict):
                    fs = form.get("summary") or {}
                    if fs:
                        form_signal = {
                            "played": fs.get("played", 0),
                            "win_rate": fs.get("win_rate", 0),
                            "avg_total_goals": fs.get("avg_total_goals", 0),
                        }
                        if sport not in ("basketball", "basket"):
                            form_signal["under_2_5_rate"] = fs.get("under_2_5_rate", 0)
                        signals[f"{side}_form_stats"] = form_signal
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

                # 市场方向判断（双向：降盘/under水位降=看小；升盘/over水位降=看大）
                market_support = "neutral"
                signal_strength = "weak"
                if line_delta is not None:
                    try:
                        ld = float(line_delta)
                        if ld <= -0.25:
                            market_support = "under"
                            signal_strength = "strong" if abs(ld) >= 0.5 else "medium"
                        elif ld >= 0.25:
                            market_support = "over"
                            signal_strength = "strong" if abs(ld) >= 0.5 else "medium"
                    except (TypeError, ValueError):
                        pass

                # 赔率变化增强信号：odds_delta 是 dict（如 {"under":0.04,"over":-0.05}），
                # 修复：此前 float(dict) 必 TypeError 被静默吞掉，水位信号从未生效。
                # 语义：某方向水位下降=该方向被打水（市场倾向该方向）。
                if odds_delta is not None and market_support == "neutral":
                    try:
                        if isinstance(odds_delta, dict):
                            u_od = odds_delta.get("under")
                            o_od = odds_delta.get("over")
                            u_od = float(u_od) if u_od is not None else None
                            o_od = float(o_od) if o_od is not None else None
                        else:
                            u_od = o_od = None
                            od = float(odds_delta)
                            # 单数值回退语义（历史约定）：水位上升=看小
                            if od <= -0.08:
                                market_support = "under"
                                signal_strength = "medium"
                            elif od >= 0.08:
                                market_support = "over"
                                signal_strength = "medium"
                        if u_od is not None and u_od <= -0.05 and (o_od is None or o_od > 0):
                            market_support = "under"  # under 水位降且 over 未同步降
                            signal_strength = "medium"
                        elif o_od is not None and o_od <= -0.05 and (u_od is None or u_od > 0):
                            market_support = "over"   # over 水位降且 under 未同步降
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

        # --- 5. 实时节奏量化（pace投影 + 联赛基准对比） ---
        hs = match_info.get("home_score")
        aws = match_info.get("away_score")
        if hs is not None or aws is not None:
            current_total = int((hs or 0) + (aws or 0))
            total_line_val = _line_for_pick(market_odds, match_info, "total")
            if total_line_val is not None:
                try:
                    tl = float(total_line_val)
                    # 尝试从 clock 解析已进行分钟数
                    played_mins = None
                    try:
                        from app.services.bookmakers.match_live import (
                            match_elapsed_seconds,
                            parse_match_clock_minutes,
                        )
                        elapsed_secs = match_elapsed_seconds(
                            sport=sport,
                            period=str(match_info.get("period") or ""),
                            clock=clock,
                        )
                        if elapsed_secs is not None:
                            played_mins = elapsed_secs / 60.0
                        else:
                            played_mins = parse_match_clock_minutes(clock, allow_countdown=False)
                    except Exception:
                        pass

                    pace_signal: dict[str, Any] = {
                        "current_total": current_total,
                        "line": tl,
                        "margin": round(tl - current_total, 2),
                    }
                    if played_mins and played_mins > 0:
                        full_mins = 90.0 if sport != "basketball" else 48.0
                        remain_mins = max(0.0, full_mins - played_mins)
                        pace = current_total / played_mins
                        projection = pace * full_mins

                        # ── 精细化节奏分析：基于时间分布的进球概率模型 ──
                        # 足球进球时间分布（经验统计）：
                        #   0-15': 12%  15-30': 15%  30-45': 18%  45-60': 17%
                        #   60-75': 22%  75-90': 16%
                        # 篮球得分分布：
                        #   Q1: 22%  Q2: 23%  Q3: 25%  Q4: 30%（犯规+罚球加速）
                        if sport == "basketball":
                            # 篮球四节得分权重
                            quarter_weights = [0.22, 0.23, 0.25, 0.30]
                            quarter_mins = full_mins / 4  # 12分钟/节
                            # 已完成节的权重总和
                            completed_quarters = int(played_mins / quarter_mins)
                            completed_weight = sum(quarter_weights[:completed_quarters])
                            # 剩余时间的权重
                            remain_weight = sum(quarter_weights[completed_quarters:])
                            # 当前节内已完成比例
                            intra_q_progress = (played_mins % quarter_mins) / quarter_mins
                            if completed_quarters < 4:
                                current_q_partial = quarter_weights[completed_quarters] * intra_q_progress
                                completed_weight += current_q_partial
                                remain_weight = sum(quarter_weights[completed_quarters:]) - current_q_partial
                            # 按权重分布推算全场进球
                            if completed_weight > 0:
                                weighted_projection = current_total / completed_weight
                                # 剩余时间预期进球 = 全场预期 × 剩余权重
                                expected_remaining = weighted_projection * remain_weight
                                # 篮球后段加速：Q4 得分权重 0.30 > Q1 的 0.22
                                # 线性外推低估后段，加权模型更准确
                                pace_signal["weighted_projection"] = round(weighted_projection, 2)
                                pace_signal["weighted_expected_remaining"] = round(expected_remaining, 2)
                                pace_signal["quarter_weights"] = quarter_weights
                                pace_signal["completed_weight"] = round(completed_weight, 3)
                                pace_signal["remain_weight"] = round(remain_weight, 3)
                        else:
                            # 足球15分钟段进球权重
                            segment_weights = [0.12, 0.15, 0.18, 0.17, 0.22, 0.16]  # 6段×15分钟
                            segment_mins = 15.0
                            completed_segs = int(played_mins / segment_mins)
                            completed_weight = sum(segment_weights[:completed_segs])
                            # 当前段内已完成比例
                            intra_seg_progress = (played_mins % segment_mins) / segment_mins
                            if completed_segs < 6:
                                current_seg_partial = segment_weights[completed_segs] * intra_seg_progress
                                completed_weight += current_seg_partial
                                remain_weight = sum(segment_weights[completed_segs:]) - current_seg_partial
                            else:
                                remain_weight = 0.0
                            # 按权重分布推算全场进球
                            if completed_weight > 0:
                                weighted_projection = current_total / completed_weight
                                expected_remaining = weighted_projection * remain_weight
                                # 60-75' 是进球高发期（权重0.22），线性外推会低估这段
                                # 75-90' 权重0.16略低于均值，线性外推略高估
                                pace_signal["weighted_projection"] = round(weighted_projection, 2)
                                pace_signal["weighted_expected_remaining"] = round(expected_remaining, 2)
                                pace_signal["segment_weights"] = segment_weights
                                pace_signal["completed_weight"] = round(completed_weight, 3)
                                pace_signal["remain_weight"] = round(remain_weight, 3)

                            # 足球后段进球衰减规则（仅余量薄时生效）
                            # 线性外推高估后段进球，但仅在特定场景需修正
                            margin = tl - current_total
                            if played_mins > 50 and margin <= 1.0:
                                # 余量薄+后半段：后段进球概率衰减
                                # 60-75': 保留 70% pace；75-90': 保留 50% pace
                                if remain_mins > 0:
                                    late_factor = 0.7 if played_mins < 60 else (0.5 if played_mins < 75 else 0.3)
                                    decayed_remaining = pace * remain_mins * late_factor
                                    pace_signal["decayed_expected_remaining"] = round(decayed_remaining, 2)
                                    pace_signal["decay_factor"] = late_factor
                                    pace_signal["decay_note"] = (
                                        f"余量薄({margin:.1f})+{played_mins:.0f}'后段，"
                                        f"后段节奏衰减至{late_factor:.0%}"
                                    )

                        # 0球特判：pace=0 不能用于推演
                        if current_total == 0:
                            league_avg = 2.5 if sport != "basketball" else 150.0
                            # 按时间权重计算剩余预期进球
                            if sport != "basketball" and remain_weight > 0:
                                expected_remaining_by_avg = league_avg * remain_weight
                            else:
                                expected_remaining_by_avg = league_avg * remain_mins / full_mins
                            pace_signal["league_avg_baseline"] = league_avg
                            pace_signal["expected_remaining_by_avg"] = round(expected_remaining_by_avg, 2)
                            pace_signal["zero_zero_warning"] = True
                            pace_signal["zero_zero_note"] = (
                                f"0球@{played_mins:.0f}'，pace=0不可用，"
                                f"按联赛均值{league_avg}×剩余权重={expected_remaining_by_avg:.1f}球"
                            )

                        pace_signal["played_mins"] = round(played_mins, 1)
                        pace_signal["remain_mins"] = round(remain_mins, 1)
                        pace_signal["pace"] = round(pace, 4)
                        pace_signal["pace_projection"] = round(projection, 2)
                        pace_signal["pace_vs_line"] = round(projection - tl, 2)
                        pace_signal["pace_above_line"] = projection >= tl
                    signals["live_pace_analysis"] = pace_signal
                except (TypeError, ValueError):
                    pass

        # --- 6. 盘口线 vs 历史交锋均值偏差 ---
        h2h_avg = (signals.get("h2h_baseline") or {}).get("avg_total_goals", 0)
        form_avg = signals.get("form_combined_expected_goals", 0)
        total_line_val2 = _line_for_pick(market_odds, match_info, "total")
        if total_line_val2 is not None and (h2h_avg or form_avg):
            try:
                tl2 = float(total_line_val2)
                deviations = {}
                if h2h_avg:
                    deviations["h2h_vs_line_pct"] = round((h2h_avg - tl2) / tl2 * 100, 1)
                    deviations["h2h_reliable"] = abs(h2h_avg - tl2) / tl2 < 0.3
                if form_avg:
                    deviations["form_vs_line_pct"] = round((form_avg - tl2) / tl2 * 100, 1)
                if deviations:
                    signals["line_vs_history"] = deviations
            except (TypeError, ValueError):
                pass

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
                        market_support = "under" if ld <= -0.25 else "over" if ld >= 0.25 else "neutral"
                        signals["line_change"] = {
                            "initial": open_line,
                            "current": total_line,
                            "delta": ld,
                            "magnitude": abs(ld),
                            "direction": "line_up(支持大球)" if ld > 0 else "line_down(支持小球)" if ld < 0 else "stable",
                            "market_support": market_support,
                            "signal_strength": "strong" if abs(ld) >= 0.5 else "medium" if abs(ld) >= 0.25 else "weak",
                        }
                    except (TypeError, ValueError):
                        pass

                if odds_delta is not None:
                    try:
                        # odds_delta 是 dict（under/over 各自水位差），修复 float(dict) 死代码
                        if isinstance(odds_delta, dict):
                            u_od = odds_delta.get("under")
                            o_od = odds_delta.get("over")
                            u_f = float(u_od) if u_od is not None else None
                            o_f = float(o_od) if o_od is not None else None
                            parts = []
                            if u_f is not None and abs(u_f) >= 0.05:
                                parts.append(
                                    f"小球水位{'下降(市场支持小球)' if u_f < 0 else '上升(不利小球)'} Δ{u_f:+.3f}"
                                )
                            if o_f is not None and abs(o_f) >= 0.05:
                                parts.append(
                                    f"大球水位{'下降(市场支持大球)' if o_f < 0 else '上升(不利大球)'} Δ{o_f:+.3f}"
                                )
                            if parts:
                                signals["odds_change"] = {
                                    "odds_delta": odds_delta,
                                    "signal": "；".join(parts),
                                }
                        else:
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
                            "signal": "节奏明显慢于预期(>30%偏差)→利小球" if pace_deviation < -30 else "节奏明显快于预期(>30%偏差)→利大球" if pace_deviation > 30 else "节奏偏慢(利小球弱信号)" if pace_deviation < -15 else "节奏偏快(利大球弱信号)" if pace_deviation > 15 else "节奏接近预期(偏差<15%，噪音)",
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
