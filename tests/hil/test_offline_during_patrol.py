"""HIL: long disconnect while a patrol is active → 1 CRITICAL + 1 CRITICAL recovered."""
import asyncio
import os

import pytest

from services.fleet_api import FleetAPI
from services.notifications import dispatcher
from services.notifications.events import AnomalyEvent, Severity, Source
from services.task_runtime import current_tasks

ROBOT_IP = os.environ.get("HIL_ROBOT_IP", "192.168.50.133")


@pytest.mark.hil
@pytest.mark.asyncio
async def test_long_disconnect_during_patrol_is_critical():
    captured: list[AnomalyEvent] = []

    async def _capture(evt):
        captured.append(evt)

    dispatcher.dispatch = _capture

    fleet = FleetAPI()
    res = await fleet.register_robot("kachaka", ROBOT_IP)
    assert res["ok"]

    slot = fleet._robots["kachaka"]
    slot.debouncer.set_debounce_provider(lambda: 20.0)

    # Simulate a running patrol — the predicate is just current_tasks lookup.
    current_tasks["kachaka"] = "fake-task-id-it10"
    try:
        print("\n>>> Disconnect WiFi for at least 30s while patrol is 'running', then restore. <<<")
        await asyncio.sleep(60)
    finally:
        current_tasks.pop("kachaka", None)

    await fleet.unregister_robot("kachaka")

    offline = [e for e in captured if e.source == Source.ROBOT_OFFLINE]
    assert len(offline) == 1
    assert offline[0].severity == Severity.CRITICAL
    recovered = [e for e in captured if e.source == Source.ROBOT_RECOVERED]
    assert len(recovered) == 1
    # Recovery severity mirrors the OFFLINE severity (patrol → CRITICAL).
    assert recovered[0].severity == Severity.CRITICAL
