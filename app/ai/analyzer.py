"""
AI 赛事分析引擎 - 多模型 Ensemble（gpt/deepseek/doubao/kimi/minimax）

仅做大小球(total)分析。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timezone
import time
from typing import Optional, Any

from openai import AsyncOpenAI

from app.config import settings
from app.core.cache import cache

logger = logging.getLogger(__name__)

VALID_PREDICTIONS = {"over", "under"}
VALID_BET_TYPES = {"total"}

_PRED_ALIASES = {
    "over": "over",
    "under": "under",
    "o": "over",
    "u": "under",
    "大": "over",
    "小": "under",
    "大球": "over",
    "小球": "under",
}

_BT_ALIASES = {
    "total": "total",
    "ou": "total",
    "totals": "total",
    "大小": "total",
    "大小球": "total",
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
    elif "大球" in s or s == "大" or "over" in s:
        pred = "over"
    elif "小球" in s or s == "小" or "under" in s:
        pred = "under"
    else:
        pred = ""

    bt = normalize_bet_type(bet_type)
    if bt == "total" and pred not in ("over", "under"):
        return ""
    if pred not in VALID_PREDICTIONS:
        return ""
    return pred


def _infer_bet_type(prediction: str, declared: str = "") -> str:
    return "total"


def _flatten_market_odds(market_odds: Optional[dict]) -> dict[str, float]:
    """把嵌套 markets 或扁平 odds 合成 selection->odds 映射。"""
    if not market_odds:
        return {}
    if any(k in market_odds for k in ("over", "under", "home", "away", "draw")):
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


def _parse_percent(value: Any) -> Optional[float]:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("%", "").strip()
    try:
        num = float(raw)
    except (TypeError, ValueError):
        return None
    if num > 1:
        num /= 100.0
    if num < 0:
        return None
    return min(num, 1.0)


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


# (短名, KEY 字段, BASE_URL 字段, MODEL 字段)
MODEL_DEFS = [
    ("doubao", "DOUBAO_API_KEY", "DOUBAO_BASE_URL", "DOUBAO_MODEL"),
    ("gpt", "GPT_API_KEY", "GPT_BASE_URL", "GPT_MODEL"),
    ("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"),
    ("kimi", "KIMI_API_KEY", "KIMI_BASE_URL", "KIMI_MODEL"),
    ("minimax", "MINIMAX_API_KEY", "MINIMAX_BASE_URL", "MINIMAX_MODEL"),
    ("glm", "GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL"),
]


class MatchAnalyzer:
    """多模型 Ensemble：每模型独立 OpenAI 兼容客户端。"""

    def __init__(self):
        self.clients: dict[str, AsyncOpenAI] = {}
        self.models: dict[str, str] = {}
        self._init_clients()

    def _init_clients(self):
        fallback_key = (settings.NEWAPI_API_KEY or "").strip() or None
        fallback_base = (settings.NEWAPI_BASE_URL or "").strip() or "https://www.juaiapi.com/v1"
        self.clients.clear()
        self.models.clear()
        for key, key_attr, base_attr, model_attr in MODEL_DEFS:
            api_key = (getattr(settings, key_attr, None) or "").strip() or fallback_key
            base_url = (getattr(settings, base_attr, None) or "").strip() or fallback_base
            model_name = (getattr(settings, model_attr, None) or "").strip()
            if not api_key or not model_name:
                continue
            self.clients[key] = AsyncOpenAI(
                api_key=api_key, base_url=base_url,
                timeout=float(settings.LLM_CLIENT_TIMEOUT_SEC), max_retries=0,
            )
            self.models[key] = model_name
            logger.info("AI model ready: %s -> %s (%s)", key, model_name, base_url)

    async def analyze_match(
        self,
        match_info: dict,
        historical_data: Optional[dict] = None,
        market_odds: Optional[dict] = None,
        news: Optional[list] = None,
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
        line_tag = (
            match_info.get("total_line")
            or match_info.get("spread_line")
            or match_info.get("line")
            or ""
        )
        # v4：多盘口（足球胜负/让球/大小）
        cache_key = f"ai:ensemble:v4:{fk}:{sport}:{line_tag}"
        try:
            cached = await cache.get_json(cache_key)
            if (
                cached
                and cached.get("consensus_reached")
                and cached.get("models_used")
                and not cached.get("error")
                and str(cached.get("prediction") or "") in VALID_PREDICTIONS
                and str(cached.get("bet_type") or "total") in VALID_BET_TYPES
            ):
                logger.info(
                    "[AI分析] 缓存命中 match=%s %s vs %s | pred=%s conf=%.2f models=%s",
                    match_info.get("id"), match_info.get("home_team"), match_info.get("away_team"),
                    cached.get("prediction"), float(cached.get("confidence") or 0),
                    cached.get("models_used"),
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

        prompt = self._build_analysis_prompt(match_info, historical_data, market_odds, news)
        logger.info(
            "[AI分析] Prompt 构建完成 match=%s | 长度=%d 字符 | 含analysis=%s 含live=%s 含trend=%s",
            match_info.get("id"), len(prompt),
            bool(isinstance(historical_data, dict) and historical_data.get("analysis")),
            bool(isinstance(historical_data, dict) and historical_data.get("live")),
            bool(isinstance(historical_data, dict) and historical_data.get("trend")),
        )

        try:
            timeout = float(settings.ENSEMBLE_TIMEOUT_SEC)
            votes = await asyncio.wait_for(self._run_ensemble(prompt), timeout=timeout)
            logger.info(
                "[AI分析] Ensemble 完成 match=%s | 总投票=%d 成功=%d 失败=%d",
                match_info.get("id"), len(votes),
                sum(1 for v in votes if v.get("ok")),
                sum(1 for v in votes if not v.get("ok")),
            )
            analysis = self._aggregate_consensus(votes, market_odds, match_info=match_info)
            logger.info(
                "[AI分析] 共识判定 match=%s | consensus=%s pred=%s conf=%.2f ratio=%.2f models=%s | odds=%.2f",
                match_info.get("id"),
                analysis.get("consensus_reached"),
                analysis.get("prediction"),
                float(analysis.get("confidence") or 0),
                float(analysis.get("consensus_ratio") or 0),
                analysis.get("models_used"),
                float(analysis.get("odds") or 0),
            )
            if analysis.get("consensus_reached") and analysis.get("models_used"):
                try:
                    await cache.set_json(cache_key, analysis, ttl=settings.LLM_CACHE_TTL)
                except Exception:
                    pass
            return analysis

        except asyncio.TimeoutError:
            logger.error(
                "[AI分析] Ensemble 超时 match=%s timeout=%.0fs",
                match_info.get("id"), timeout,
            )
            return self._fallback_result("AI分析超时，改用盘口启发式", error="ensemble_timeout")
        except Exception as e:
            logger.error("[AI分析] Ensemble 失败 match=%s: %s", match_info.get("id"), e)
            return self._fallback_result(f"AI分析暂不可用: {e}", error=str(e))

    def _select_ensemble_models(self) -> list[str]:
        order_raw = str(getattr(settings, "ENSEMBLE_MODEL_ORDER", "") or "")
        order = [x.strip().lower() for x in order_raw.split(",") if x.strip()]
        max_n = max(1, int(settings.ENSEMBLE_MAX_MODELS))
        selected: list[str] = []
        for key in order:
            if key in self.clients and (self.models.get(key) or "") and key not in selected:
                selected.append(key)
        for key in self.clients:
            if key not in selected and (self.models.get(key) or ""):
                selected.append(key)
        if not selected:
            return []

        latency_stats = self._get_model_latency_stats()
        if isinstance(latency_stats, dict) and latency_stats:
            order_index = {key: idx for idx, key in enumerate(selected)}

            def _sort_key(model_key: str):
                row = latency_stats.get(model_key) or {}
                try:
                    samples = int(row.get("count") or 0)
                except (TypeError, ValueError):
                    samples = 0
                try:
                    avg_ms = float(row.get("avg_ms") or 0)
                except (TypeError, ValueError):
                    avg_ms = 0.0
                return (
                    0 if samples > 0 else 1,
                    avg_ms if samples > 0 else 0.0,
                    order_index.get(model_key, 999),
                )

            selected = sorted(selected, key=_sort_key)

        return selected[:max_n]

    def _vote_from_raw(self, name: str, raw) -> dict:
        if isinstance(raw, Exception):
            logger.warning(
                "[AI投票] 模型=%s 调用失败: %s", name, raw,
            )
            return {
                "model": name,
                "ok": False,
                "error": str(raw),
                "prediction": None,
                "bet_type": None,
                "confidence": 0.0,
                "latency_ms": 0.0,
            }
        meta = raw.get("_meta") if isinstance(raw, dict) else {}
        raw_text = raw.get("content", "") if isinstance(raw, dict) else raw
        parsed = self._parse_analysis_result(raw_text)
        bt = normalize_bet_type(parsed.get("bet_type"))
        pred = normalize_prediction(parsed.get("prediction"), bet_type=bt)
        if not bt:
            bt = _infer_bet_type(pred, "")
        latency_ms = float((meta or {}).get("latency_ms") or 0)

        # 仅做大小球：bet_type 必须为 total，prediction 必须为 over/under
        if bt != "total" or pred not in ("over", "under"):
            logger.info(
                "[AI投票] 模型=%s ❌ 无效 | raw_bt=%s raw_pred=%s latency=%dms | reasoning=%s",
                name, parsed.get("bet_type"), parsed.get("prediction"),
                int(latency_ms),
                str(parsed.get("reasoning", ""))[:120],
            )
            return {
                "model": name,
                "ok": False,
                "error": f"invalid pick: bet_type={parsed.get('bet_type')!r} pred={parsed.get('prediction')!r}",
                "prediction": None,
                "bet_type": None,
                "confidence": 0.0,
                "latency_ms": latency_ms,
            }
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

        logger.info(
            "[AI投票] 模型=%s ✅ 有效 | pred=%s conf=%.2f line=%s risk=%s latency=%dms | reasoning=%s",
            name, pred, conf, line_f, parsed.get("risk_level", "?"),
            int(latency_ms),
            str(parsed.get("reasoning", ""))[:120],
        )

        return {
            "model": name,
            "ok": True,
            "error": None,
            "prediction": pred,
            "bet_type": bt,
            "line": line_f,
            "confidence": conf,
            "latency_ms": latency_ms,
            "reasoning": parsed.get("reasoning", ""),
            "key_factors": parsed.get("key_factors", []) or [],
            "risk_level": parsed.get("risk_level", "medium"),
            "value_bets": parsed.get("value_bets", []) or [],
            "raw": parsed,
        }

    async def _run_ensemble(self, prompt: str) -> list[dict]:
        if not self.clients:
            self._init_clients()
        if not self.clients:
            raise RuntimeError(
                "未配置任何模型：请在 .env 为各模型填写独立 *_API_KEY / *_BASE_URL / *_MODEL"
            )

        model_names = self._select_ensemble_models()
        if not model_names:
            raise RuntimeError("未配置任何可用模型名（*_MODEL）")
        speed_meta = self._get_model_latency_stats()
        logger.info(
            "ensemble select models=%s max_models=%s speed_meta=%s",
            model_names,
            int(getattr(settings, "ENSEMBLE_MAX_MODELS", 3) or 3),
            {
                k: {
                    "avg_ms": round(float((speed_meta.get(k) or {}).get("avg_ms") or 0), 1),
                    "count": int((speed_meta.get(k) or {}).get("count") or 0),
                }
                for k in model_names
            } if isinstance(speed_meta, dict) else {},
        )

        quorum = max(1, int(getattr(settings, "ENSEMBLE_QUORUM", 3) or 3))
        quorum = min(quorum, len(model_names))

        task_map: dict[asyncio.Task, str] = {}
        for model_key in model_names:
            model_name = self.models.get(model_key) or ""
            client = self.clients[model_key]
            task_map[
                asyncio.create_task(self._call_model(prompt, model_key, model_name, client))
            ] = model_key

        try:
            votes: list[dict] = []
            pending = set(task_map.keys())
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    name = task_map[t]
                    try:
                        raw = t.result()
                    except Exception as e:
                        raw = e
                    votes.append(self._vote_from_raw(name, raw))
                    last_vote = votes[-1]
                    logger.info(
                        "ensemble vote model=%s ok=%s latency_ms=%s",
                        name,
                        bool(last_vote.get("ok")),
                        int(last_vote.get("latency_ms") or 0),
                    )
                ok_n = sum(1 for v in votes if v.get("ok"))
                if ok_n >= quorum:
                    for t in pending:
                        t.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    logger.info(
                        "ensemble early-exit ok=%s/%s quorum=%s cancelled=%s",
                        ok_n,
                        len(model_names),
                        quorum,
                        len(pending),
                    )
                    break

            ok_votes = [v for v in votes if v["ok"]]
            if not ok_votes:
                errors = "; ".join(f'{v["model"]}: {v["error"]}' for v in votes)
                raise RuntimeError(f"所有模型调用失败: {errors}")

            return votes
        finally:
            # 确保所有 task 被清理（超时/取消时不留孤儿）
            for t in task_map:
                if not t.done():
                    t.cancel()
            if task_map:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*task_map.keys(), return_exceptions=True),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("ensemble cleanup timed out, %d tasks may be orphaned", len(task_map))

    def _aggregate_consensus(
        self,
        votes: list[dict],
        market_odds: Optional[dict],
        *,
        match_info: Optional[dict] = None,
    ) -> dict:
        ok_votes = [
            v for v in votes
            if v["ok"]
            and v.get("prediction") in VALID_PREDICTIONS
            and v.get("bet_type") in VALID_BET_TYPES
        ]
        if not ok_votes:
            logger.info(
                "[AI共识] ❌ 无有效投票 | 总=%d 失败=%d | 失败原因: %s",
                len(votes), sum(1 for v in votes if not v.get("ok")),
                "; ".join(f'{v["model"]}: {v.get("error","?")}' for v in votes if not v.get("ok")),
            )
            return self._fallback_result("无有效模型投票")

        # 共识键：bet_type + prediction
        key_counts = Counter((v["bet_type"], v["prediction"]) for v in ok_votes)
        (winning_bt, winning_pred), win_count = key_counts.most_common(1)[0]
        consensus_ratio = win_count / len(ok_votes)
        min_ratio = settings.ENSEMBLE_MIN_CONSENSUS
        min_votes = min(max(1, settings.ENSEMBLE_MIN_VOTES), len(ok_votes))
        single_model_min_conf = float(0.70)
        if isinstance(match_info, dict):
            raw_conf = match_info.get("strategy_min_confidence", match_info.get("min_confidence"))
            try:
                cfg_conf = float(raw_conf)
                if cfg_conf > 1:
                    cfg_conf /= 100.0
                if 0.0 <= cfg_conf <= 0.99:
                    single_model_min_conf = cfg_conf
            except (TypeError, ValueError):
                pass

        logger.info(
            "[AI共识] 投票明细: %s | 赢家=%s/%s 票数=%d/%d ratio=%.4f | 门槛: min_ratio=%.2f min_votes=%d",
            {v["model"]: f'{v["prediction"]}/{v.get("confidence",0):.2f}' for v in ok_votes},
            winning_bt, winning_pred, win_count, len(ok_votes), consensus_ratio,
            min_ratio, min_votes,
        )
        if len(ok_votes) == 1:
            logger.info(
                "[AI共识] 单模型模式 | conf=%.2f >= 门槛=%.2f => consensus=%s",
                ok_votes[0]["confidence"], single_model_min_conf,
                ok_votes[0]["confidence"] >= single_model_min_conf,
            )
            # 单模型场景不能天然视为“已达共识”，必须额外过当前生效置信度门槛
            consensus_reached = ok_votes[0]["confidence"] >= single_model_min_conf
            if consensus_reached:
                consensus_ratio = 1.0
        else:
            consensus_reached = (
                win_count >= min_votes
                and consensus_ratio + 1e-9 >= min_ratio
            )
            logger.info(
                "[AI共识] 多模型模式 | win_count=%d >= min_votes=%d=%s | ratio=%.4f >= min_ratio=%.2f=%s => consensus=%s",
                win_count, min_votes, win_count >= min_votes,
                consensus_ratio, min_ratio, consensus_ratio + 1e-9 >= min_ratio,
                consensus_reached,
            )

        agreeing = [
            v for v in ok_votes
            if v["prediction"] == winning_pred and v["bet_type"] == winning_bt
        ]
        # 模型动态权重：按历史命中率加权（Redis 存储累计统计）
        model_weights = self._get_model_weights()
        weight_sum = sum(
            model_weights.get(v.get("model", ""), 0.5) * v["confidence"]
            for v in agreeing
        ) or 1.0
        weighted_conf = sum(
            model_weights.get(v.get("model", ""), 0.5) * v["confidence"] ** 2
            for v in agreeing
        ) / weight_sum
        if not consensus_reached:
            weighted_conf *= consensus_ratio

        key_factors: list[str] = []
        value_bets: list[dict] = []
        reason_parts: list[str] = []
        risk_levels: list[str] = []
        lines = []
        for v in agreeing:
            for f in v.get("key_factors", []):
                if f and f not in key_factors:
                    key_factors.append(f)
            for vb in v.get("value_bets", []):
                if vb and vb not in value_bets:
                    value_bets.append(vb)
            if v.get("reasoning"):
                reason_parts.append(f"[{v['model']}] {v['reasoning']}")
            if v.get("risk_level"):
                risk_levels.append(v["risk_level"])
            if v.get("line") is not None:
                lines.append(v["line"])

        risk_level = self._merge_risk(risk_levels)
        reasoning = " | ".join(reason_parts) if reason_parts else "Ensemble 共识分析"
        line = None
        if lines:
            try:
                line = float(sum(lines) / len(lines))
            except (TypeError, ValueError):
                line = lines[0]
        if line is None:
            line = _line_for_pick(market_odds, match_info, winning_bt)

        consensus_votes = {f"{bt}:{pred}": n for (bt, pred), n in key_counts.items()}

        analysis = {
            "prediction": winning_pred,
            "bet_type": winning_bt,
            "line": line,
            "confidence": round(weighted_conf, 4),
            "reasoning": reasoning[:800],
            "key_factors": key_factors[:8],
            "value_bets": value_bets[:5],
            "risk_level": risk_level,
            "consensus_reached": consensus_reached,
            "consensus_votes": consensus_votes,
            "consensus_ratio": round(consensus_ratio, 4),
            "models_used": [v["model"] for v in ok_votes],
            "models_failed": [v["model"] for v in votes if not v["ok"]],
            "ensemble": [
                {
                    "model": v["model"],
                    "ok": v["ok"],
                    "prediction": v.get("prediction"),
                    "bet_type": v.get("bet_type"),
                    "confidence": v.get("confidence", 0),
                    "latency_ms": v.get("latency_ms", 0),
                    "error": v.get("error"),
                }
                for v in votes
            ],
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        }

        od = _odds_for_pick(market_odds, winning_bt, winning_pred)
        if od > 1:
            analysis["odds"] = od

        if not consensus_reached:
            analysis["risk_level"] = "high"
            analysis["reasoning"] = (
                f"[共识不足 {win_count}/{len(ok_votes)}={consensus_ratio:.0%}] "
                + analysis["reasoning"]
            )

        # 记录预测到 Redis（供历史校准和模型权重更新）
        mid = 0
        if isinstance(match_info, dict):
            try:
                mid = int(match_info.get("id") or 0)
            except (TypeError, ValueError):
                mid = 0
        if mid > 0:
            self._record_prediction(
                mid, winning_pred, winning_bt,
                float(analysis.get("confidence") or 0),
                float(analysis.get("odds") or 0),
                votes,
            )

        return analysis

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

        if source not in ("", "none"):
            if completeness >= 0.65:
                fundamental_points += 3
                support_reasons.append("基本面维度较完整")
            elif completeness >= 0.50:
                fundamental_points += 2
            elif completeness >= 0.30:
                fundamental_points += 1

        form_signal = self._recent_form_signal(ctx, selection=selection, bet_type=bet_type, line=line)
        if form_signal["supportive"]:
            fundamental_points += 2
            support_reasons.append(form_signal["reason"])
        elif form_signal["conflict"]:
            conflict_points += 1
            conflict_reasons.append(form_signal["reason"])

        h2h_signal = self._h2h_signal(ctx.get("h2h"), selection=selection, bet_type=bet_type, line=line)
        if h2h_signal["supportive"]:
            fundamental_points += 1
            support_reasons.append(h2h_signal["reason"])
        elif h2h_signal["conflict"]:
            conflict_points += 1
            conflict_reasons.append(h2h_signal["reason"])

        standings_signal = self._standings_signal(ctx.get("standings"), selection=selection, bet_type=bet_type)
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

        if stat_signals:
            stat_signal = self._statistical_alignment_signal(stat_signals, selection=selection, bet_type=bet_type, line=line)
            if stat_signal["supportive"]:
                fundamental_points += 2
                support_reasons.append(stat_signal["reason"])
            elif stat_signal["conflict"]:
                conflict_points += 2
                conflict_reasons.append(stat_signal["reason"])

        if {"home_form", "away_form"}.issubset(fields_present):
            fundamental_points += 1
        if fields_present.intersection({"h2h", "standings"}):
            fundamental_points += 1

        confidence_delta = 0.0
        confidence_cap = None
        confidence_floor = None
        if market_points >= 4 and fundamental_points >= 5 and conflict_points == 0:
            confidence_delta += 0.05
            confidence_floor = max(confidence, 0.60)
        elif market_points >= 3 and fundamental_points >= 3 and conflict_points <= 1:
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
            confidence_cap = min(0.58, confidence_cap) if confidence_cap is not None else 0.58

        verdict = "supportive"
        if conflict_points >= 3:
            verdict = "conflict"
        elif conflict_points > market_points:
            verdict = "mixed"
        elif market_points + fundamental_points < 4:
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
        if bet_type == "total":
            if selection == "over":
                if direction == "line_up":
                    signals.append("supportive")
                elif direction == "line_down":
                    signals.append("adverse")
            elif selection == "under":
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
            if selection == "over" and avg_total >= float(line) + 0.35:
                supportive = True
                reason = f"近况总进球均值 {avg_total:.2f} 高于盘口 {float(line):.2f}"
            elif selection == "under" and avg_total <= float(line) - 0.35:
                supportive = True
                reason = f"近况总进球均值 {avg_total:.2f} 低于盘口 {float(line):.2f}"
            elif selection == "over" and avg_total <= float(line) - 0.25:
                conflict = True
                reason = f"近况总进球均值 {avg_total:.2f} 偏低"
            elif selection == "under" and avg_total >= float(line) + 0.25:
                conflict = True
                reason = f"近况总进球均值 {avg_total:.2f} 偏高"
        return {"supportive": supportive, "conflict": conflict, "reason": reason}

    @staticmethod
    def _h2h_signal(
        h2h: Any,
        *,
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
                if selection == "over" and avg_total >= float(line) + 0.25:
                    supportive = True
                    reason = f"交锋总进球均值 {avg_total:.2f} 偏大"
                elif selection == "under" and avg_total <= float(line) - 0.25:
                    supportive = True
                    reason = f"交锋总进球均值 {avg_total:.2f} 偏小"
        return {"supportive": supportive, "conflict": conflict, "reason": reason}

    @staticmethod
    def _standings_signal(
        standings: Any,
        *,
        selection: str,
        bet_type: str,
    ) -> dict[str, Any]:
        if not isinstance(standings, dict):
            return {"supportive": False, "conflict": False, "reason": ""}
        home = standings.get("home") if isinstance(standings.get("home"), dict) else {}
        away = standings.get("away") if isinstance(standings.get("away"), dict) else {}
        supportive = False
        conflict = False
        reason = ""
        if bet_type == "total":
            home_gf = _to_float(home.get("goals_for"), 0.0)
            away_gf = _to_float(away.get("goals_for"), 0.0)
            home_ga = _to_float(home.get("goals_against"), 0.0)
            away_ga = _to_float(away.get("goals_against"), 0.0)
            attack_sum = home_gf + away_gf
            concede_sum = home_ga + away_ga
            if selection == "over" and attack_sum > 0 and concede_sum > 0 and (attack_sum + concede_sum) >= 4.2:
                supportive = True
                reason = "双方联赛攻防数据偏大球"
            elif selection == "under" and attack_sum > 0 and concede_sum > 0 and (attack_sum + concede_sum) <= 3.0:
                supportive = True
                reason = "双方联赛攻防数据偏小球"
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
        from app.services.bookmakers.match_live import parse_match_clock_minutes

        sport = str(match_info.get("sport") or "").strip().lower()
        clock = str(match_info.get("clock") or "").strip()
        mins = parse_match_clock_minutes(clock, allow_countdown=(sport == "basketball"))
        supportive = False
        conflict = False
        reason = ""
        if mins is None:
            return {"supportive": False, "conflict": False, "reason": ""}
        if sport in ("football", "soccer"):
            if selection == "over" and 60 <= mins <= 75:
                supportive = True
                reason = "足球 60-75 分钟通常更适合追大球"
            elif selection == "under" and mins < 25:
                supportive = True
                reason = "比赛早段节奏通常更谨慎"
        elif sport == "basketball":
            if selection == "over" and mins >= 36:
                supportive = True
                reason = "篮球末节通常更适合大分"
        return {"supportive": supportive, "conflict": conflict, "reason": reason}

    @staticmethod
    def _statistical_alignment_signal(
        stat_signals: dict[str, Any],
        *,
        selection: str,
        bet_type: str,
        line: Optional[float],
    ) -> dict[str, Any]:
        supportive = False
        conflict = False
        reason = ""
        if bet_type in {"moneyline", "spread"}:
            xg = stat_signals.get("standings_xg_diff") if isinstance(stat_signals.get("standings_xg_diff"), dict) else {}
            edge = _to_float(xg.get("edge"), 0.0)
            if selection == "home" and edge >= 0.35:
                supportive = True
                reason = f"xG/积分边际偏向主队 ({edge:.2f})"
            elif selection == "away" and edge <= -0.35:
                supportive = True
                reason = f"xG/积分边际偏向客队 ({edge:.2f})"
        return {"supportive": supportive, "conflict": conflict, "reason": reason}

    @staticmethod
    def _merge_risk(levels: list[str]) -> str:
        order = {"low": 0, "medium": 1, "high": 2}
        if not levels:
            return "medium"
        worst = max(levels, key=lambda x: order.get(str(x).lower(), 1))
        return str(worst).lower() if str(worst).lower() in order else "medium"

    def _build_analysis_prompt(
        self,
        match_info: dict,
        historical_data: Optional[dict],
        market_odds: Optional[dict],
        news: Optional[list],
    ) -> str:
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

            # 收集 8 大分析维度
            dim_data["历史交锋"] = h2h_block
            dim_data["球队近期状态"] = {"home": home_form, "away": away_form} if (home_form or away_form) else None
            dim_data["联赛积分排名"] = historical_data.get("standings") or None

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
            "历史交锋", "球队近期状态", "联赛积分排名", "亚洲盘", "盘口变化",
        ]
        for dn in dim_names:
            dv = dim_data.get(dn)
            if dv:
                dim_available += 1
                dim_lines.append(f"  [{dn}] 有数据")
            else:
                dim_lines.append(f"  [{dn}] 数据缺失")
        dim_summary = f"（{dim_available}/5 维度有数据）"

        if h2h_block:
            prompt += f"\n## 历史交锋记录\n{json.dumps(h2h_block, ensure_ascii=False, separators=(',', ':'))}\n"
        if home_form:
            prompt += (
                f"\n## 主队近10场状态（{match_info.get('home_team', '主队')}）\n"
                f"{json.dumps(home_form, ensure_ascii=False, separators=(',', ':'))}\n"
            )
        if away_form:
            prompt += (
                f"\n## 客队近10场状态（{match_info.get('away_team', '客队')}）\n"
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

        # 分析页 / 直播页 / 走势页额外数据
        if isinstance(historical_data, dict):
            analysis_data = historical_data.get("analysis")
            if analysis_data:
                prompt += f"\n## 分析页额外数据\n{json.dumps(analysis_data, ensure_ascii=False, separators=(',', ':'))}\n"
            live_data = historical_data.get("live")
            if live_data:
                prompt += f"\n## 直播页数据（首发/概率/统计）\n{json.dumps(live_data, ensure_ascii=False, separators=(',', ':'))}\n"
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
            "\n## 投注市场（亚洲大小：仅分析全场大小球）\n"
            f"- 盘口线 total_line: {total_line if total_line is not None else '未知'}"
            f"{score_hint}\n"
        )
        flat = _flatten_market_odds(market_odds)
        if markets_block and "total" in markets_block:
            prompt += f"- 当前大小球（含 opening/变盘）: {json.dumps(markets_block['total'], ensure_ascii=False)}\n"
        elif flat:
            prompt += f"- 当前大小球赔率: {json.dumps(flat, ensure_ascii=False)}\n"
        line_moves = None
        if isinstance(market_odds, dict):
            line_moves = market_odds.get("line_movements")
        if line_moves:
            prompt += f"- 盘口变化摘要: {json.dumps(line_moves, ensure_ascii=False)}\n"
        prompt += f"""
## 分析框架 {dim_summary}
{chr(10).join(dim_lines)}

## 量化分析指引（必须严格遵循）

### 1. 统计信号优先级
- **交锋胜率基线**：若交锋历史多高分场次，倾向大球；多低分场次，倾向小球。
- **xG diff（积分榜期望分差）**：edge 绝对值大表示实力悬殊，弱队可能刷分倾向大球。
- **水位变化信号**：direction="下降(有利-买方)"时大球赔率下降=市场看好大球，可提高 confidence；"上升"时反之。
- **比赛阶段权重**：篮球 Q4 是得分爆发期，此阶段大球概率应上浮；Q1 节奏偏慢，倾向小球。

### 2. 信号一致性校准
- 多数信号方向一致 -> confidence 可达 0.60-0.70
- 信号方向分歧 -> confidence 必须 ≤ 0.50
- 水位变化与基本面矛盾 -> confidence 必须 ≤ 0.45，risk_level=high

### 3. 数据质量约束
- 核心维度 < 3/4 时 confidence 必须 < {0.55}。
- 不得编造缺失数据；缺失维度标注"数据缺失"。
- 统计信号是预计算值，不得质疑或修改，只能在此基础上做调整。
- 须结合盘口升/降水；变盘与基本面冲突时降低 confidence。

## 输出格式（严格JSON）
{{
    "bet_type": "total",
    "prediction": "over 或 under",
    "line": null,
    "confidence": 0.0-1.0,
    "reasoning": "1.统计信号:xG diff=...,交锋胜率=...,水位变化=...,比赛阶段=...2.历史交锋:...3.近期状态:...4.排名:...5.亚洲盘:...6.盘口变化:...综合:信号一致性分析+最终判断",
    "key_factors": ["因素1", "因素2", "因素3"],
    "risk_level": "low/medium/high",
    "value_bets": [
        {{"selection": "over/under", "bet_type": "total", "reason": "为什么有价值"}}
    ]
}}

注意：prediction 只能是 over 或 under；reasoning 必须先分析统计信号再分析各维度；只输出JSON。
"""

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

    async def _call_model(
        self,
        prompt: str,
        model_key: str,
        model_name: str,
        client: AsyncOpenAI,
    ) -> dict:
        messages = [
            {"role": "system", "content": "你是专业体育赛事分析师。只输出JSON。"},
            {"role": "user", "content": prompt},
        ]
        started = time.perf_counter()
        ok = False

        def _is_transient(err: Exception) -> bool:
            """连接错误/DNS失败等网络问题不重试；仅超时/429/500/502/503 重试"""
            err_str = str(err).lower()
            if "connection error" in err_str or ("connection" in err_str and "refused" in err_str):
                return False
            if "name or service not known" in err_str or "nodename" in err_str:
                return False
            return True

        try:
            response = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=settings.LLM_TEMPERATURE,
                max_tokens=settings.LLM_MAX_TOKENS,
            )
        except Exception as e:
            if not _is_transient(e):
                logger.warning("模型 %s 网络错误，不重试: %s", model_key, e)
                raise
            logger.debug("model %s first call failed (%s), retry", model_key, e)
            await asyncio.sleep(0.3)
            response = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=settings.LLM_TEMPERATURE,
                max_tokens=settings.LLM_MAX_TOKENS,
            )
        content = response.choices[0].message.content or ""
        # 空响应重试一次（API 网关偶尔返回 200 OK 但 content 为空）
        if not content:
            logger.debug("model %s returned empty content, retrying", model_key)
            await asyncio.sleep(0.3)
            try:
                response = await client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=settings.LLM_TEMPERATURE,
                    max_tokens=settings.LLM_MAX_TOKENS,
                )
                content = response.choices[0].message.content or ""
            except Exception:
                pass
        ok = bool(content)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        self._record_model_latency(model_key, elapsed_ms, ok=ok)
        return {"content": content, "_meta": {"latency_ms": elapsed_ms, "model": model_key}}

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
            return {}  # 返回空 dict，_vote_from_raw 会标记为 ok=False

    def _fallback_result(self, reason: str, error: Optional[str] = None) -> dict:
        result = {
            "prediction": "under",
            "bet_type": "total",
            "confidence": 0.33,
            "reasoning": reason,
            "key_factors": [],
            "value_bets": [],
            "risk_level": "high",
            "consensus_reached": False,
            "consensus_votes": {},
            "consensus_ratio": 0.0,
            "ensemble": [],
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

        # --- 2. 交锋胜率统计基线 ---
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
                }

        # --- 3. 盘口水位变化信号 ---
        if isinstance(market_odds, dict):
            line_moves = market_odds.get("line_movements")
            if isinstance(line_moves, list) and len(line_moves) >= 2:
                first = line_moves[0] if isinstance(line_moves[0], dict) else {}
                last = line_moves[-1] if isinstance(line_moves[-1], dict) else {}
                try:
                    first_odds = float(first.get("odds") or first.get("price") or 0)
                    last_odds = float(last.get("odds") or last.get("price") or 0)
                    if first_odds > 0 and last_odds > 0:
                        change_pct = round((last_odds - first_odds) / first_odds * 100, 1)
                        signals["odds_movement"] = {
                            "first_odds": first_odds,
                            "latest_odds": last_odds,
                            "change_pct": change_pct,
                            "direction": "上升(不利-买方)" if change_pct > 0 else "下降(有利-买方)" if change_pct < 0 else "稳定",
                        }
                except (TypeError, ValueError):
                    pass

        # --- 4. 比赛阶段权重 ---
        period = str(match_info.get("period") or "").lower()
        clock = str(match_info.get("clock") or "")
        if period or clock:
            # 足球阶段权重：0-15' 开局(低进球), 15-45' 中段, 45-60' 下半场开局, 60-75' 高发期, 75+' 冲刺期
            # 篮球阶段权重：Q1 低, Q2 中, Q3 中, Q4 高
            stage_weight = "unknown"
            if period in ("1h", "first_half", "ht"):
                stage_weight = "上半场(进球率较低)"
            elif period in ("2h", "second_half", "ft"):
                stage_weight = "下半场(进球率高发)"
            elif "q1" in period:
                stage_weight = "Q1(节奏偏慢)"
            elif "q4" in period:
                stage_weight = "Q4(得分爆发期)"
            # 尝试从 clock 提取分钟数
            try:
                import re
                mins = re.search(r"(\d+)", clock)
                if mins:
                    m = int(mins.group(1))
                    if m >= 60 and m <= 75:
                        stage_weight = "60-75分钟(足球进球高发期)"
                    elif m >= 75:
                        stage_weight = "75分钟+(冲刺期,大小球突变)"
            except Exception:
                pass
            signals["match_stage"] = {
                "period": period,
                "clock": clock,
                "stage_weight": stage_weight,
            }

        return signals

    _model_weights_cache: dict[str, float] = {}
    _model_weights_ts: float = 0.0
    _model_latency_cache: dict[str, dict[str, float]] = {}
    _model_latency_ts: float = 0.0

    @classmethod
    def _get_model_weights(cls) -> dict[str, float]:
        """从 Redis 加载模型历史命中率作为动态权重（带 60s 缓存）。

        Redis key: ai:model_weights -> {model_name: hit_rate}
        无记录时返回默认权重 0.5（中性）。
        """
        import time
        now = time.time()
        # 60 秒缓存，避免每次共识聚合都查 Redis
        if cls._model_weights_cache and (now - cls._model_weights_ts) < 60:
            return cls._model_weights_cache
        try:
            import redis
            r = redis.Redis(host="ob-redis", port=6379, socket_timeout=1.0, socket_connect_timeout=1.0)
            data = r.get("ai:model_weights")
            r.close()
            if data:
                import json as _json
                weights = _json.loads(data)
                cls._model_weights_cache = weights
                cls._model_weights_ts = now
                return weights
        except Exception:
            pass
        return {}

    @classmethod
    def _get_model_latency_stats(cls) -> dict[str, dict[str, float]]:
        """从 Redis 读取模型耗时统计，供下次优先选择更快模型。"""
        now = time.time()
        if cls._model_latency_cache and (now - cls._model_latency_ts) < 30:
            return cls._model_latency_cache
        try:
            import redis
            r = redis.Redis(host="ob-redis", port=6379, socket_timeout=1.0, socket_connect_timeout=1.0)
            data = r.get("ai:model_latency_stats")
            r.close()
            if data:
                import json as _json
                stats = _json.loads(data)
                if isinstance(stats, dict):
                    cls._model_latency_cache = stats
                    cls._model_latency_ts = now
                    return stats
        except Exception:
            pass
        return {}

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

    @classmethod
    def _record_model_latency(cls, model_key: str, elapsed_ms: float, ok: bool) -> None:
        """记录模型真实耗时，按 EWMA 更新平均耗时。"""
        try:
            elapsed_ms = max(1.0, float(elapsed_ms or 0))
        except (TypeError, ValueError):
            return
        model = str(model_key or "").strip().lower()
        if not model:
            return

        async def _save():
            import redis.asyncio as aioredis
            r = aioredis.Redis(host="ob-redis", port=6379, socket_timeout=1.0)
            try:
                raw = await r.get("ai:model_latency_stats")
                stats = json.loads(raw) if raw else {}
                if not isinstance(stats, dict):
                    stats = {}
                row = stats.get(model) or {}
                try:
                    prev_avg = float(row.get("avg_ms") or elapsed_ms)
                except (TypeError, ValueError):
                    prev_avg = elapsed_ms
                try:
                    prev_count = int(row.get("count") or 0)
                except (TypeError, ValueError):
                    prev_count = 0
                try:
                    ok_count = int(row.get("ok_count") or 0)
                except (TypeError, ValueError):
                    ok_count = 0
                try:
                    fail_count = int(row.get("fail_count") or 0)
                except (TypeError, ValueError):
                    fail_count = 0
                alpha = 0.35
                avg_ms = elapsed_ms if prev_count <= 0 else round(prev_avg * (1 - alpha) + elapsed_ms * alpha, 2)
                stats[model] = {
                    "avg_ms": avg_ms,
                    "last_ms": round(elapsed_ms, 2),
                    "count": prev_count + 1,
                    "ok_count": ok_count + (1 if ok else 0),
                    "fail_count": fail_count + (0 if ok else 1),
                    "last_ok": bool(ok),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                await r.setex("ai:model_latency_stats", 86400 * 7, json.dumps(stats, ensure_ascii=False))
                cls._model_latency_cache = stats
                cls._model_latency_ts = time.time()
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

analyzer = MatchAnalyzer()
