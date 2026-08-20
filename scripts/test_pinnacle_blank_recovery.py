#!/usr/bin/env python3
"""平博白屏/维护遮挡恢复逻辑模拟测试。

用本地 HTTP 服务 + Chromium 模拟三种异常页面状态，验证真实生产恢复函数：
  1. 白屏（body 无文本无控件）→ recover_pinnacle_blank_page 应 reload 恢复
  2. 顽固白屏（reload 后仍白屏）→ 应返回 False 不死循环
  3. 维护横幅（"正在维护"遮罩）→ clear_pinnacle_maintenance 应刷新清除
  4. 正常滚球页 → 恢复函数零副作用（绝不 reload）

reload 证明：页面 JS 用 sessionStorage 计数（reload 保留），通过计数变化证明
reload 真实发生；recover_after=1 表示 reload 后注入正常内容（模拟真实站点
刷新即恢复的行为）。
"""
import asyncio
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.expanduser("~/Downloads/ob-sports"))
_STABLE_PW = os.path.expanduser("~/Library/Caches/ms-playwright")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _STABLE_PW
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

from playwright.async_api import async_playwright  # noqa: E402

from app.services.bookmakers.plugins.pinnacle.venue import (  # noqa: E402
    clear_pinnacle_maintenance,
    page_shows_maintenance,
    pinnacle_page_is_blank,
    recover_pinnacle_blank_page,
)

PASS = "\033[32m✅ PASS\033[0m"
FAIL = "\033[31m❌ FAIL\033[0m"

_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Pinnacle Mock</title></head>
<body>
<div id="app"></div>
<script>
  const params = new URLSearchParams(location.search);
  const mode = params.get('mode') || 'normal';
  const recoverAfter = params.get('recover_after') === '1';
  let n = parseInt(sessionStorage.getItem('reload_n') || '0', 10) + 1;
  sessionStorage.setItem('reload_n', String(n));
  const app = document.getElementById('app');
  app.dataset.reloadN = String(n);

  const effective = (n > 1 && recoverAfter) ? 'normal' : mode;
  if (effective === 'blank') {
    app.innerHTML = '<div></div>';
  } else if (effective === 'maint') {
    app.innerHTML = '<div>系统正在维护，请稍后再试</div>'
      + '<button>滚球</button><button>足球</button>';
  } else {
    app.innerHTML = '<div>滚球 足球 篮球 总进球 大小球</div>'
      + '<button>投下1注</button><button>移除全部</button>'
      + '<span>余额 393.97</span>';
  }
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = _HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start_server(port: int) -> None:
    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


async def _reload_count(page) -> int:
    try:
        v = await page.evaluate(
            "() => parseInt(document.getElementById('app').dataset.reloadN || '0', 10)"
        )
        return int(v or 0)
    except Exception:
        return -1


async def _reset(page, mode: str, recover: str):
    try:
        await page.evaluate("() => sessionStorage.clear()")
    except Exception:
        pass  # 首次导航前在 about:blank，无 sessionStorage
    await page.goto(
        f"http://127.0.0.1:8899/mock.html?mode={mode}&recover_after={recover}",
        wait_until="load",
    )
    await page.wait_for_timeout(250)


async def main():
    port = 8899
    _start_server(port)
    results = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # ---------- 用例 1：白屏 → reload 恢复 ----------
        await _reset(page, "blank", "1")
        assert await pinnacle_page_is_blank(page), "前置失败：应识别为白屏"
        n0 = await _reload_count(page)
        ok = await recover_pinnacle_blank_page(page, attempts=2)
        n1 = await _reload_count(page)
        blank_after = await pinnacle_page_is_blank(page)
        results.append(("白屏 → reload 触发并恢复", ok and n1 > n0 and not blank_after,
                        f"recovered={ok} reload计数:{n0}→{n1} 恢复后白屏={blank_after}"))

        # ---------- 用例 2：顽固白屏 → False 不死循环 ----------
        await _reset(page, "blank", "0")
        ok2 = await recover_pinnacle_blank_page(page, attempts=2)
        results.append(("顽固白屏 → 返回 False（不无限重试）", ok2 is False,
                        f"recovered={ok2}（2次reload后仍白屏，正确放弃）"))

        # ---------- 用例 3：维护横幅 → 刷新清除 ----------
        await _reset(page, "maint", "1")
        assert await page_shows_maintenance(page), "前置失败：应有维护横幅"
        acted = await clear_pinnacle_maintenance(page)
        still = await page_shows_maintenance(page)
        n3 = await _reload_count(page)
        results.append(("维护横幅 → 刷新后清除", acted and not still and n3 > 1,
                        f"检测到横幅={acted} 清除后残留={still} reload计数={n3}"))

        # ---------- 用例 4：正常页 → 零副作用 ----------
        await _reset(page, "normal", "0")
        n4a = await _reload_count(page)
        blank = await pinnacle_page_is_blank(page)
        ok4 = await recover_pinnacle_blank_page(page, attempts=2)
        n4b = await _reload_count(page)
        maint_acted = await clear_pinnacle_maintenance(page)
        n4c = await _reload_count(page)
        results.append(("正常页 → 零副作用（不 reload）",
                        (not blank) and ok4 and not maint_acted and n4c == n4a,
                        f"误判白屏={blank} 恢复函数返回={ok4} 维护误触发={maint_acted} "
                        f"reload计数:{n4a}→{n4b}→{n4c}"))

        await browser.close()

    print("\n" + "=" * 64)
    print("平博白屏/遮挡恢复逻辑测试结果（真实生产函数）")
    print("=" * 64)
    fails = 0
    for name, ok, detail in results:
        print(f"{PASS if ok else FAIL}  {name}")
        print(f"       {detail}")
        fails += 0 if ok else 1
    print("-" * 64)
    print(f"通过 {len(results) - fails}/{len(results)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
