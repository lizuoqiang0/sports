import asyncio

from app.core import worker_leader


class _Client:
    async def set(self, *_args, **_kwargs):
        return False

    async def get(self, _key):
        return worker_leader.worker_id()

    async def expire(self, *_args, **_kwargs):
        raise AssertionError("leader renewal must use atomic compare-and-expire")


def test_leader_renewal_uses_atomic_owner_check(monkeypatch) -> None:
    class _Cache:
        client = _Client()

        async def extend_lock_if_owned(self, key, token, *, ttl_sec):
            assert key == "ob:worker:leader:background"
            assert token == worker_leader.worker_id()
            assert ttl_sec == 25
            return True

    monkeypatch.setattr("app.core.cache.cache", _Cache())

    assert asyncio.run(worker_leader._try_acquire()) is True
