"""AI 运行状态接口必须是只读接口。"""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from app.api.ai_bets import ai_status
from app.ai import auto_better


class _Db:
    def __init__(self, user):
        self.user = user

    async def get(self, _model, _user_id):
        return self.user


class TestAIStatus(IsolatedAsyncioTestCase):
    async def test_closed_ai_masks_stale_marker_without_stopping_engine(self):
        user = SimpleNamespace(id=7, ai_enabled=False, bet_mode="active")
        stop_engine = AsyncMock()

        with (
            patch("app.api.ai_bets.get_engine_status", new=AsyncMock(return_value={"running": True})),
            patch("app.api.ai_bets.stop_user_engine", new=stop_engine),
            patch(
                "app.ai.recs_job.list_analysis_watching_sports",
                new=AsyncMock(return_value=[]),
            ),
        ):
            response = await ai_status(current_user=user, db=_Db(user))

        self.assertFalse(response.data["engine_running"])
        self.assertEqual(response.data["bet_mode"], "active")
        stop_engine.assert_not_awaited()

    async def test_start_recovers_lock_only_when_running_marker_is_absent(self):
        class _Cache:
            def __init__(self):
                self.client = SimpleNamespace(eval=AsyncMock(return_value=1))

            async def get(self, _key):
                return "stale-owner"

            async def exists(self, _key):
                return False

        cache = _Cache()
        with patch("app.core.cache.cache", cache):
            cleared = await auto_better._clear_stale_engine_lock(7, "ai:engine:lock:7")

        self.assertTrue(cleared)
        cache.client.eval.assert_awaited_once()
