import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from lifespan_state import _register_retry_loop


@pytest.mark.asyncio
async def test_retry_loop_succeeds_on_third_attempt():
    fleet = MagicMock()
    fleet.register_robot = AsyncMock(side_effect=[
        {"ok": False, "error": "ping failed"},
        {"ok": False, "error": "ping failed"},
        {"ok": True, "serial": "BKP"},
    ])
    fake_sleep = AsyncMock()
    await _register_retry_loop(
        fleet, "kachaka", "1.2.3.4", "Kachaka Care",
        initial_delay=0.0, sleep=fake_sleep,
    )
    assert fleet.register_robot.call_count == 3


@pytest.mark.asyncio
async def test_retry_loop_cancellable():
    fleet = MagicMock()
    fleet.register_robot = AsyncMock(return_value={"ok": False, "error": "ping failed"})
    task = asyncio.create_task(
        _register_retry_loop(fleet, "kachaka", "1.2.3.4", "Kachaka Care", initial_delay=10.0)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_retry_loop_caps_at_60s_and_doubles():
    """Pin the back-off schedule: 1, 2, 4, 8, 16, 32, 60, 60, ..."""
    fleet = MagicMock()
    fleet.register_robot = AsyncMock(return_value={"ok": False, "error": "ping failed"})

    sleeps: list[float] = []
    async def fake_sleep(d: float) -> None:
        sleeps.append(d)
        if len(sleeps) >= 8:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _register_retry_loop(
            fleet, "kachaka", "1.2.3.4", "Kachaka Care",
            initial_delay=1.0, sleep=fake_sleep,
        )
    assert sleeps[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert sleeps[7] == 60.0
