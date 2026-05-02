"""HIL: WiFi blip < debounce should produce zero notifications."""
import asyncio
import os

import pytest

from services.fleet_api import FleetAPI
from services.notifications import dispatcher
from services.notifications.events import AnomalyEvent

ROBOT_IP = os.environ.get("HIL_ROBOT_IP", "192.168.50.133")


@pytest.mark.hil
@pytest.mark.asyncio
async def test_short_idle_blip_silent():
    captured: list[AnomalyEvent] = []

    async def _capture(evt: AnomalyEvent) -> None:
        captured.append(evt)

    dispatcher.dispatch = _capture  # bypass real sinks

    fleet = FleetAPI()
    res = await fleet.register_robot("kachaka", ROBOT_IP)
    assert res["ok"]

    # Shorten debounce via the public hook so settings live-edits aren't required.
    slot = fleet._robots["kachaka"]
    slot.debouncer.set_debounce_provider(lambda: 30.0)

    print("\n>>> Disconnect WiFi from the robot for ~10s, then restore. <<<")
    await asyncio.sleep(40)

    await fleet.unregister_robot("kachaka")

    offline_events = [e for e in captured if e.source.value == "robot_offline"]
    assert offline_events == []
