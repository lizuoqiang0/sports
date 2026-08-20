"""余额失效保护单元测试：Gate 会话掉线（余额全非 live）时不误触止损。"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PASS = "\033[32m✅\033[0m"
FAIL = "\033[31m❌\033[0m"


def test_code_guard():
    """calc_daily_pnl 含数据可信性保护。"""
    import app.ai.strategy_gates as sg

    src = inspect.getsource(sg.calc_daily_pnl)
    return [
        ("全站非 live → 返回 Decimal(0)（中性，不触发风控）",
         'return Decimal("0")' in src, ""),
        ("live 判定含 is_live 或 live 字段", "is_live" in src and "live" in src, ""),
        ("无站点数据（空列表）不误保护（正常路径）", "if site_balances and not live_sites" in src, ""),
        ("触发保护有告警日志", "logger.warning" in src, ""),
    ]


async def _test_behavior_async():
    """行为测试：模拟三种余额状态。"""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.ai.strategy_gates import calc_daily_pnl

    results = []

    def _mk_db():
        db = AsyncMock()
        pending_res = MagicMock()
        pending_res.scalar.return_value = 0  # 同步方法（SQLAlchemy scalar）
        db.execute = AsyncMock(return_value=pending_res)
        return db

    # 场景1：全部非 live（Gate 掉线）→ 保护生效返回 0
    dead_balances = [
        {"code": "ob", "balance": 0.0, "is_live": False, "live": False},
        {"code": "pinnacle", "balance": 0.0, "is_live": False, "live": False},
    ]
    db = _mk_db()
    with patch("app.services.balances.load_site_balances", return_value=dead_balances), \
         patch("app.services.daily_pnl.get_daily_pnl", new_callable=AsyncMock) as gp:
        got = await calc_daily_pnl(db, 1)
        results.append((
            "全站非 live（Gate掉线）→ pnl=0（不触发止损）",
            got == 0 and not gp.called,
            f"pnl={got}, get_daily_pnl called={gp.called}",
        ))

    # 场景2：部分 live → 正常计算
    mixed = [
        {"code": "ob", "balance": 138.26, "is_live": True, "live": True},
        {"code": "pinnacle", "balance": 0.0, "is_live": False, "live": False},
    ]
    db = _mk_db()
    with patch("app.services.balances.load_site_balances", return_value=mixed), \
         patch("app.services.daily_pnl.get_daily_pnl", new_callable=AsyncMock) as gp:
        gp.return_value = {"daily_pnl": -12.34}
        got = await calc_daily_pnl(db, 1)
        results.append((
            "部分站点 live → 正常计算 pnl",
            gp.called and float(got) == -12.34,
            f"pnl={got}",
        ))

    # 场景3：全部 live → 正常计算
    all_live = [
        {"code": "ob", "balance": 138.26, "is_live": True, "live": True},
        {"code": "pinnacle", "balance": 393.97, "is_live": True, "live": True},
    ]
    db = _mk_db()
    with patch("app.services.balances.load_site_balances", return_value=all_live), \
         patch("app.services.daily_pnl.get_daily_pnl", new_callable=AsyncMock) as gp:
        gp.return_value = {"daily_pnl": 5.0}
        got = await calc_daily_pnl(db, 1)
        results.append((
            "全站 live → 正常计算 pnl",
            gp.called and float(got) == 5.0,
            f"pnl={got}",
        ))
    return results


def test_risk_gate_semantics():
    """止损闸门语义：pnl=0（中性）时 stop_loss=100 不触发。"""
    import app.ai.strategy_gates as sg

    src = inspect.getsource(sg.check_daily_risk)
    return [
        ("止损条件 pnl <= -stop（0 > -100 不触发）", "pnl <= -stop" in src, ""),
        ("止盈条件 pnl >= take（0 < 500 不触发）", "pnl >= take" in src, ""),
    ]


async def run_all():
    results = []
    results.extend(test_code_guard())
    results.extend(await _test_behavior_async())
    results.extend(test_risk_gate_semantics())
    return results


def main():
    results = asyncio.run(run_all())
    print("\n" + "=" * 64)
    print("余额失效保护测试（Gate掉线不误触止损）")
    print("=" * 64)
    fails = 0
    for name, ok, detail in results:
        print(f"{PASS if ok else FAIL}  {name}")
        if detail:
            print(f"       {detail}")
        fails += 0 if ok else 1
    print("-" * 64)
    print(f"通过 {len(results) - fails}/{len(results)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
