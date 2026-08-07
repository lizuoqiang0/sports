"""
本机 Chromium 启动加固（macOS Crashpad/xattr 权限导致 launch SIGABRT）。

仅影响浏览器进程启动参数；不改动盘口采数 / 下单点击流程。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _real_home() -> Path:
    """真实用户 HOME；拒绝指到仓库内的假 HOME。"""
    repo = str(_repo_root())
    candidates: list[Path] = []
    for key in ("HOME", "USERPROFILE"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            candidates.append(Path(raw).expanduser())
    candidates.append(Path.home())
    for p in candidates:
        try:
            rp = p.resolve()
        except Exception:
            continue
        if not rp.is_dir():
            continue
        # 曾出现 HOME=…/ob-sports-betting/瓦勒伦加 → Crashpad 写 Application Support 失败
        if str(rp).startswith(repo + os.sep) or str(rp) == repo:
            continue
        return rp
    return Path.home()


def chrome_runtime_dir() -> Path:
    home = _real_home()
    if (home / "Library" / "Caches").exists() or (home / "Library").exists():
        base = home / "Library" / "Caches" / "ob-sports-betting"
    else:
        base = home / ".cache" / "ob-sports-betting"
    base.mkdir(parents=True, exist_ok=True)
    (base / "chrome-crashes").mkdir(parents=True, exist_ok=True)
    return base


def prepare_chromium_env() -> Path:
    """为 Gate/登录进程设置可写 Crashpad 目录，减少 setxattr Operation not permitted。"""
    home = _real_home()
    os.environ["HOME"] = str(home)
    runtime = chrome_runtime_dir()
    crash = runtime / "chrome-crashes"
    os.environ["BREAKPAD_DUMP_LOCATION"] = str(crash)
    os.environ["CHROME_LOG_FILE"] = str(runtime / "chrome.log")
    return runtime

def chromium_launch_args(*, maximized: bool = True) -> list[str]:
    runtime = prepare_chromium_env()
    crash = runtime / "chrome-crashes"
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-crash-reporter",
        "--disable-breakpad",
        f"--crash-dumps-dir={crash}",
        "--disable-features=Crashpad,CrashReporting",
        "--disable-dev-shm-usage",
    ]
    if maximized:
        args.append("--start-maximized")
    return args


async def launch_headed_chromium(pw: Any, *, maximized: bool = True):
    """Playwright chromium.launch(headless=False) 加固版。

    返回 Browser 或 BrowserContext（persistent 模式）。
    调用方应通过 hasattr(result, 'new_context') 区分。
    """
    prepare_chromium_env()
    args = chromium_launch_args(maximized=maximized)
    try:
        return await pw.chromium.launch(headless=False, args=args)
    except Exception as e1:
        logger.warning("chromium launch failed (%s), retry without maximized", e1)
        args2 = [a for a in args if a != "--start-maximized"]
        try:
            return await pw.chromium.launch(headless=False, args=args2)
        except Exception as e2:
            # Playwright 1.48+ 禁止 --user-data-dir 参数，改用 launch_persistent_context
            ud = chrome_runtime_dir() / "chrome-ud-tmp"
            ud.mkdir(parents=True, exist_ok=True)
            logger.warning("chromium launch retry with persistent context (%s): %s", ud, e2)
            return await pw.chromium.launch_persistent_context(
                user_data_dir=str(ud),
                headless=False,
                args=args2,
            )
