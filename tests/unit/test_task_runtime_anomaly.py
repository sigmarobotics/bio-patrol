"""Unit tests for AnomalyEvent dispatch sites in task_runtime."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from services.notifications.events import Severity, Source


def _captured_events():
    captured = []

    async def _fake_dispatch(event):
        captured.append(event)

    return captured, _fake_dispatch


def test_shelf_drop_dispatches_critical_event():
    from services import task_runtime
    from common_types import Task, TaskStep, TaskStatus

    captured, fake_dispatch = _captured_events()

    fleet = MagicMock()
    fleet.cancel_command = AsyncMock(return_value=None)
    fleet.return_home = AsyncMock(return_value=None)
    engine = task_runtime.TaskEngine(fleet, "kachaka")

    task = Task(
        task_id="t-1", robot_id="kachaka", steps=[], status=TaskStatus.IN_PROGRESS
    )

    trigger_step = TaskStep(
        step_id="s-1",
        action="move_shelf",
        params={"location_id": "101-1", "shelf_id": "S_04"},
    )

    with patch.object(task_runtime.dispatcher, "dispatch", side_effect=fake_dispatch), \
         patch.object(engine, "_query_shelf_pose", new=AsyncMock(return_value={})), \
         patch.object(engine, "_collect_remaining_beds", return_value=["101-2", "101-3"]):
        asyncio.run(engine._handle_shelf_drop(task, step_index=0, trigger_step=trigger_step))

    assert len(captured) == 1
    event = captured[0]
    assert event.severity == Severity.CRITICAL
    assert event.source == Source.SHELF_DROP
    assert event.bed_key == "101-1"
    assert event.task_id == "t-1"
    assert "貨架掉落" in event.title
    assert "掉落位置：101-1" in event.body
    assert "剩餘 2 床" in event.body
    assert "返回充電站" in event.body
    assert event.raw["shelf_id"] == "S_04"
    assert event.raw["remaining_beds"] == ["101-2", "101-3"]


# ── return_shelf release vs drop (2026-07-24 false-alarm regression) ──

def _engine_with_event():
    from services import task_runtime
    fleet = MagicMock()
    engine = task_runtime.TaskEngine(fleet, "kachaka")
    engine.shelf_drop_event = asyncio.Event()
    return engine, fleet


def test_successful_return_shelf_is_not_a_drop():
    from common_types import TaskStep
    engine, fleet = _engine_with_event()
    fleet.return_shelf = AsyncMock(return_value={"ok": True})
    step = TaskStep(step_id="r-1", action="return_shelf", params={"shelf_id": "S01"})
    asyncio.run(engine._do_return_shelf(step))
    assert engine._shelf_release_expected is True
    assert not engine.shelf_drop_event.is_set()


def test_failed_return_with_shelf_at_home_is_not_a_drop():
    from common_types import TaskStep
    engine, fleet = _engine_with_event()
    fleet.return_shelf = AsyncMock(return_value={"ok": False, "error": "TIMEOUT"})
    fleet.get_slot_or_none = MagicMock(return_value=None)
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": [
        {"id": "S01", "home_location_id": "S01_home", "pose": {"x": -0.10, "y": 3.28}}
    ]})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": [
        {"id": "S01_home", "pose": {"x": -0.04, "y": 3.25}}
    ]})
    step = TaskStep(step_id="r-1", action="return_shelf", params={"shelf_id": "S01"})
    asyncio.run(engine._do_return_shelf(step))
    assert not engine.shelf_drop_event.is_set()


def test_failed_return_with_shelf_en_route_is_a_drop():
    from common_types import TaskStep
    engine, fleet = _engine_with_event()
    fleet.return_shelf = AsyncMock(return_value={"ok": False, "error": "TIMEOUT"})
    fleet.get_slot_or_none = MagicMock(return_value=None)
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": [
        {"id": "S01", "home_location_id": "S01_home", "pose": {"x": -15.0, "y": 6.0}}
    ]})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": [
        {"id": "S01_home", "pose": {"x": -0.04, "y": 3.25}}
    ]})
    step = TaskStep(step_id="r-1", action="return_shelf", params={"shelf_id": "S01"})
    asyncio.run(engine._do_return_shelf(step))
    assert engine.shelf_drop_event.is_set()
    assert engine._shelf_release_expected is False
