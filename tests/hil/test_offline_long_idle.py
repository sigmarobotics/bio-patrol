"""HIL: WiFi outage > debounce while idle → 1 INFO + 1 RECOVERED."""
import asyncio
import os

import pytest

from services.fleet_api import FleetAPI
from services.notifications import dispatcher
from services.notifications.events import AnomalyEvent, Severity, Source

ROBOT_IP = os.environ.get("HIL_ROBOT_IP", "192.168.50.133")


@pytest.mark.hil
@pytest.mark.asyncio
async def test_long_idle_emits_info_then_recovered():
    captured: list[AnomalyEvent] = []

    async def _capture(evt: AnomalyEvent) -> None:
        captured.append(evt)

    dispatcher.dispatch = _capture

    fleet = FleetAPI()
    res = await fleet.register_robot("kachaka", ROBOT_IP)
    assert res["ok"]

    slot = fleet._robots["kachaka"]
    slot.debouncer.set_debounce_provider(lambda: 20.0)

    print("\n>>> Disconnect WiFi from the robot for at least 30s, then restore. <<<")
    await asyncio.sleep(60)

    await fleet.unregister_robot("kachaka")

    offline = [e for e in captured if e.source == Source.ROBOT_OFFLINE]
    recovered = [e for e in captured if e.source == Source.ROBOT_RECOVERED]
    assert len(offline) == 1, f"expected 1 OFFLINE, got {len(offline)}"
    assert offline[0].severity == Severity.INFO
    # Recovery mirrors the OFFLINE severity (idle → INFO).
    assert len(recovered) == 1
    assert recovered[0].severity == Severity.INFO
