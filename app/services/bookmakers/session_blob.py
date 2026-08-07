"""会话快照：非 Kaiyun 站用 cookies + localStorage 作为 session_token。"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

SESS_PREFIX = "sess:"


def encode_session_blob(*, cookies: list[dict], storage: dict, token_hint: str = "") -> str:
    payload = {
        "cookies": cookies or [],
        "storage": storage or {},
        "token": token_hint or "",
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return SESS_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii")


def decode_session_blob(token: str) -> Optional[dict]:
    t = (token or "").strip()
    if not t.startswith(SESS_PREFIX):
        return None
    try:
        raw = base64.urlsafe_b64decode(t[len(SESS_PREFIX) :].encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.warning("decode_session_blob failed: %s", e)
    return None


def is_session_blob(token: str) -> bool:
    return (token or "").startswith(SESS_PREFIX)


async def read_page_storage(page) -> dict[str, str]:
    try:
        data = await page.evaluate(
            """() => {
              const out = {};
              try {
                for (let i = 0; i < localStorage.length; i++) {
                  const k = localStorage.key(i);
                  if (k) out[k] = localStorage.getItem(k) || '';
                }
              } catch (e) {}
              return out;
            }"""
        )
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def apply_session_blob(context, page, token: str) -> str:
    """恢复 cookies/localStorage；返回可用的短 token（若有）。"""
    blob = decode_session_blob(token)
    if not blob:
        # 纯字符串 token：写入常见 key
        if token:
            try:
                await page.evaluate(
                    """(t) => {
                      try {
                        localStorage.setItem('X-API-TOKEN', t);
                        localStorage.setItem('token', t);
                        localStorage.setItem('access_token', t);
                      } catch (e) {}
                    }""",
                    token,
                )
            except Exception:
                pass
        return token
    cookies = blob.get("cookies") or []
    if cookies:
        try:
            await context.add_cookies(cookies)
        except Exception as e:
            logger.warning("add_cookies failed: %s", e)
    storage = blob.get("storage") or {}
    if storage:
        try:
            await page.evaluate(
                """(obj) => {
                  try {
                    Object.entries(obj || {}).forEach(([k, v]) => {
                      try { localStorage.setItem(k, v == null ? '' : String(v)); } catch (e) {}
                    });
                  } catch (e) {}
                }""",
                storage,
            )
        except Exception as e:
            logger.warning("restore storage failed: %s", e)
    hint = str(blob.get("token") or "")
    if not hint:
        for k in ("X-API-TOKEN", "token", "access_token", "accessToken", "Authorization"):
            v = storage.get(k)
            if v:
                hint = str(v)
                break
    return hint or token


async def capture_session_token(context, page, preferred_keys: list[str] | None = None) -> str:
    storage = await read_page_storage(page)
    cookies = []
    try:
        cookies = await context.cookies()
    except Exception:
        cookies = []
    hint = ""
    for k in preferred_keys or []:
        v = storage.get(k)
        if v:
            hint = str(v)
            break
    if not hint:
        for k in ("X-API-TOKEN", "token", "access_token", "accessToken", "Authorization"):
            if storage.get(k):
                hint = str(storage[k])
                break
    # Kaiyun：优先返回纯 TOKEN，便于 HTTP 校验
    if hint and storage.get("X-API-TOKEN") == hint and len(hint) > 20:
        return hint
    return encode_session_blob(cookies=cookies, storage=storage, token_hint=hint)


def pick_token_from_storage(storage: dict[str, Any], keys: list[str] | None = None) -> str:
    for k in keys or []:
        v = storage.get(k)
        if v:
            return str(v)
    for k in ("X-API-TOKEN", "token", "access_token", "accessToken", "Authorization"):
        if storage.get(k):
            return str(storage[k])
    return ""
