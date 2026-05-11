import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_reregister_cancels_lifespan_retry(monkeypatch):
    """When IP changes, the old-IP retry loop must be cancelled before the
    new register_robot call — otherwise it can succeed against the stale IP."""
    import lifespan_state
    from routers.settings import _reregister_robot

    cancelled = asyncio.Event()

    async def _fake_retry():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    retry = asyncio.create_task(_fake_retry())
    await asyncio.sleep(0)  # let the task reach its first await
    lifespan_state._register_retry_tasks["kachaka"] = retry

    fake_fleet = MagicMock()
    fake_fleet.unregister_robot = AsyncMock(return_value=True)
    fake_fleet.register_robot = AsyncMock(return_value={"ok": True, "serial": "BKP"})

    monkeypatch.setattr("dependencies.get_fleet", lambda: fake_fleet)
    monkeypatch.setattr("routers.settings.engines", {"kachaka": object()})

    result = await _reregister_robot("9.9.9.9")
    assert result["ok"]
    assert cancelled.is_set()
    assert "kachaka" not in lifespan_state._register_retry_tasks
