"""Safe Pinnacle modal handling and failed-slip cleanup."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _page_targets(page: Any) -> list[Any]:
    targets = [page]
    try:
        main = getattr(page, "main_frame", None)
        for frame in list(getattr(page, "frames", []) or []):
            if frame is main:
                continue
            targets.append(frame)
    except Exception:
        pass
    return targets


_DISMISS_BLOCKER_JS = r"""() => {
  // PINNACLE_DISMISS_BLOCKER
  const textOf = (el) => String(
    el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || ''
  ).replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    try {
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st && st.display !== 'none' && st.visibility !== 'hidden'
        && st.opacity !== '0' && r.width > 20 && r.height > 15;
    } catch (e) { return false; }
  };
  const roots = Array.from(document.querySelectorAll(
    '[role="alert"], [role="dialog"], [aria-modal="true"], '
    + '[class*="modal" i], [class*="dialog" i], [class*="popup" i], '
    + '[class*="notice" i], [class*="toast" i], [class*="message" i]'
  )).filter(visible).sort((a, b) => textOf(a).length - textOf(b).length);
  const protectedBet = /确认投注|您是否想要投注|清空注单|清除注单|您是否想要清空注单/;
  const blocker = /当前选项不适用|选择的注单暂时无效|暂时无效|余额不足|不能低于|无法|失败|已取消|已接受|投注成功|投注已|限额|拒绝|错误|请稍后|公告|活动|优惠|欢迎|提示|消息|更新|维护|版权|专有|保留所有权利|copyright|cookie|隐私|条款/i;
  const tradePwd = /交易密码|支付密码|资金密码|提款密码|fund\s*password|pay\s*password/i;
  // 安全推广弹窗只能选择「暂不/关闭」，绝不能误点启用 2FA 的主按钮。
  const securityPrompt = /为您的账户添加额外保护|双重验证|二次验证|登录时启用.*验证|\b2FA\b|two[ -]?factor authentication/i;
  const safeClose = /^(好的|好|知道了|我知道了|关闭|暂不|稍后|跳过|继续|同意|接受|OK|确定|Close|Cancel|I agree|Accept|Continue|×|X)$/i;
  const cancelOnly = /^(关闭|取消|暂不|稍后|不，谢谢|Close|Cancel|Not now|Maybe later|No thanks|×|X)$/i;
  for (const root of roots) {
    const body = textOf(root);
    if (!body || body.length > 900 || protectedBet.test(body)) continue;
    if (!blocker.test(body) && !tradePwd.test(body) && !securityPrompt.test(body)) continue;
    const nodes = Array.from(root.querySelectorAll(
      'button, a, [role="button"], input[type="button"], input[type="submit"], [class*="close" i]'
    )).filter(visible);
    for (const el of nodes) {
      const label = textOf(el);
      if ((tradePwd.test(body) || securityPrompt.test(body)) ? cancelOnly.test(label) : safeClose.test(label)) {
        try { el.click(); return { clicked: label, prompt: body.slice(0, 100) }; } catch (e) {}
      }
    }
  }
  return { clicked: '', prompt: '' };
}"""


_CANCEL_BET_CONFIRM_JS = r"""() => {
  // PINNACLE_CANCEL_BET_CONFIRM
  const roots = Array.from(document.querySelectorAll(
    '[role="dialog"], [aria-modal="true"], [class*="modal" i], [class*="dialog" i], [class*="popup" i], div, section, aside'
  )).filter((root) => {
    const body = String(root.innerText || root.textContent || '');
    return body.length < 800 && /确认投注|您是否想要投注/.test(body)
      && /取消|Cancel/i.test(body) && /\bOK\b/i.test(body);
  }).sort((a, b) => String(a.innerText || '').length - String(b.innerText || '').length);
  for (const root of roots) {
    const body = String(root.innerText || root.textContent || '');
    if (!/确认投注|您是否想要投注/.test(body)) continue;
    for (const el of root.querySelectorAll('button, a, [role="button"], input')) {
      const label = String(el.innerText || el.textContent || el.value || '').replace(/\s+/g, ' ').trim();
      if (label === '取消' || /^Cancel$/i.test(label)) {
        try { el.click(); return label; } catch (e) {}
      }
    }
  }
  return '';
}"""


_CLICK_CLEAR_ALL_JS = r"""() => {
  // PINNACLE_CLICK_CLEAR_ALL
  const visible = (el) => {
    try {
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st && st.display !== 'none' && st.visibility !== 'hidden'
        && r.width > 15 && r.height > 10;
    } catch (e) { return false; }
  };
  const candidates = Array.from(document.querySelectorAll('button, a, [role="button"], span, div'))
    .filter((node) => /^(清除全部|移除全部|Remove All)$/i.test(
      String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim()
    ))
    .sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  for (const node of candidates) {
    const label = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
    const el = node.matches('button, a, [role="button"]')
      ? node : (node.closest('button, a, [role="button"]') || node);
    // 真实 compact UI 的「清除全部」可能是绑定点击事件的裸 span/div。
    // 文案已全字精确匹配，因此允许点击节点本身并依靠事件冒泡。
    if (!visible(node) || !visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    try { el.click(); return label; } catch (e) {}
  }
  return '';
}"""


_CONFIRM_CLEAR_JS = r"""() => {
  // PINNACLE_CONFIRM_CLEAR
  const textOf = (el) => String(el.innerText || el.textContent || el.value || '')
    .replace(/\s+/g, ' ').trim();
  const roots = Array.from(document.querySelectorAll(
    '[role="dialog"], [aria-modal="true"], [class*="modal" i], [class*="dialog" i], [class*="popup" i], div, section, aside'
  )).filter((root) => {
    const body = textOf(root);
    return body.length < 600 && /您是否想要清空注单|清空注单|清除注单/.test(body)
      && /好的|OK|确定/.test(body) && /取消|Cancel/i.test(body);
  })
    .sort((a, b) => textOf(a).length - textOf(b).length);
  for (const root of roots) {
    const actions = Array.from(root.querySelectorAll(
      'button, a, [role="button"], input, span, div'
    )).filter((el) => /^(好的|OK|确定)$/.test(textOf(el)))
      .sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
    for (const node of actions) {
      const label = textOf(node);
      if (label === '好的' || label === 'OK' || label === '确定') {
        const el = node.matches('button, a, [role="button"], input')
          ? node : (node.closest('button, a, [role="button"]') || node);
        try { el.click(); return label; } catch (e) {}
      }
    }
  }
  return '';
}"""


_VERIFY_SLIP_EMPTY_JS = r"""() => {
  // PINNACLE_VERIFY_SLIP_EMPTY
  const body = String((document.body && document.body.innerText) || '');
  const counts = Array.from(body.matchAll(/投下\s*(\d+)\s*注/g)).map((m) => Number(m[1]));
  const hasBet = counts.some((n) => n > 0);
  const clearDialog = /您是否想要清空注单|清空注单/.test(body);
  let clearButton = false;
  for (const el of document.querySelectorAll('button, a, [role="button"], span, div')) {
    const t = String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (/^(清除全部|移除全部|Remove All)$/i.test(t)
        && !el.disabled && el.getAttribute('aria-disabled') !== 'true') clearButton = true;
  }
  return { empty: !hasBet && !clearDialog && !clearButton, hasBet, clearDialog, clearButton };
}"""


async def dismiss_pinnacle_blocking_modals(page: Any, *, max_clicks: int = 6) -> list[str]:
    """Close known blocking notices without touching bet/clear confirmations."""
    actions: list[str] = []
    for _ in range(max(1, int(max_clicks))):
        clicked = False
        for target in _page_targets(page):
            try:
                result = await asyncio.wait_for(
                    target.evaluate(_DISMISS_BLOCKER_JS), timeout=3.0
                )
            except Exception:
                continue
            if isinstance(result, dict) and result.get("clicked"):
                action = f"{result.get('clicked')}:{str(result.get('prompt') or '')[:80]}"
                actions.append(action)
                logger.info("pinnacle blocker dismissed: %s", action)
                clicked = True
                try:
                    await page.wait_for_timeout(250)
                except Exception:
                    pass
                break
        if not clicked:
            break
    return actions


async def cleanup_pinnacle_failed_slips(page: Any) -> tuple[bool, str]:
    """Cancel pending confirmation, then perform 清除全部 -> 好的 and verify empty."""
    targets = _page_targets(page)

    for target in targets:
        try:
            cancelled = await asyncio.wait_for(
                target.evaluate(_CANCEL_BET_CONFIRM_JS), timeout=2.5
            )
        except Exception:
            continue
        if cancelled:
            logger.info("pinnacle pending confirmation cancelled before cleanup")
            try:
                await page.wait_for_timeout(350)
            except Exception:
                pass
            break

    await dismiss_pinnacle_blocking_modals(page, max_clicks=4)

    last = "clear_button_missing"
    for _ in range(3):
        hit = ""
        for target in targets:
            try:
                hit = str(
                    await asyncio.wait_for(
                        target.evaluate(_CLICK_CLEAR_ALL_JS), timeout=3.0
                    )
                    or ""
                )
            except Exception:
                continue
            if hit:
                break

        if hit:
            try:
                await page.wait_for_timeout(400)
            except Exception:
                pass
            confirmed = ""
            for _wait in range(8):
                for target in targets:
                    try:
                        confirmed = str(
                            await asyncio.wait_for(
                                target.evaluate(_CONFIRM_CLEAR_JS), timeout=2.5
                            )
                            or ""
                        )
                    except Exception:
                        continue
                    if confirmed:
                        break
                if confirmed:
                    break
                try:
                    await page.wait_for_timeout(200)
                except Exception:
                    pass
            last = f"clicked:{hit}|confirmed:{confirmed or 'missing'}"
            try:
                await page.wait_for_timeout(450)
            except Exception:
                pass

        for target in targets:
            try:
                state = await asyncio.wait_for(
                    target.evaluate(_VERIFY_SLIP_EMPTY_JS), timeout=2.5
                )
            except Exception:
                continue
            if isinstance(state, dict) and state.get("empty"):
                logger.info("pinnacle failed slip cleanup verified: %s", last)
                return True, f"{last}|empty_verified"
            if isinstance(state, dict):
                last = f"{last}|state:{state}"
        await dismiss_pinnacle_blocking_modals(page, max_clicks=2)

    logger.warning("pinnacle failed slip cleanup not verified: %s", last)
    return False, last
