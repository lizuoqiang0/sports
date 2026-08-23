"""
模拟平博成功确认弹窗，测试订单号提取逻辑。

运行: python3 scripts/test_pinnacle_bet_ref.py
"""
import asyncio
import re
from decimal import Decimal


async def test_bet_ref_extraction():
    """用真实的 Playwright 浏览器上下文模拟弹窗，验证 ref_js 提取逻辑。"""
    from playwright.async_api import async_playwright

    # 模拟弹窗的 HTML（含平博真实场景中可能出现的成功确认文本）
    mock_html = """
    <html><body>
      <!-- 模拟平博成功确认弹窗 -->
      <div class="modal-overlay" role="dialog" style="display:block">
        <div class="modal-content">
          <div class="success-message" role="alert">
            投注成功 Bet ID: PCL-8X3921K
          </div>
          <div class="bet-detail">
            <span>注单号: 92384756</span><br>
            <span>Wager Reference: WGR-2026-0823-7741</span><br>
            <span>投注金额: 50.00</span><br>
            <span>赔率: 1.85</span>
          </div>
          <button class="ok-btn">OK</button>
        </div>
      </div>
    </body></html>
    """

    # 与 bet_ui.py 中完全一致的 ref_js
    ref_js = """() => {
      const out = [];
      for (const el of document.querySelectorAll('[role="alert"], [class*="modal" i], [class*="dialog" i], [class*="toast" i], [class*="message" i], [class*="notice" i], [class*="success" i], [class*="confirm" i]')) {
        const t = String(el.innerText || '').replace(/\\s+/g, ' ').trim();
        const st = window.getComputedStyle(el);
        if (!t || t.length < 4 || t.length > 500) continue;
        if (st && (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0')) continue;
        out.push(t);
      }
      const body = String((document.body && document.body.innerText) || '');
      const re = /(?:Bet\\s*ID|Wager\\s*Reference|Order|Ticket|Ref|注单号|确认号|订单号|编号)[:#\\s]*([A-Za-z0-9\\-]{6,20})/gi;
      const m = body.match(re);
      if (m) {
        for (const match of m.slice(0, 3)) {
          const idMatch = match.match(/([A-Za-z0-9\\-]{6,20})$/);
          if (idMatch) out.push('ref:' + idMatch[1]);
        }
      }
      const url = String(window.location.href || '');
      const urlMatch = url.match(/(?:bet|order|ticket|wager)[/=]([A-Za-z0-9\\-]{6,20})/i);
      if (urlMatch) out.push('url:' + urlMatch[1]);
      return out.slice(0, 5).join(' ;; ');
    }"""

    # 与 bet_ui.py 中完全一致的解析逻辑
    def parse_bet_ref(ref_text: str) -> str:
        bet_ref = ""
        for part in str(ref_text).split(" ;; "):
            part = part.strip()
            if part.startswith("ref:"):
                bet_ref = part[4:].strip()
                break
            if part.startswith("url:"):
                bet_ref = part[4:].strip()
                break
        if not bet_ref:
            id_match = re.search(r'([A-Za-z0-9\-]{8,20})', str(ref_text))
            if id_match:
                bet_ref = id_match.group(1)
        return bet_ref

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(mock_html)

        # 执行 ref_js 提取
        ref_text = await page.evaluate(ref_js)
        print("=" * 60)
        print("1. 原始提取结果")
        print("=" * 60)
        print(f"   ref_text: {ref_text}")

        bet_ref = parse_bet_ref(ref_text)
        print()
        print("=" * 60)
        print("2. 解析出的订单号")
        print("=" * 60)
        print(f"   bet_ref: {bet_ref}")

        # 测试不同弹窗场景
        scenarios = [
            ("Bet ID 弹窗", '<div role="alert">投注成功 Bet ID: PCL-8X3921K</div>'),
            ("注单号弹窗", '<div class="success-msg">下单成功 订单号: 92384756</div>'),
            ("Wager Reference", '<div class="confirm">Wager Reference: WGR-2026-8841</div>'),
            ("纯数字 Ticket", '<div class="modal">Ticket #72930451 已接受</div>'),
            ("中文确认号", '<div class="notice">确认号: CNF-2026-5582</div>'),
            ("无订单号(余额不足)", '<div class="error">余额不足，请充值</div>'),
        ]

        print()
        print("=" * 60)
        print("3. 多场景测试")
        print("=" * 60)
        for name, html in scenarios:
            await page.set_content(f"<html><body>{html}</body></html>")
            text = await page.evaluate(ref_js)
            ref = parse_bet_ref(text)
            status = f"✅ 提取到: {ref}" if ref else "❌ 未提取到"
            print(f"   {name:20s} → {status}")

        await browser.close()

    print()
    print("=" * 60)
    print("✅ 测试完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_bet_ref_extraction())
