"""Fail-closed safety tests for Pinnacle's dedicated total-market UI path."""

import asyncio
from pathlib import Path
from decimal import Decimal

from app.services.bookmakers.plugins.pinnacle.bet_ui import (
    _has_exact_line,
    _validate_pinnacle_slip_text,
    ui_place_pinnacle_total,
)
from app.services.bookmakers.plugins.pinnacle.modals import (
    cleanup_pinnacle_failed_slips,
    dismiss_pinnacle_blocking_modals,
)
from app.services.bookmakers.plugins.pinnacle.odds import _odds_from_period0
from app.services.bookmakers.venue_live import _parse_total_from_raw
from app.services.bookmakers.odds_write import apply_odds_version


def _validate(text: str, *, selection: str = "under", line: float = 2.5, count=1):
    return _validate_pinnacle_slip_text(
        text,
        home="维尔纽斯投资",
        away="让球盘比赛",
        selection=selection,
        line=line,
        bet_count=count,
    )


def test_line_match_is_numeric_token_not_odds_substring():
    assert _has_exact_line("全场大小 2.5 小 1.91", 2.5)
    assert not _has_exact_line("让球盘 0.5 主 2.500 客 1.55", 2.5)
    assert not _has_exact_line("全场大小 4.5-5 小 1.91", 4.5)


def test_compact_dom_total_omitted_over_label_uses_line_adjacent_to_under():
    raw = "奥斯达 松兹瓦尔 1.662 4.630 4.280 0.5-1 1.840 2.020 3.0 1.813 小 2.030"
    assert _parse_total_from_raw(raw, sport="football") == {
        "line": 3.0,
        "over": 1.813,
        "under": 2.030,
    }


def test_compact_dom_prefers_full_game_line_over_half_line():
    raw = "巴伦西亚 皇家贝蒂斯 2-2 2-2.5 1.892 小 2.000 1.0 2.070 小 1.781"
    parsed = _parse_total_from_raw(raw, sport="football")
    assert parsed and parsed["line"] == 2.25


def test_quarter_line_alias_is_accepted():
    assert _has_exact_line("全场大小 3.5-4 小 1.91", 3.75)


def test_integer_basketball_line_accepts_decimal_display_alias():
    assert _has_exact_line("全场大小 157.0 小 1.91", 157)
    assert _has_exact_line("全场大小 157 小 1.91", 157.0)


def test_exact_full_game_under_slip_passes():
    ok, reason = _validate_pinnacle_slip_text(
        "维尔纽斯投资 - 比赛对手\n全场大小盘 2.5\n小 1.91\n投下 1 注",
        home="维尔纽斯投资",
        away="比赛对手",
        selection="under",
        line=2.5,
        bet_count=1,
    )
    assert (ok, reason) == (True, "ok")


def test_market_word_inside_team_name_does_not_cause_false_rejection():
    ok, reason = _validate(
        "维尔纽斯投资 - 让球盘比赛\n全场大小盘 2.5\n小 1.91\n投下 1 注"
    )
    assert (ok, reason) == (True, "ok")


def test_exact_full_game_over_slip_passes():
    ok, reason = _validate_pinnacle_slip_text(
        "维尔纽斯投资 - 比赛对手\n总进球 2.5\n大 1.88\n投下 1 注",
        home="维尔纽斯投资",
        away="比赛对手",
        selection="over",
        line=2.5,
        bet_count=1,
    )
    assert (ok, reason) == (True, "ok")


def test_handicap_slip_is_blocked_even_if_total_words_are_also_present():
    ok, reason = _validate(
        "维尔纽斯投资 - 让球盘比赛\n让球盘 0.5\n小 1.91\n大小盘 2.5\n投下 1 注"
    )
    assert not ok
    assert reason == "slip_market_mismatch:handicap"


def test_wrong_direction_is_blocked():
    ok, reason = _validate("维尔纽斯投资\n全场大小盘 2.5\n大 1.91\n投下 1 注")
    assert not ok
    assert reason == "slip_direction_mismatch"


def test_wrong_line_is_blocked():
    ok, reason = _validate("维尔纽斯投资\n全场大小盘 3.5\n小 1.91\n投下 1 注")
    assert not ok
    assert reason == "slip_line_mismatch"


def test_wrong_team_is_blocked():
    ok, reason = _validate("其它球队 - 另一球队\n全场大小盘 2.5\n小 1.91\n投下 1 注")
    assert not ok
    assert reason == "slip_team_mismatch"


def test_provider_native_team_alias_validates_translated_slip():
    ok, reason = _validate_pinnacle_slip_text(
        "FC Pando - Ingenieros Chaco\n全场大小盘 4.5\n小 1.83\n投下 1 注",
        home="潘多",
        away="查科工程师",
        selection="under",
        line=4.5,
        bet_count=1,
        team_aliases=("FC Pando", "Ingenieros Chaco"),
    )
    assert (ok, reason) == (True, "ok")


def test_compact_total_keeps_mid_and_provider_native_teams():
    rows = _odds_from_period0(
        [[], [[2.5, 2.5, 1.91, 1.95, "selection-1", 0]], []],
        mid="123456789",
        sport_id=29,
        event_home="FC Pando",
        event_away="Ingenieros Chaco",
        event_league="Bolivia",
    )
    total = next(row for row in rows if row.bet_type == "total")
    assert total.odds_data["_site"] == {
        "bet_type": "total",
        "line": 2.5,
        "mid": "123456789",
        "event_home": "FC Pando",
        "event_away": "Ingenieros Chaco",
        "event_league": "Bolivia",
        "sport_id": "29",
        "site_code": "pinnacle",
        "selections": {
            "over": {"id": "selection-1", "oid": "selection-1", "price": 1.91, "name": "over"},
            "under": {"id": "selection-1", "oid": "selection-1", "price": 1.95, "name": "under"},
        },
    }


def test_unchanged_price_refreshes_internal_ui_locator_metadata():
    class Current:
        odds_data = {"under": 1.95, "_site": {"mid": "old"}}
        spread = 0
        total = 2.5
        valid_to = None

    class Db:
        def add(self, _row):
            raise AssertionError("unchanged public odds must not create a version")

    current = Current()
    row, wrote = apply_odds_version(
        Db(),
        current=current,
        match_id=1,
        bet_type="total",
        odds_data={"under": 1.95, "_site": {"mid": "new", "event_home": "Home"}},
        spread=0,
        total=2.5,
        provider="平博",
        is_live=True,
        now=__import__("datetime").datetime.now(),
        odds_cls=object,
    )
    assert wrote is False
    assert row is current
    assert current.odds_data["_site"]["mid"] == "new"


def test_half_market_is_blocked():
    ok, reason = _validate("维尔纽斯投资\n上半场大小盘 2.5\n小 1.91\n投下 1 注")
    assert not ok
    assert reason == "slip_period_mismatch"


def test_team_total_market_is_blocked():
    ok, reason = _validate("维尔纽斯投资\n主队总进球 2.5\n小 1.91\n投下 1 注")
    assert not ok
    assert reason == "slip_market_mismatch:non_match_total"


def test_multiple_selections_are_blocked():
    ok, reason = _validate("维尔纽斯投资\n全场大小盘 2.5\n小 1.91\n投下 2 注", count=2)
    assert not ok
    assert reason == "slip_multiple_bets:2"


def test_confirmation_code_has_no_generic_button_fallbacks():
    source = Path(
        "app/services/bookmakers/plugins/pinnacle/bet_ui.py"
    ).read_text(encoding="utf-8")
    assert 'button:has-text("投注")' not in source
    assert '[role="button"]:has-text("下注")' not in source
    assert 'button[type="submit"]' not in source
    assert 't === \'确认投注\'' not in source
    assert "stale confirmation cancelled" in source
    assert "cancel_stale_confirm_js" in source
    step1 = source.split('step1_js = """', 1)[1].split('step2_js = """', 1)[0]
    assert r"^投下\\s*1\\s*注$" in step1
    assert r"^投下\\s*\\d+\\s*注$" not in step1
    assert "t === 'Place Bet'" not in step1
    assert "placeControl" in step1
    assert "aria-disabled" in step1


def test_event_mid_and_native_alias_recovery_precede_team_search():
    source = Path(
        "app/services/bookmakers/plugins/pinnacle/bet_ui.py"
    ).read_text(encoding="utf-8")
    assert "eventId:" in source
    assert "event aliases resolved" in source
    direct_click = source.index("result, last_miss = await _try_click_all_frames()")
    filtered_search = source.index("if not await _search_team(query)", direct_click)
    assert direct_click < filtered_search


def test_unverified_stake_never_reaches_place_button():
    source = Path(
        "app/services/bookmakers/plugins/pinnacle/bet_ui.py"
    ).read_text(encoding="utf-8")
    unverified = source.index('return False, f"stake_write_unverified|{fill_detail}"')
    place_button = source.index('step1_js = """', unverified)
    assert unverified < place_button


def test_modal_handler_protects_bet_and_clear_confirmations():
    source = Path(
        "app/services/bookmakers/plugins/pinnacle/modals.py"
    ).read_text(encoding="utf-8")
    assert "确认投注|您是否想要投注|清空注单|清除注单" in source
    assert "PINNACLE_CLICK_CLEAR_ALL" in source
    assert "^(清除全部|移除全部|Remove All)$" in source
    assert "label === '好的' || label === 'OK' || label === '确定'" in source
    assert "&& /好的|OK|确定/.test(body) && /取消|Cancel/i.test(body)" in source
    assert "input, span, div" in source
    assert "PINNACLE_VERIFY_SLIP_EMPTY" in source
    assert "双重验证|二次验证" in source
    assert "Not now|Maybe later|No thanks" in source
    assert "securityPrompt.test(body)" in source


class _FakePinnaclePage:
    frames = []
    main_frame = None

    def __init__(self, *, blockers: list[str] | None = None):
        self.blockers = list(blockers or [])
        self.clear_clicks = 0
        self.clear_confirms = 0

    async def evaluate(self, script, *_args):
        if "PINNACLE_CANCEL_BET_CONFIRM" in script:
            return ""
        if "PINNACLE_DISMISS_BLOCKER" in script:
            if self.blockers:
                return {"clicked": self.blockers.pop(0), "prompt": "提示"}
            return {"clicked": "", "prompt": ""}
        if "PINNACLE_CLICK_CLEAR_ALL" in script:
            self.clear_clicks += 1
            return "清除全部"
        if "PINNACLE_CONFIRM_CLEAR" in script:
            self.clear_confirms += 1
            return "好的"
        if "PINNACLE_VERIFY_SLIP_EMPTY" in script:
            return {
                "empty": self.clear_clicks > 0 and self.clear_confirms > 0,
                "hasBet": False,
                "clearDialog": False,
                "clearButton": False,
            }
        raise AssertionError("unexpected script")

    async def wait_for_timeout(self, _milliseconds):
        return None


def test_failed_slip_cleanup_runs_clear_then_good_and_verifies_empty():
    page = _FakePinnaclePage()
    ok, detail = asyncio.run(cleanup_pinnacle_failed_slips(page))
    assert ok is True
    assert page.clear_clicks == 1
    assert page.clear_confirms == 1
    assert "confirmed:好的" in detail
    assert "empty_verified" in detail


def test_blocker_handler_repeats_until_all_known_modals_are_closed():
    page = _FakePinnaclePage(blockers=["好的", "关闭", "知道了"])
    actions = asyncio.run(dismiss_pinnacle_blocking_modals(page))
    assert [item.split(":", 1)[0] for item in actions] == ["好的", "关闭", "知道了"]


def test_real_2fa_prompt_clicks_defer_never_enable():
    playwright = __import__("playwright.async_api", fromlist=["async_playwright"])

    async def run():
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(
                """<!doctype html><html><body>
                <div role="dialog" id="two-factor" style="position:fixed;width:700px;height:420px">
                  <h2>为您的账户添加额外保护（双重验证）</h2>
                  <p>您是否现在想为登录启用双重验证（2FA）？</p>
                  <button id="enable" onclick="window.enableClicked=true">登录时启用双重验证</button>
                  <button id="defer" onclick="window.deferClicked=true;this.parentElement.remove()">暂不</button>
                </div>
                </body></html>"""
            )
            actions = await dismiss_pinnacle_blocking_modals(page)
            state = await page.evaluate(
                "() => ({enable: !!window.enableClicked, defer: !!window.deferClicked, modal: !!document.querySelector('#two-factor')})"
            )
            await browser.close()
            return actions, state

    actions, state = asyncio.run(run())
    assert actions and actions[0].startswith("暂不:")
    assert state == {"enable": False, "defer": True, "modal": False}


def test_no_debit_path_cleans_failed_slip_before_returning_failure():
    source = Path("app/services/bookmakers/site_bet.py").read_text(encoding="utf-8")
    no_debit_pos = source.index('"%s UI bet no debit')
    cleanup_pos = source.index(
        "await cleanup_pinnacle_failed_slips(page)", no_debit_pos
    )
    failure_return_pos = source.index("return PlaceBetResult(", cleanup_pos)
    assert no_debit_pos < cleanup_pos < failure_return_pos


def test_every_non_preview_ui_failure_gets_global_cleanup():
    source = Path("app/services/bookmakers/site_bet.py").read_text(encoding="utf-8")
    assert 'not str(ui_detail or "").startswith("preview_ready|")' in source
    assert "cleanup_pinnacle_failed_slips(page)" in source
    assert "pinnacle_session_expired(page)" in source
    assert "平博体育页当前为访客状态" in source


def test_odds_and_dom_scrapers_dismiss_pinnacle_blockers():
    for filename in ("odds.py", "live_text.py"):
        source = Path(
            f"app/services/bookmakers/plugins/pinnacle/{filename}"
        ).read_text(encoding="utf-8")
        assert "await dismiss_pinnacle_blocking_modals(page)" in source


def test_gate_keep_alive_continuously_dismisses_pinnacle_blockers():
    source = Path("scripts/browser_gate.py").read_text(encoding="utf-8")
    keepalive = source.split("async def _keep_sessions_refresh_loop", 1)[1]
    assert "await dismiss_pinnacle_blocking_modals(sess.page)" in keepalive
    assert "await ensure_pinnacle_page_ready(" in keepalive


def test_pinnacle_bet_fails_closed_when_page_recovery_fails():
    source = Path(
        "app/services/bookmakers/plugins/pinnacle/bet_ui.py"
    ).read_text(encoding="utf-8")
    ready = source.index("if not await ensure_pinnacle_page_ready(")
    fail = source.index('return False, "pinnacle_page_recovery_failed"', ready)
    first_click = source.index("result, last_miss = await _try_click_all_frames()", fail)
    assert ready < fail < first_click


def test_preview_route_bypasses_plugin_bet_implementation():
    source = Path("scripts/browser_gate.py").read_text(encoding="utf-8")
    guard_pos = source.index("if not preview_only:")
    plugin_pos = source.index("result = await plug.place_bet(", guard_pos)
    shared_ui_pos = source.index("result = await place_site_bet(", plugin_pos)
    assert guard_pos < plugin_pos < shared_ui_pos
    assert "return await _bet_place(req, preview_only=True)" in source
    assert '@app.post("/bet/cleanup")' in source
    assert "cleanup_pinnacle_failed_slips(page)" in source
    assert '@app.post("/bet/pinnacle-total-candidates")' in source


def test_flat_league_grid_resolves_target_total_by_text_sequence_without_submit():
    """The real compact board has no per-event wrapper around teams and markets."""
    playwright = __import__("playwright.async_api", fromlist=["async_playwright"])

    async def run():
        html = """<!doctype html><html><head><style>
          .league-grid > span, .league-grid > button { display:block; }
        </style></head><body>
          <div class="league-grid">
            <span>奥斯达</span><span>松兹瓦尔</span><span>和局</span>
            <button><span><i></i>1.662</span></button>
            <button><span><i></i>4.630</span></button>
            <button><span><i></i>4.280</span></button>
            <span><i></i>0.5-1</span>
            <button><span><i></i>1.840</span></button>
            <button><span><i></i>2.020</span></button>
            <span><i></i>3.0</span>
            <button id="target-over" onclick="openSlip('over')"><span><i></i>1.813</span></button>
            <span><i></i>小</span>
            <button onclick="openSlip('under')"><span><i></i>2.030</span></button>

            <span>另一主队</span><span>另一客队</span><span>和局</span>
            <button><span><i></i>1.700</span></button>
            <button><span><i></i>3.900</span></button>
            <button><span><i></i>4.100</span></button>
            <span><i></i>0.5</span>
            <button><span><i></i>1.900</span></button>
            <button><span><i></i>1.900</span></button>
            <span><i></i>3.0</span>
            <button onclick="openSlip('wrong-over')"><span><i></i>1.813</span></button>
            <span><i></i>小</span>
            <button><span><i></i>2.030</span></button>
          </div>
          <aside id="slip" class="betslip"></aside>
          <script>
            window.clickedSide = '';
            window.submitClicks = 0;
            function openSlip(side) {
              window.clickedSide = side;
              if (side !== 'over') return;
              document.getElementById('slip').innerHTML = `
                <div>奥斯达 - 松兹瓦尔</div>
                <div>全场大小盘 3.0</div><div>大 @ 1.813</div>
                <label>投注金额 <input type="number" placeholder="投注金额"></label>
                <button onclick="window.submitClicks++">投下 1 注</button>`;
            }
          </script>
        </body></html>"""
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))
            await page.goto("https://mock-pinnacle.test/zh-cn/compact/sports/soccer/live")
            clicked, detail, actual_stake, _ = await ui_place_pinnacle_total(
                page,
                home="奥斯达",
                away="松兹瓦尔",
                selection="over",
                odds=1.813,
                stake=Decimal("1"),
                line=3.0,
                sport="football",
                live_only=True,
                preview_only=True,
            )
            state = await page.evaluate(
                "() => ({side: window.clickedSide, submits: window.submitClicks, stake: document.querySelector('#slip input')?.value || ''})"
            )
            await browser.close()
        assert clicked is False
        assert detail.startswith("preview_ready|")
        assert actual_stake == Decimal("1")
        assert state == {"side": "over", "submits": 0, "stake": "1"}

    asyncio.run(run())
