"""
AI 赛事分析引擎 - 多模型 Ensemble（gpt/deepseek/doubao/kimi/minimax）

足球：在胜负 / 让球 / 大小中选 1 个最佳方向；
篮球：仅全场大小。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Optional, Any

from openai import AsyncOpenAI

from app.config import settings
from app.core.cache import cache

logger = logging.getLogger(__name__)

VALID_PREDICTIONS = {"over", "under", "home", "away", "draw"}
VALID_BET_TYPES = {"total", "moneyline", "spread"}

_PRED_ALIASES = {
    "over": "over",
    "under": "under",
    "o": "over",
    "u": "under",
    "大": "over",
    "小": "under",
    "大球": "over",
    "小球": "under",
    "home": "home",
    "away": "away",
    "draw": "draw",
    "主": "home",
    "客": "away",
    "平": "draw",
    "主胜": "home",
    "客胜": "away",
    "平局": "draw",
    "h": "home",
    "a": "away",
    "d": "draw",
    "x": "draw",
    "1": "home",
    "2": "away",
}

_BT_ALIASES = {
    "total": "total",
    "ou": "total",
    "totals": "total",
    "大小": "total",
    "大小球": "total",
    "moneyline": "moneyline",
    "1x2": "moneyline",
    "ml": "moneyline",
    "胜负": "moneyline",
    "独赢": "moneyline",
    "spread": "spread",
    "ah": "spread",
    "handicap": "spread",
    "asian_handicap": "spread",
    "让球": "spread",
    "让分": "spread",
}


def normalize_bet_type(raw) -> str:
    s = str(raw or "").strip().lower()
    if s in _BT_ALIASES:
        return _BT_ALIASES[s]
    if "让" in s:
        return "spread"
    if "胜负" in s or "独赢" in s or "1x2" in s:
        return "moneyline"
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
    elif "主胜" in s or "home" in s:
        pred = "home"
    elif "客胜" in s or "away" in s:
        pred = "away"
    elif "平" in s or "draw" in s:
        pred = "draw"
    else:
        pred = ""

    bt = normalize_bet_type(bet_type)
    if bt == "total" and pred not in ("over", "under"):
        return ""
    if bt == "moneyline" and pred not in ("home", "away", "draw"):
        return ""
    if bt == "spread" and pred not in ("home", "away"):
        return ""
    if pred not in VALID_PREDICTIONS:
        return ""
    return pred


def _infer_bet_type(prediction: str, declared: str = "") -> str:
    bt = normalize_bet_type(declared)
    if bt:
        return bt
    pred = normalize_prediction(prediction)
    if pred in ("over", "under"):
        return "total"
    if pred == "draw":
        return "moneyline"
    if pred in ("home", "away"):
        return ""  # 歧义：胜负或让球，需模型声明
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


def _devig_odds(odds_dict: dict[str, float]) -> dict[str, float]:
    """去除博彩公司 margin（vig），返回公平赔率。

    原理：1/odds_A + 1/odds_B + ... = 1 + vig
    归一化后 fair_prob_i = (1/odds_i) / sum(1/odds_all)
    fair_odds_i = 1 / fair_prob_i
    """
    inv_sum = 0.0
    invs: dict[str, float] = {}
    for sel, od in odds_dict.items():
        if od and od > 1.0:
            inv = 1.0 / od
            invs[sel] = inv
            inv_sum += inv
    if inv_sum <= 0:
        return odds_dict
    fair: dict[str, float] = {}
    for sel, inv in invs.items():
        fair_prob = inv / inv_sum
        fair[sel] = round(1.0 / fair_prob, 4) if fair_prob > 0 else 0.0
    return fair


def _devig_ev(confidence: float, raw_odds: float, all_odds: dict[str, float]) -> float:
    """用去 vig 后的公平赔率计算 EV。

    EV = confidence × fair_odds - 1
    """
    if not all_odds or raw_odds <= 1.0:
        # 无法去 vig 时回退到原始赔率
        return round(confidence * raw_odds - 1, 4) if raw_odds > 1.0 else 0.0
    fair_odds = _devig_odds(all_odds)
    # 找到当前选择的公平赔率（匹配原始赔率最接近的选择）
    fair_od = 0.0
    best_diff = float("inf")
    for sel, fo in fair_odds.items():
        orig = all_odds.get(sel, 0)
        diff = abs(orig - raw_odds) if orig > 0 else float("inf")
        if diff < best_diff:
            best_diff = diff
            fair_od = fo
    if fair_od <= 1.0:
        return round(confidence * raw_odds - 1, 4)
    return round(confidence * fair_od - 1, 4)


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
                logger.debug(f"AI ensemble 缓存命中: {cache_key}")
                return cached
        except Exception as e:
            logger.debug("AI cache unavailable: %s", e)

        prompt = self._build_analysis_prompt(match_info, historical_data, market_odds, news)

        try:
            timeout = float(settings.ENSEMBLE_TIMEOUT_SEC)
            votes = await asyncio.wait_for(self._run_ensemble(prompt), timeout=timeout)
            analysis = self._aggregate_consensus(votes, market_odds, match_info=match_info)
            analysis = self._apply_context_quality_cap(analysis, historical_data)

            if analysis.get("consensus_reached") and analysis.get("models_used"):
                try:
                    await cache.set_json(cache_key, analysis, ttl=settings.LLM_CACHE_TTL)
                except Exception:
                    pass
            return analysis

        except asyncio.TimeoutError:
            logger.error("AI ensemble 超时")
            return self._fallback_result("AI分析超时，改用盘口启发式", error="ensemble_timeout")
        except Exception as e:
            logger.error(f"AI ensemble 分析失败: {e}")
            return self._fallback_result(f"AI分析暂不可用: {e}", error=str(e))

    def _select_ensemble_models(self) -> list[str]:
        order_raw = str(getattr(settings, "ENSEMBLE_MODEL_ORDER", "") or "")
        order = [x.strip().lower() for x in order_raw.split(",") if x.strip()]
        max_n = max(1, int(settings.ENSEMBLE_MAX_MODELS))
        selected: list[str] = []
        for key in order:
            if key in self.clients and (self.models.get(key) or "") and key not in selected:
                selected.append(key)
            if len(selected) >= max_n:
                return selected
        for key in self.clients:
            if key not in selected and (self.models.get(key) or ""):
                selected.append(key)
            if len(selected) >= max_n:
                break
        return selected

    def _vote_from_raw(self, name: str, raw) -> dict:
        if isinstance(raw, Exception):
            logger.warning("模型 %s 调用失败: %s", name, raw)
            return {
                "model": name,
                "ok": False,
                "error": str(raw),
                "prediction": None,
                "bet_type": None,
                "confidence": 0.0,
            }
        parsed = self._parse_analysis_result(raw)
        bt = normalize_bet_type(parsed.get("bet_type"))
        pred = normalize_prediction(parsed.get("prediction"), bet_type=bt)
        if not bt:
            bt = _infer_bet_type(pred, "")
        if not bt or pred not in VALID_PREDICTIONS:
            return {
                "model": name,
                "ok": False,
                "error": f"invalid pick: bet_type={parsed.get('bet_type')!r} pred={parsed.get('prediction')!r}",
                "prediction": None,
                "bet_type": None,
                "confidence": 0.0,
            }
        # home/away 未声明盘口时默认胜负（更常见）
        if not normalize_bet_type(parsed.get("bet_type")) and pred in ("home", "away"):
            bt = "moneyline"
        if bt == "spread" and pred == "draw":
            return {
                "model": name,
                "ok": False,
                "error": "spread cannot be draw",
                "prediction": None,
                "bet_type": None,
                "confidence": 0.0,
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
        return {
            "model": name,
            "ok": True,
            "error": None,
            "prediction": pred,
            "bet_type": bt,
            "line": line_f,
            "confidence": conf,
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
            return self._fallback_result("无有效模型投票")

        # 共识键：bet_type + prediction
        key_counts = Counter((v["bet_type"], v["prediction"]) for v in ok_votes)
        (winning_bt, winning_pred), win_count = key_counts.most_common(1)[0]
        consensus_ratio = win_count / len(ok_votes)
        min_ratio = settings.ENSEMBLE_MIN_CONSENSUS
        min_votes = min(max(1, settings.ENSEMBLE_MIN_VOTES), len(ok_votes))
        consensus_reached = (
            win_count >= min_votes
            and consensus_ratio + 1e-9 >= min(min_ratio, 1.0 if len(ok_votes) == 1 else min_ratio)
        )
        # 单模型可用时：置信度够高才放行（提高胜率）
        if len(ok_votes) == 1 and ok_votes[0]["confidence"] >= settings.AI_SINGLE_MODEL_MIN_CONFIDENCE:
            consensus_reached = True
            consensus_ratio = 1.0

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
                    "error": v.get("error"),
                }
                for v in votes
            ],
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
        }

        od = _odds_for_pick(market_odds, winning_bt, winning_pred)
        if od > 1:
            # 去 vig 计算 EV：仅用同市场的赔率去 vig（避免跨市场稀释）
            market_only_odds = self._extract_market_odds(market_odds, winning_bt)
            if market_only_odds and len(market_only_odds) >= 2:
                analysis["expected_value"] = _devig_ev(analysis["confidence"], od, market_only_odds)
            else:
                analysis["expected_value"] = round((analysis["confidence"] * od) - 1, 4)
            analysis["kelly_fraction"] = self._calc_kelly_fraction_raw(
                od, analysis["confidence"]
            )
            analysis["odds"] = od
        else:
            analysis["expected_value"] = 0.0
            analysis["kelly_fraction"] = 0.0

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
            # 更新参考字段（EV/Kelly 仅展示，不参与决策）
            try:
                od = float(analysis.get("odds") or 0)
                if od > 1:
                    analysis["expected_value"] = round((cap * od) - 1, 4)
                    analysis["kelly_fraction"] = self._calc_kelly_fraction_raw(od, cap)
            except Exception:
                pass
        else:
            analysis["quality_cap"] = cap
        return analysis

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
        is_football = sport in ("football", "soccer")

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
        # 8 大维度数据收集
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
            dim_data["球员伤病"] = historical_data.get("player_status") or historical_data.get("news_injuries") or None
            dim_data["球员级别数据"] = historical_data.get("player_stats") or None
            dim_data["战意/轮换"] = historical_data.get("motivation") or None
            dim_data["联赛积分排名"] = historical_data.get("standings") or None

        # 盘口维度
        has_markets = isinstance(market_odds, dict) and bool(market_odds.get("markets") or any(
            k in (market_odds or {}) for k in ("moneyline", "spread", "total")
        ))
        has_line_moves = isinstance(market_odds, dict) and bool(market_odds.get("line_movements"))
        dim_data["亚洲盘"] = has_markets
        dim_data["盘口变化"] = has_line_moves

        # 构建维度分析框架（始终列出全部 8 维度，标注有无数据）
        dim_lines = []
        dim_available = 0
        dim_names = [
            "历史交锋", "球队近期状态", "球员伤病", "球员级别数据",
            "战意/轮换", "联赛积分排名", "亚洲盘", "盘口变化",
        ]
        for dn in dim_names:
            dv = dim_data.get(dn)
            if dv:
                dim_available += 1
                dim_lines.append(f"  [{dn}] 有数据")
            else:
                dim_lines.append(f"  [{dn}] 数据缺失")
        dim_summary = f"（{dim_available}/8 维度有数据）"

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
            player_status = historical_data.get("player_status") or historical_data.get("news_injuries") or []
            player_stats = historical_data.get("player_stats") or {}
            motivation = historical_data.get("motivation") or {}
            dims_present = historical_data.get("dimensions_present") or []
            dims_missing = historical_data.get("dimensions_missing") or []
            if player_status:
                prompt += "\n## 球员伤病/伤停\n" + "\n".join(f"- {x}" for x in (player_status[:8] if isinstance(player_status, list) else [str(player_status)]) ) + "\n"
            if isinstance(player_stats, dict) and (player_stats.get("home") or player_stats.get("away")):
                prompt += f"\n## 球员级别数据\n{json.dumps(player_stats, ensure_ascii=False, separators=(',', ':'))}\n"
            if isinstance(motivation, dict) and (
                motivation.get("home") or motivation.get("away") or motivation.get("notes")
            ):
                prompt += f"\n## 战意/轮换\n{json.dumps(motivation, ensure_ascii=False, separators=(',', ':'))}\n"
            if standings.get("home") or standings.get("away"):
                prompt += f"\n## 联赛积分排名\n{json.dumps(standings, ensure_ascii=False, separators=(',', ':'))}\n"

            # 泊松统计模型：用球员场均进失球计算预期比分概率
            if is_football and isinstance(player_stats, dict):
                poisson_block = self._calc_poisson_expected(player_stats)
                if poisson_block:
                    prompt += f"\n## 泊松统计模型（量化基线，非主观判断）\n{json.dumps(poisson_block, ensure_ascii=False, separators=(',', ':'))}\n"

            if quality or dims_present or dims_missing:
                prompt += (
                    f"\n## 数据维度覆盖\nsource={quality.get('source') if isinstance(quality, dict) else ''} "
                    f"completeness={(quality or {}).get('completeness') if isinstance(quality, dict) else ''} "
                    f"present={dims_present or (quality or {}).get('fields_present')} "
                    f"missing={dims_missing}\n"
                )

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

        if is_football:
            prompt += f"\n## 投注市场（亚洲盘：独赢/亚洲让球/亚洲大小，只选 1 个最佳）{score_hint}\n"
            if markets_block:
                prompt += f"- 可用盘口（含 opening / 盘口变化 line_movement）: {json.dumps(markets_block, ensure_ascii=False)}\n"
            else:
                flat = _flatten_market_odds(market_odds)
                if flat:
                    prompt += f"- 当前赔率: {json.dumps(flat, ensure_ascii=False)}\n"
            line_moves = None
            if isinstance(market_odds, dict):
                line_moves = market_odds.get("line_movements")
            if line_moves:
                prompt += f"- 盘口变化摘要: {json.dumps(line_moves, ensure_ascii=False)}\n"
            prompt += (
                "请在可用盘口中只选择 **一个** 置信最高的方向：\n"
                "- moneyline(独赢): prediction=home/draw/away\n"
                "- spread(亚洲让球): prediction=home/away（主队让球线视角；结合 opening 与升/降水）\n"
                "- total(亚洲大小): prediction=over/under（结合 opening 总分与升/降水）\n"
            )
            prompt += f"""
## 分析框架 {dim_summary}
{chr(10).join(dim_lines)}

## 量化分析指引（必须严格遵循）

### 1. 统计信号优先级
- **泊松模型**给出预期比分和大小球概率，是量化基线。若你的判断与泊松概率偏差 >15%，必须在 reasoning 中解释原因。
- **xG diff（积分榜期望进球差）**：edge>0.5 表示明显实力差距，可提高 confidence；edge<0.2 表示实力接近，应降低 confidence。
- **交锋胜率基线**：若交锋胜率与泊松概率方向一致，confidence 可上浮；方向矛盾时 confidence 必须下调 0.1。
- **攻防效率比**：效率比>1.5 表示进攻强于防守，倾向大球；<1.0 表示防守强于进攻，倾向小球。
- **水位变化信号**：direction="下降(有利-买方)"时可适度提高 confidence；"上升(不利-买方)"时必须降低 confidence 且 risk_level 标为 high。
- **比赛阶段权重**：足球 60-75 分钟是进球高发期，此阶段大球概率应上浮；75 分钟+是大小球突变期，谨慎判断。

### 2. 信号一致性校准
- 多数信号（泊松/xG/交锋/效率比）方向一致 -> confidence 可达 0.65-0.75
- 信号方向分歧 -> confidence 必须 ≤ 0.55
- 水位变化与基本面矛盾 -> confidence 必须 ≤ 0.50，risk_level=high

### 3. 数据质量约束
- 维度<4/8时confidence<{settings.LLM_NO_DATA_CONFIDENCE_CAP}。
- 不得编造缺失数据；缺失维度标注"数据缺失"。
- 泊松模型和统计信号是预计算值，不得质疑或修改，只能在此基础上做调整。

## 输出（严格JSON）
{{"bet_type":"moneyline/spread/total","prediction":"home/draw/away或home/away或over/under","line":null,"confidence":0.0-1.0,"reasoning":"1.泊松基线:预期比分X-Y,大小球概率...2.统计信号:xG diff=...,交锋胜率=...,效率比=...,水位变化=...3.历史交锋:...4.近期状态:...5.伤病:...6.球员数据:...7.战意:...8.排名:...9.亚洲盘:...10.盘口变化:...综合:信号一致性分析+最终判断","key_factors":["因素1","因素2"],"risk_level":"low/medium/high","value_bets":[]}}
"""
        else:
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
- **攻防效率比**：效率比>1.5 倾向大球；<1.0 倾向小球。edge>0.5 表示双方节奏差异大。
- **交锋胜率基线**：若交锋历史多高分场次，倾向大球；多低分场次，倾向小球。
- **xG diff（积分榜期望分差）**：edge 绝对值大表示实力悬殊，弱队可能刷分倾向大球。
- **水位变化信号**：direction="下降(有利-买方)"时大球赔率下降=市场看好大球，可提高 confidence；"上升"时反之。
- **比赛阶段权重**：篮球 Q4 是得分爆发期，此阶段大球概率应上浮；Q1 节奏偏慢，倾向小球。

### 2. 信号一致性校准
- 多数信号方向一致 -> confidence 可达 0.60-0.70
- 信号方向分歧 -> confidence 必须 ≤ 0.50
- 水位变化与基本面矛盾 -> confidence 必须 ≤ 0.45，risk_level=high

### 3. 数据质量约束
- 数据维度 < 4/8 时 confidence 必须 < {settings.LLM_NO_DATA_CONFIDENCE_CAP}。
- 不得编造缺失数据；缺失维度标注"数据缺失"。
- 统计信号是预计算值，不得质疑或修改，只能在此基础上做调整。
- 须结合盘口升/降水；变盘与基本面冲突时降低 confidence。

## 输出格式（严格JSON）
{{
    "bet_type": "total",
    "prediction": "over 或 under",
    "line": null,
    "confidence": 0.0-1.0,
    "reasoning": "1.统计信号:效率比=...,交锋胜率=...,xG diff=...,水位变化=...,比赛阶段=...2.历史交锋:...3.近期状态:...4.伤病:...5.球员数据:...6.战意:...7.排名:...8.亚洲盘:...9.盘口变化:...综合:信号一致性分析+最终判断",
    "key_factors": ["因素1", "因素2", "因素3"],
    "risk_level": "low/medium/high",
    "value_bets": [
        {{"selection": "over/under", "bet_type": "total", "reason": "为什么有价值"}}
    ]
}}

注意：prediction 只能是 over 或 under；reasoning 必须先分析统计信号再分析各维度；只输出JSON。
"""

        if news:
            prompt += "\n## 相关新闻/伤病\n"
            for n in news[:8]:
                prompt += f"- {n}\n"

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
                    f"confidence 必须低于 {settings.LLM_NO_DATA_CONFIDENCE_CAP}。\n"
                )
        except Exception:
            if ctx_source == "none":
                prompt += (
                    f"\n> 注意：无真实交锋/近况/伤病数据，请仅基于盘口赔率分析，"
                    f"confidence 必须低于 {settings.LLM_NO_DATA_CONFIDENCE_CAP}。\n"
                )
        return prompt

    async def _call_model(
        self,
        prompt: str,
        model_key: str,
        model_name: str,
        client: AsyncOpenAI,
    ) -> str:
        messages = [
            {"role": "system", "content": "你是专业体育赛事分析师。只输出JSON。"},
            {"role": "user", "content": prompt},
        ]

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
        return content

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
            "expected_value": 0.0,
            "kelly_fraction": 0.0,
            "consensus_reached": False,
            "consensus_votes": {},
            "consensus_ratio": 0.0,
            "ev_passed": True,
            "ensemble": [],
            "models_used": [],
            "models_failed": [],
        }
        if error:
            result["error"] = error
        return result

    @staticmethod
    def _extract_market_odds(market_odds: Optional[dict], bet_type: str) -> dict[str, float]:
        """提取指定市场的赔率（仅同类选择，避免跨市场去 vig 稀释）。

        total -> {over, under}
        moneyline -> {home, draw, away}
        spread -> {home, away}
        """
        if not market_odds:
            return {}
        # 合法选择按市场类型
        valid_sel = {
            "total": {"over", "under"},
            "moneyline": {"home", "draw", "away"},
            "spread": {"home", "away"},
        }.get(bet_type, set())

        markets = market_odds.get("markets") if isinstance(market_odds, dict) else None
        if isinstance(markets, dict) and bet_type in markets:
            entry = markets[bet_type] or {}
            if isinstance(entry, dict):
                # 格式1: odds 在子键 "odds" 中
                if "odds" in entry and isinstance(entry["odds"], dict):
                    result = {}
                    for k, v in entry["odds"].items():
                        if k in valid_sel:
                            try:
                                fv = float(v) if v else 0
                                if fv > 1.0:
                                    result[k] = fv
                            except (TypeError, ValueError):
                                pass
                    if result:
                        return result
                # 格式2: odds 直接在 entry 上（如 {line: 2.5, over: 1.85, under: 1.95}）
                result = {}
                for k, v in entry.items():
                    if k in valid_sel:
                        try:
                            fv = float(v)
                            if fv > 1.0:
                                result[k] = fv
                        except (TypeError, ValueError):
                            pass
                if result:
                    return result
        # 回退：从 flat 中按 bet_type 筛选
        flat = _flatten_market_odds(market_odds)
        if valid_sel:
            return {k: v for k, v in flat.items() if k in valid_sel}
        return flat

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

        # --- 3. 球员攻防效率比 ---
        if isinstance(historical_data, dict):
            ps = historical_data.get("player_stats") or {}
            if isinstance(ps, dict):
                h_list = ps.get("home") or []
                a_list = ps.get("away") or []
                h = h_list[0] if isinstance(h_list, list) and h_list else (h_list if isinstance(h_list, dict) else {})
                a = a_list[0] if isinstance(a_list, list) and a_list else (a_list if isinstance(a_list, dict) else {})
                if isinstance(h, dict) and isinstance(a, dict):
                    try:
                        h_att = float(h.get("avg_goals") or 0)
                        h_def = float(h.get("avg_conceded") or 0)
                        a_att = float(a.get("avg_goals") or 0)
                        a_def = float(a.get("avg_conceded") or 0)
                        if h_att > 0 and a_att > 0:
                            h_eff = round(h_att / max(h_def, 0.1), 2)
                            a_eff = round(a_att / max(a_def, 0.1), 2)
                            signals["efficiency_ratio"] = {
                                "home_attack_defense": h_eff,
                                "away_attack_defense": a_eff,
                                "edge": round(h_eff - a_eff, 2),
                            }
                    except (TypeError, ValueError):
                        pass

        # --- 4. 盘口水位变化信号 ---
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

        # --- 5. 比赛阶段权重 ---
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

    @staticmethod
    def _calc_kelly_fraction_raw(pred_odds: float, confidence: float) -> float:
        if pred_odds <= 1:
            return 0.0
        p = confidence
        q = 1 - p
        b = pred_odds - 1
        kelly = (p * b - q) / b if b > 0 else 0
        return round(max(0.0, min(kelly, settings.AI_KELLY_FRACTION_CAP)), 4)

    @staticmethod
    def _calc_poisson_expected(player_stats: dict) -> Optional[dict]:
        """用球员场均进失球计算泊松分布预期比分概率。

        输入: {"home": [{"avg_goals": 2.0, "avg_conceded": 1.0, ...}], "away": [...]}
        输出: {"expected_home_goals": 1.5, "expected_away_goals": 1.0,
               "over_2_5_prob": 0.54, "under_2_5_prob": 0.46,
               "home_win_prob": 0.42, "draw_prob": 0.28, "away_win_prob": 0.30}
        """
        import math

        home_list = player_stats.get("home") or []
        away_list = player_stats.get("away") or []
        if not home_list or not away_list:
            return None
        h = home_list[0] if isinstance(home_list, list) else home_list
        a = away_list[0] if isinstance(away_list, list) else away_list
        if not isinstance(h, dict) or not isinstance(a, dict):
            return None

        try:
            h_attack = float(h.get("avg_goals") or h.get("goals_per_game") or 0)
            h_defense = float(h.get("avg_conceded") or h.get("conceded_per_game") or 0)
            a_attack = float(a.get("avg_goals") or a.get("goals_per_game") or 0)
            a_defense = float(a.get("avg_conceded") or a.get("conceded_per_game") or 0)
        except (TypeError, ValueError):
            return None

        if h_attack <= 0 or a_attack <= 0:
            return None

        # 预期进球：主队攻击力 × 客队防守力（取几何平均归一化）
        exp_home = (h_attack * a_defense) / 2.0
        exp_away = (a_attack * h_defense) / 2.0
        # 限制在合理范围
        exp_home = max(0.1, min(exp_home, 5.0))
        exp_away = max(0.1, min(exp_away, 5.0))

        # 泊松分布计算各比分概率
        max_goals = 7
        home_probs = [math.exp(-exp_home) * (exp_home ** i) / math.factorial(i) for i in range(max_goals + 1)]
        away_probs = [math.exp(-exp_away) * (exp_away ** i) / math.factorial(i) for i in range(max_goals + 1)]

        home_win = draw = away_win = over_25 = under_25 = 0.0
        for hg in range(max_goals + 1):
            for ag in range(max_goals + 1):
                p = home_probs[hg] * away_probs[ag]
                if hg > ag:
                    home_win += p
                elif hg == ag:
                    draw += p
                else:
                    away_win += p
                if hg + ag > 2:
                    over_25 += p
                else:
                    under_25 += p

        # 归一化（修正截断误差）
        total_wdl = home_win + draw + away_win
        if total_wdl > 0:
            home_win /= total_wdl
            draw /= total_wdl
            away_win /= total_wdl
        total_ou = over_25 + under_25
        if total_ou > 0:
            over_25 /= total_ou
            under_25 /= total_ou

        return {
            "expected_home_goals": round(exp_home, 2),
            "expected_away_goals": round(exp_away, 2),
            "expected_total_goals": round(exp_home + exp_away, 2),
            "over_2_5_prob": round(over_25, 3),
            "under_2_5_prob": round(under_25, 3),
            "home_win_prob": round(home_win, 3),
            "draw_prob": round(draw, 3),
            "away_win_prob": round(away_win, 3),
        }


analyzer = MatchAnalyzer()
