"""
Redis 缓存层 - 赔率缓存、会话管理、限流
"""
import json
import logging
from typing import Optional, Any

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)


class CacheManager:
    """异步Redis缓存封装"""

    def __init__(self):
        self._client: Optional[redis.Redis] = None

    async def connect(self):
        """建立连接池"""
        self._client = redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=settings.REDIS_MAX_CONNECTIONS,
            socket_connect_timeout=2.0,
            socket_timeout=None,
            health_check_interval=30,
        )
        await self._client.ping()
        logger.info("Redis connected")

    async def close(self):
        """关闭连接"""
        if self._client:
            await self._client.close()

    @property
    def client(self) -> redis.Redis:
        if not self._client:
            raise RuntimeError("Cache not connected. Call connect() first.")
        return self._client

    # === 基础操作 ===
    async def get(self, key: str) -> Optional[str]:
        return await self.client.get(key)

    async def set(self, key: str, value: str, ttl: Optional[int] = None) -> bool:
        if ttl is None:
            ttl = settings.REDIS_CACHE_TTL
        if ttl and ttl > 0:
            return await self.client.set(key, value, ex=ttl)
        return await self.client.set(key, value)

    async def delete(self, key: str) -> int:
        return await self.client.delete(key)

    async def exists(self, key: str) -> bool:
        return bool(await self.client.exists(key))

    # === JSON操作 ===
    async def set_json(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        return await self.set(key, json.dumps(value, ensure_ascii=False), ttl)

    async def get_json(self, key: str) -> Optional[Any]:
        data = await self.get(key)
        if data:
            return json.loads(data)
        return None

    # === 哈希操作 (适合赔率存储) ===
    async def hset(self, key: str, field: str, value: Any) -> int:
        return await self.client.hset(key, field, json.dumps(value))

    async def hget(self, key: str, field: str) -> Optional[dict]:
        data = await self.client.hget(key, field)
        if data:
            return json.loads(data)
        return None

    async def hgetall(self, key: str) -> dict:
        data = await self.client.hgetall(key)
        return {k: json.loads(v) for k, v in data.items()}

    async def hdel(self, key: str, *fields: str) -> int:
        return await self.client.hdel(key, *fields)

    # === 列表操作 (流水/消息队列) ===
    async def lrange(self, key: str, start: int = 0, end: int = -1) -> list[str]:
        return await self.client.lrange(key, start, end)

    # === 发布订阅 (赔率变动通知) ===
    async def publish(self, channel: str, message: dict) -> int:
        return await self.client.publish(channel, json.dumps(message))

    async def subscribe(self, channel: str) -> redis.client.PubSub:
        pubsub = self.client.pubsub()
        await pubsub.subscribe(channel)
        return pubsub

    # === 分布式锁（下单防重/引擎互斥） ===
    async def acquire_lock(self, key: str, *, ttl_sec: int = 120, token: str = "1") -> bool:
        """SET NX EX；成功返回 True。"""
        try:
            ok = await self.client.set(key, token, nx=True, ex=max(5, int(ttl_sec)))
            return bool(ok)
        except Exception as e:
            logger.warning("acquire_lock failed key=%s: %s", key, e)
            return False

    async def release_lock(self, key: str, token: str = "1") -> None:
        """仅释放自己持有的锁。"""
        lua = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        end
        return 0
        """
        try:
            await self.client.eval(lua, 1, key, token)
        except Exception as e:
            logger.debug("release_lock failed key=%s: %s", key, e)

    async def extend_lock_if_owned(self, key: str, token: str, *, ttl_sec: int) -> bool:
        """仅续期自己持有的锁（Lua 原子校验）。用于长周期任务的看门狗。"""
        lua = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('expire', KEYS[1], ARGV[2])
        end
        return 0
        """
        try:
            return bool(await self.client.eval(lua, 1, key, token, int(ttl_sec)))
        except Exception as e:
            logger.debug("extend_lock_if_owned failed key=%s: %s", key, e)
            return False

    # === 便捷方法 ===
    async def get_cached_odds(self, match_id: int) -> Optional[dict]:
        return await self.get_json(f"odds:match:{match_id}")


cache = CacheManager()
