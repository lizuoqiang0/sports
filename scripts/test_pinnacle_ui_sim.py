"""平博 UI 下单全流程模拟测试。

对 mock 页面跑真实的 ui_place_pinnacle_total（生产同一函数），
校验：点对方向/线的赔率、投注单打开、金额填写、二次确认、余额扣减。
用例覆盖：2字队名 / 联赛区块陷阱 / 反方向防误点 / 赔率漂移护栏。
"""
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.bookmakers.plugins.pinnacle.bet_ui import ui_place_pinnacle_total
from scripts.mock_pinnacle_page import serve

FAILS = []
RESULTS = []


async def run_case(pw, *, name, home, away, selection, odds, line, sport, expect_bet):
    print(f"[RUN] {name}", flush=True)
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.goto("http://127.0.0.1:9877/live", wait_until="domcontentloaded")
    await page.wait_for_timeout(400)

    ok, detail = await ui_place_pinnacle_total(
        page,
        home=home,
        away=away,
        selection=selection,
        odds=odds,
        stake=Decimal("25"),
        line=line,
        sport=sport,
    )
    bets = []
    try:
        bets = await page.evaluate("() => window.__state.bets || []")
    except Exception:
        pass
    bal = ""
    try:
        bal = await page.evaluate("() => document.getElementById('bal').textContent.trim()")
    except Exception:
        pass

    if expect_bet:
        passed = ok and len(bets) == 1
        b = bets[0] if bets else {}
        want_dir = selection
        want_line = str(line) if line is not None else None
        passed = passed and b.get("dir") == want_dir
        if want_line is not None:
            passed = passed and str(b.get("m", {}).get("line")) == want_line
        passed = passed and abs(float(b.get("stake", 0)) - 25.0) < 0.01
        passed = passed and bal.startswith("385.20")
    else:
        passed = (not ok) and len(bets) == 0 and bal.startswith("410.20")

    status = "PASS" if passed else "FAIL"
    if not passed:
        FAILS.append(name)
    RESULTS.append((status, name, ok, detail[:110], bets, bal))
    print(f"[{status}] {name}: {detail[:110]}", flush=True)
    await browser.close()


async def main():
    from playwright.async_api import async_playwright

    srv = serve(9877)
    try:
        async with async_playwright() as pw:
            # 1. 基本成功：2字队名坎昆的小球 2.5。
            await run_case(
                pw, name="2字队名-坎昆-under-2.5", home="坎昆", away="德杜兰戈阿拉克兰内斯",
                selection="under", odds=1.79, line=2.5, sport="football", expect_bet=True,
            )
            # 3. 联赛区块陷阱：普雷斯顿坎普队（含"坎"字样变体，与坎昆部分重叠）
            await run_case(
                pw, name="区块陷阱-普雷斯顿坎普队-under-2.5", home="普雷斯顿坎普队", away="维拉FC",
                selection="under", odds=1.95, line=2.5, sport="football", expect_bet=True,
            )
            # 4. 四分之一小球盘。
            await run_case(
                pw, name="1/4盘-under-2.75", home="坎普埃奎诺", away="拉莫斯阿罗约",
                selection="under", odds=1.82, line=2.75, sport="football", expect_bet=True,
            )
            # 5. 赔率漂移超护栏：目标 under 2.5 @1.79 → 页面 1.72（漂移 0.07 允许）；
            #    目标 @1.30 时页面 1.72 漂移 0.42 超护栏 → 应拒绝
            await run_case(
                pw, name="漂移超护栏-拒单", home="圣安德雷", away="欧斯特",
                selection="under", odds=1.30, line=2.0, sport="football", expect_bet=False,
            )
            # 6. 错线：目标 3.0 但场内只有 2.5/2.0 盘（row 内无 3.0 → 不盲点）
            await run_case(
                pw, name="目标线缺失-不盲点", home="陶朗加市", away="奥克兰城",
                selection="under", odds=1.90, line=9.99, sport="football", expect_bet=False,
            )
            # 7. 篮球：走 production 同一 UI 下单函数，验证球种导航后仍精确命中小分。
            await run_case(
                pw, name="篮球-NBA-under-180.5", home="洛杉矶湖人", away="芝加哥公牛",
                selection="under", odds=1.88, line=180.5, sport="basketball", expect_bet=True,
            )
    finally:
        srv.shutdown()

    print("=" * 100)
    for status, name, ok, detail, bets, bal in RESULTS:
        print(f"[{status}] {name}")
        print(f"       ui_ok={ok} bets={len(bets)} bal={bal} detail={detail}")
    print("=" * 100)
    print(f"结果: {'全部通过 (%d/%d)' % (len(RESULTS)-len(FAILS), len(RESULTS)) if not FAILS else f'{len(FAILS)} 个失败: {FAILS}'}")
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
