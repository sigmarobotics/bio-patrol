"""HIL: registration retry loop succeeds when robot comes online."""
import asyncio
import os

import pytest

from services.fleet_api import FleetAPI

UNREACHABLE_IP = "192.168.255.254:26400"  # known-bad
REAL_IP = os.environ.get("HIL_ROBOT_IP", "192.168.50.133")


@pytest.mark.hil
@pytest.mark.asyncio
async def test_retry_loop_recovers_when_robot_returns():
    from lifespan_state import _register_retry_loop

    fleet = FleetAPI()

    # First, attempt against a deliberately unreachable IP.
    bad = await fleet.register_robot("kachaka", UNREACHABLE_IP, "Test Robot")
    assert not bad["ok"]

    print(
        "\n>>> The retry loop will switch to the real robot in 3s. "
        "Make sure 192.168.50.133 is reachable. <<<"
    )

    async def _switch():
        await asyncio.sleep(3)
        return await fleet.register_robot("kachaka", REAL_IP, "Real Robot")

    task = asyncio.create_task(
        _register_retry_loop(fleet, "kachaka", REAL_IP, "Real Robot", initial_delay=1.0)
    )
    switch_result = await _switch()
    await asyncio.sleep(2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert switch_result["ok"]
    await fleet.unregister_robot("kachaka")
