import asyncio

from app.services import daily_pnl


class _Cache:
    def __init__(self):
        self.values = {}
        self.json_values = {}

    async def delete(self, key):
        removed = int(key in self.values or key in self.json_values)
        self.values.pop(key, None)
        self.json_values.pop(key, None)
        return removed

    async def set(self, key, value, ttl=None):
        self.values[key] = (value, ttl)
        return True

    async def set_json(self, key, value, ttl=None):
        self.json_values[key] = (value, ttl)
        return True


def test_reset_pnl_baseline_replaces_snapshot_and_daily_baseline(monkeypatch):
    fake_cache = _Cache()
    monkeypatch.setattr(daily_pnl, "cache", fake_cache)

    result = asyncio.run(daily_pnl.reset_pnl_baseline(42, 123.456))

    snapshot_key = daily_pnl._BALANCE_SNAPSHOT_KEY.format(user_id=42)
    assert result["total_assets"] == 123.46
    assert result["balance_delta"] == 0.0
    assert result["reference_balance"] == 123.46
    assert fake_cache.json_values[snapshot_key][0]["reference_balance"] == 123.46
    assert fake_cache.json_values[snapshot_key][1] == 0
    assert len(fake_cache.values) == 1
    assert next(iter(fake_cache.values.values())) == ("123.46", 90000)
