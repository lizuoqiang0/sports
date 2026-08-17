"""
平博真实连接器：与 OB 同一套 Browser Gate 登录 + 长连接 + 盘口 + 下单逻辑。
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

import httpx

from app.services.bookmakers.base import (
    BookmakerConnector,
    PlaceBetResult,
    RemoteMatch,
    RemoteOdds,
    VerifyResult,
)
from app.services.bookmakers.browser_login import interactive_site_login
from app.services.bookmakers.gate_client import (
    fetch_health,
    gate_url,
    post_balance,
    post_login,
    post_odds_sync,
    post_place_bet,
    prefer_login,
    to_decimal,
)
from app.services.bookmakers.site_odds import fetch_site_odds_via_page
from app.services.bookmakers.site_profiles import get_site_profile
from app.services.bookmakers.plugins.ob.kaiyun import is_demo_url

logger = logging.getLogger(__name__)

CAPTCHA_HINT = (
    "请先运行: bash scripts/ensure_browser_gate.sh start ，"
    "再点验证连接；或在弹出浏览器中完成登录/人机验证。"
)

_to_decimal = to_decimal
_gate_url = gate_url


class BrowserSiteConnector(BookmakerConnector):
    """通用真站连接器（pinnacle；OB 也可走此路径）。"""

    def __init__(self, code: str, base_url: str, username: str, password: str, **kwargs: Any):
        super().__init__(base_url, username, password, **kwargs)
        self.code = (code or "").lower()
        profile_meta = get_site_profile(self.code)
        self.name = profile_meta.get("name") or self.code
        self.session_token = str(kwargs.get("session_token") or "")
        self._profile: dict = dict(kwargs.get("profile") or {})
        self._balance = _to_decimal(kwargs.get("balance"), Decimal("0"))

    async def _probe_site(self, timeout: float = 3.0) -> bool:
        try:
            async with httpx.AsyncClient(verify=False, timeout=timeout, follow_redirects=True) as client:
                r = await client.get(f"{self.base_url}/")
                return r.status_code < 500
        except Exception as e:
            logger.warning("site probe failed %s: %s", self.code, e)
            return False

    async def _login_via_gate(self, *, force_new: bool = False, manual_venue: bool = False) -> Optional[VerifyResult]:
        gate = gate_url()
        if not gate:
            return None
        from app.services.bookmakers.site_profiles import needs_manual_venue

        manual = bool(manual_venue or self.extra.get("manual_venue") or needs_manual_venue(self.code))
        # 平博首登常 >90s；过短会 ReadTimeout，但网关侧登录仍继续占锁
        timeout = 120.0 if manual else (150.0 if self.code == "pinnacle" else 90.0)
        await prefer_login(
            base_url=self.base_url,
            site_code=self.code,
            seconds=90 if manual else 45,
        )
        health = await fetch_health(timeout=2.0)
        if not health:
            return VerifyResult(
                ok=False,
                message="无法连接本机浏览器网关。请运行: bash scripts/ensure_browser_gate.sh start 后再点「验证连接」。",
            )

        data = await post_login(
            base_url=self.base_url,
            username=self.username,
            password=self.password,
            session_token=self.session_token,
            site_code=self.code,
            wait_seconds=90 if manual else 35,
            force_new=force_new,
            manual_venue=manual,
            timeout=timeout,
        )
        if data.get("busy"):
            return VerifyResult(ok=False, message=str(data.get("message") or "浏览器网关正忙，请稍后再试"))
        if not data.get("ok"):
            msg = str(data.get("message") or CAPTCHA_HINT)
            if "调用浏览器网关失败" in msg:
                return VerifyResult(ok=False, message=f"{msg}。{CAPTCHA_HINT}")
            return VerifyResult(ok=False, message=msg)

        token = data.get("token") or ""
        profile = data.get("profile") or {"name": self.username}
        bal = to_decimal(data.get("balance"), Decimal("0"))
        if bal <= 0 and isinstance(profile, dict):
            bal = to_decimal(profile.get("balance"), Decimal("0"))
        if token:
            self.session_token = token
        self._profile = profile if isinstance(profile, dict) else {"name": self.username}
        if bal > 0:
            self._balance = bal
            self._profile["balance"] = float(bal)
        # 手动场馆：必须真正 keep-alive，否则无法采实时盘口
        if manual and not data.get("kept_alive"):
            return VerifyResult(
                ok=False,
                message=str(
                    data.get("message")
                    or "未建立盘口长连接。请手动进入场馆后等待验证完成（勿关闭浏览器）"
                ),
            )
        msg = str(data.get("message") or "登录成功，浏览器保持可见长连接")
        if manual:
            msg = msg if "手动" in msg or "场馆" in msg else (msg + "（手动进场馆）")
        return VerifyResult(
            ok=True,
            message=msg,
            balance=self._balance,
            profile=self._profile,
            session_token=token or self.session_token,
        )

    async def verify(self) -> VerifyResult:
        if not self.base_url or is_demo_url(self.base_url):
            return VerifyResult(ok=False, message="请填写真实网站地址")

        from app.services.bookmakers.site_profiles import needs_manual_venue

        manual = bool(self.extra.get("manual_venue")) or needs_manual_venue(self.code)

        # 站点不可达但已有会话：自动模式下可软通过；手动进馆时仍要求可达
        if self.session_token and not await self._probe_site(timeout=3.0) and not manual:
            return VerifyResult(
                ok=True,
                message="已保存会话；站点暂时不可达，跳过浏览器登录（可稍后同步）",
                balance=self._balance,
                profile=self._profile or {"name": self.username},
                session_token=self.session_token,
            )

        if not self.username or not self.password:
            if self.session_token:
                gate_result = await self._login_via_gate(force_new=False, manual_venue=manual)
                if gate_result is not None:
                    return gate_result
            return VerifyResult(ok=False, message="请填写账号和密码")

        gate_result = await self._login_via_gate(force_new=False, manual_venue=manual)
        if gate_result is not None:
            return gate_result

        try:
            data = await interactive_site_login(
                base_url=self.base_url,
                username=self.username,
                password=self.password,
                session_token=self.session_token,
                wait_seconds=90 if manual else 35,
                keep_alive=True,
                force_new=False,
                site_code=self.code,
                manual_venue=manual,
            )
        except Exception as e:
            logger.exception("site login failed %s", self.code)
            return VerifyResult(ok=False, message=f"浏览器登录失败: {e}。{CAPTCHA_HINT}")

        if not data.get("ok"):
            return VerifyResult(ok=False, message=str(data.get("message") or CAPTCHA_HINT))
        token = data.get("token") or ""
        profile = data.get("profile") or {"name": self.username}
        bal = _to_decimal(data.get("balance"), Decimal("0"))
        if token:
            self.session_token = token
        self._profile = profile
        if bal > 0:
            self._balance = bal
        return VerifyResult(
            ok=True,
            message=str(data.get("message") or "登录成功"),
            balance=self._balance,
            profile=profile,
            session_token=token,
        )

    async def fetch_balance(self) -> Decimal:
        """只读体育场馆余额；禁止 /login；拒绝中心钱包。OB 侧栏 0.00 视为有效。"""
        if self.session_token and gate_url():
            data = await post_balance(
                base_url=self.base_url,
                session_token=self.session_token or "",
                site_code=self.code,
                timeout=40.0 if self.code == "ob" else 20.0,
            )
            if data:
                src = str(data.get("balance_source") or "")
                bal = to_decimal(data.get("balance"), Decimal("0"))
                # 交易密码：不覆盖已有场馆余额
                if src == "trade_password_blocked":
                    if self._balance and self._balance > 0:
                        return self._balance
                    if bal > 0:
                        self._balance = bal
                        return self._balance
                    return Decimal("0")
                # 忙且带场馆缓存：可采用（含侧栏已确认的 0.00）
                if src == "busy":
                    if data.get("ok") and data.get("cached"):
                        self._balance = bal
                        self._profile = {
                            **(self._profile or {}),
                            "balance": float(bal),
                            "balance_source": "venue_cache",
                            "venue_balance_confirmed": True,
                        }
                        return self._balance
                    if self._balance and self._balance > 0:
                        return self._balance
                    if bal > 0:
                        self._balance = bal
                        return self._balance
                    return Decimal("0")
                # 场馆现场 / 缓存 / 空钱包才采纳
                if src in ("venue", "venue_cache", "venue_empty") or data.get("in_sportsbook"):
                    if data.get("ok") or bal > 0 or src == "venue_empty":
                        self._balance = bal
                        self._profile = {
                            **(self._profile or {}),
                            "balance": float(bal),
                            "balance_source": src or "venue",
                            "venue_balance_confirmed": True,
                        }
                        return self._balance
                elif data.get("ok") and bal > 0 and src not in ("not_in_venue", "none", "error"):
                    self._balance = bal
                    self._profile = {
                        **(self._profile or {}),
                        "balance": float(bal),
                        "balance_source": src or "venue",
                        "venue_balance_confirmed": True,
                    }
                    return self._balance
        # profile 仅当标记为场馆源
        if isinstance(self._profile, dict) and self._profile.get("balance_source") == "venue":
            bal = to_decimal(self._profile.get("balance"))
            if bal > 0:
                self._balance = bal
                return self._balance
        if self._balance and self._balance > 0:
            return self._balance
        return Decimal("0")

    def _parse_gate_matches(self, matches: list) -> list[RemoteMatch]:
        out: list[RemoteMatch] = []
        for item in matches or []:
            if not isinstance(item, dict):
                continue
            odds_list = []
            for od in item.get("odds_list") or []:
                if not isinstance(od, dict):
                    continue
                odds_list.append(
                    RemoteOdds(
                        bet_type=str(od.get("bet_type") or "total"),
                        odds_data=dict(od.get("odds_data") or {}),
                        spread=float(od.get("spread") or 0),
                        total=float(od.get("total") or 0),
                    )
                )
            out.append(
                RemoteMatch(
                    external_id=str(item.get("external_id") or ""),
                    sport=str(item.get("sport") or "").strip().lower() or "",
                    league=str(item.get("league") or ""),
                    home_team=str(item.get("home_team") or ""),
                    away_team=str(item.get("away_team") or ""),
                    start_time=str(item.get("start_time") or ""),
                    status=str(item.get("status") or "upcoming"),
                    venue=str(item.get("venue") or self.name),
                    odds_list=odds_list,
                    home_score=int(item.get("home_score") or 0),
                    away_score=int(item.get("away_score") or 0),
                    clock=str(item.get("clock") or ""),
                    period=str(item.get("period") or ""),
                )
            )
        from app.services.bookmakers.match_live import remote_match_started
        from app.services.bookmakers.sport_classify import normalize_sport, reject_sport_mismatch

        cleaned = []
        for m in out:
            sport = normalize_sport(m.sport)
            if sport not in ("football", "basketball"):
                continue
            status = str(getattr(m, "status", "") or "").strip().lower()
            if status not in ("live", "inplay", "in_play", "running", "started"):
                continue
            if reject_sport_mismatch(
                sport,
                period=m.period or "",
                home_score=m.home_score,
                away_score=m.away_score,
                text=f"{m.league} {m.home_team} {m.away_team}",
            ):
                continue
            if not remote_match_started(m):
                continue
            m.sport = sport
            m.status = "live"
            cleaned.append(m)
        return cleaned

    async def _fetch_odds_via_gate(self, *, live_only: bool, limit: int) -> Optional[list[RemoteMatch]]:
        if not gate_url() or not self.session_token:
            return None
        import asyncio

        data = None
        # 滚球：Gate 已会等车道；客户端少重试、短退避，避免空等堆叠
        max_attempts = 4 if live_only else 3
        for attempt in range(max_attempts):
            data = await post_odds_sync(
                base_url=self.base_url,
                session_token=self.session_token,
                site_code=self.code,
                live_only=live_only,
                limit=limit,
                venue_url=str((self._profile or {}).get("venue_url") or ""),
                timeout=120.0 if live_only else 90.0,
            )
            if data is None:
                return None
            if data.get("busy"):
                logger.info(
                    "gate odds %s busy (attempt %s/%s): %s",
                    self.code,
                    attempt + 1,
                    max_attempts,
                    data.get("message") or "",
                )
                await asyncio.sleep(1.0 + attempt * 1.2)
                continue
            break
        if data is None:
            return None
        if data.get("busy"):
            logger.info("gate odds %s skipped: lane busy (%s)", self.code, data.get("message") or "")
            return None
        if not data.get("ok"):
            logger.warning("gate odds %s: %s", self.code, data.get("message"))
            return None
        matches = self._parse_gate_matches(data.get("matches") or [])
        if live_only and not matches:
            return []
        return matches

    async def fetch_matches_odds(self, local_matches: list[dict]) -> list[RemoteMatch]:
        """全量入口也只采滚球足球/篮球。"""
        if is_demo_url(self.base_url):
            return []
        gated = await self._fetch_odds_via_gate(live_only=True, limit=800)
        if gated is not None:
            return gated
        return []

    async def fetch_live_matches_odds(self) -> list[RemoteMatch]:
        if is_demo_url(self.base_url):
            return []
        gated = await self._fetch_odds_via_gate(live_only=True, limit=800)
        if gated is not None:
            return gated
        return []

    async def place_bet(
        self,
        *,
        match_external_id: str,
        selection: str,
        odds: float,
        stake: Decimal,
        bet_type: str = "total",
        odds_data: Optional[dict] = None,
    ) -> PlaceBetResult:
        if gate_url() and self.session_token:
            data = await post_place_bet(
                base_url=self.base_url,
                session_token=self.session_token,
                site_code=self.code,
                match_external_id=match_external_id,
                selection=selection,
                odds=float(odds),
                stake=float(stake),
                bet_type=bet_type,
                odds_data=odds_data or {},
                timeout=90.0,
            )
            if data.get("busy"):
                return PlaceBetResult(ok=False, message="浏览器网关正忙，请稍后再下单")
            bal = to_decimal(data.get("balance_after"), self._balance)
            if data.get("ok"):
                if bal > 0:
                    self._balance = bal
                return PlaceBetResult(
                    ok=True,
                    message=str(data.get("message") or "下单成功"),
                    external_bet_id=data.get("external_bet_id"),
                    balance_after=self._balance,
                )
            msg = str(data.get("message") or "下单失败")
            if "浏览器网关下单失败" in msg:
                msg = f"{msg}。请确认本机 Gate 已启动且已验证登录"
            return PlaceBetResult(ok=False, message=msg, balance_after=self._balance)

        return PlaceBetResult(
            ok=False,
            message="未配置浏览器网关，无法真实下单。请启动本机 Browser Gate 并验证登录",
            balance_after=self._balance,
        )


def create_site_connector(
    code: str,
    *,
    base_url: str,
    username: str,
    password: str,
    **kwargs: Any,
) -> BrowserSiteConnector:
    return BrowserSiteConnector(
        code=code,
        base_url=base_url,
        username=username,
        password=password,
        **kwargs,
    )
