"""CORNER-031: shutdown_robot_tasks drains retries → workers → unregister in order."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import lifespan_state


@pytest.mark.asyncio
async def test_shutdown_cancels_retry_before_worker_before_unregister():
    order: list[str] = []

    async def long_retry():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            order.append("retry-cancelled")
            raise

    async def long_worker():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            order.append("worker-cancelled")
            raise

    retry = asyncio.create_task(long_retry())
    worker = asyncio.create_task(long_worker())
    lifespan_state._register_retry_tasks["kachaka"] = retry
    lifespan_state._worker_tasks["kachaka"] = worker

    fake_fleet = MagicMock()
    fake_fleet._robots = {"kachaka": object()}
    async def _unreg(rid):
        order.append(f"unregister:{rid}")
        return True
    fake_fleet.unregister_robot = AsyncMock(side_effect=_unreg)

    await lifespan_state.shutdown_robot_tasks(fake_fleet)

    assert order == ["retry-cancelled", "worker-cancelled", "unregister:kachaka"]
    assert lifespan_state._register_retry_tasks == {}
    assert lifespan_state._worker_tasks == {}
