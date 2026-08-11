"""TODO-017: a low-battery auto-recharge must not raise a CRITICAL drop alarm.

The robot puts its shelf down and drives to the charger by itself when the
battery runs out, so ``get_moving_shelf_id`` goes empty exactly like a drop.
The classifier separates the two from battery + command state, and the drop
handler downgrades the notification without changing task handling.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from common_types import Task, TaskStatus, TaskStep, StepAction
from services import task_runtime
from services.notifications import Severity
from services.task_runtime import classify_shelf_release


CHARGING = {"ok": True, "percentage": 8.0, "power_status": "1"}
DISCHARGING_FULL = {"ok": True, "percentage": 74.0, "power_status": "2"}
IDLE_CMD = {"ok": True, "state": "1", "command": None, "is_running": False}
RETURN_HOME_CMD = {"ok": True, "state": "2", "command": "return_home_command {\n}\n",
                   "is_running": True}


# ── classifier ───────────────────────────────────────────────────────────────

def test_low_battery_while_charging_is_a_recharge():
    assert classify_shelf_release(CHARGING, IDLE_CMD) == "low_battery_return"


def test_low_battery_while_returning_home_is_a_recharge():
    battery = {"ok": True, "percentage": 11.0, "power_status": "2"}
    assert classify_shelf_release(battery, RETURN_HOME_CMD) == "low_battery_return"


def test_healthy_battery_is_a_drop():
    assert classify_shelf_release(DISCHARGING_FULL, IDLE_CMD) == "drop"


def test_healthy_battery_returning_home_is_still_a_drop():
    assert classify_shelf_release(DISCHARGING_FULL, RETURN_HOME_CMD) == "drop"


def test_low_battery_without_recharge_signal_is_a_drop():
    battery = {"ok": True, "percentage": 9.0, "power_status": "2"}
    assert classify_shelf_release(battery, IDLE_CMD) == "drop"


def test_unreadable_battery_is_a_drop():
    assert classify_shelf_release({"ok": False, "error": "UNAVAILABLE"}, IDLE_CMD) == "drop"
    assert classify_shelf_release({}, {}) == "drop"
    assert classify_shelf_release({"ok": True, "percentage": None}, IDLE_CMD) == "drop"


# ── drop handler wiring ──────────────────────────────────────────────────────

def _engine(battery, command_state):
    fleet = MagicMock()
    fleet.get_battery_info = AsyncMock(return_value=battery)
    fleet.get_command_state = AsyncMock(return_value=command_state)
    fleet.cancel_command = AsyncMock(return_value={"ok": True})
    fleet.return_home = AsyncMock(return_value={"ok": True})
    fleet.get_raw_client = MagicMock(side_effect=RuntimeError("no robot"))
    eng = task_runtime.TaskEngine(fleet, "kachaka")
    eng.current_task_id = "t-1"
    return eng


def _task():
    return Task(
        task_id="t-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[TaskStep(step_id="s1", action=StepAction.BIO_SCAN,
                        params={"bed_key": "B_101-1"})],
    )


def _run_drop(monkeypatch, battery, command_state):
    events = []

    async def _capture(event):
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)
    monkeypatch.setattr(task_runtime.TaskEngine, "_record_skipped_scan",
                        lambda self, *a, **kw: None)
    eng = _engine(battery, command_state)
    task = _task()
    asyncio.run(eng._handle_shelf_drop(task, 0))
    assert len(events) == 1
    return task, events[0]


def test_low_battery_release_downgrades_to_warn(monkeypatch):
    task, event = _run_drop(monkeypatch, CHARGING, IDLE_CMD)

    assert event.severity == Severity.WARN
    assert "貨架掉落" not in event.title
    assert "8%" in event.body            # message names the battery level
    assert task.metadata["low_battery"] is True
    assert task.metadata["battery_pct"] == 8.0
    # Task handling is unchanged — resume still finds it.
    assert task.status == TaskStatus.SHELF_DROPPED
    assert task.metadata["shelf_drop"] is True


def test_real_drop_still_raises_critical(monkeypatch):
    task, event = _run_drop(monkeypatch, DISCHARGING_FULL, IDLE_CMD)

    assert event.severity == Severity.CRITICAL
    assert "貨架掉落" in event.title
    assert task.metadata["low_battery"] is False
    assert task.status == TaskStatus.SHELF_DROPPED


def test_classification_reads_robot_before_cancelling(monkeypatch):
    """cancel_command wipes the return-home evidence, so the reads come first."""
    calls = []
    fleet = MagicMock()

    async def _battery(robot_id):
        calls.append("battery")
        return CHARGING

    async def _cmd_state(robot_id):
        calls.append("command_state")
        return RETURN_HOME_CMD

    async def _cancel(robot_id):
        calls.append("cancel")
        return {"ok": True}

    fleet.get_battery_info = _battery
    fleet.get_command_state = _cmd_state
    fleet.cancel_command = _cancel
    fleet.return_home = AsyncMock(return_value={"ok": True})
    fleet.get_raw_client = MagicMock(side_effect=RuntimeError("no robot"))

    async def _noop(event):
        return None

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _noop)
    monkeypatch.setattr(task_runtime.TaskEngine, "_record_skipped_scan",
                        lambda self, *a, **kw: None)
    eng = task_runtime.TaskEngine(fleet, "kachaka")
    eng.current_task_id = "t-1"
    asyncio.run(eng._handle_shelf_drop(_task(), 0))

    assert calls.index("battery") < calls.index("cancel")
    assert calls.index("command_state") < calls.index("cancel")


# ── shelf pose (raw SDK) ─────────────────────────────────────────────────────

def _shelf_stub(shelf_id, *, has_pose, x=0.0, y=0.0, theta=0.0):
    shelf = MagicMock()
    shelf.id = shelf_id
    shelf.name = shelf_id
    shelf.HasField = MagicMock(return_value=has_pose)
    shelf.pose.x, shelf.pose.y, shelf.pose.theta = x, y, theta
    return shelf


def test_query_shelf_pose_reads_raw_sdk_pose():
    """kachaka_core's list_shelves() drops the pose field — pb2.Shelf has it."""
    client = MagicMock()
    client.get_shelves = MagicMock(
        return_value=[_shelf_stub("S_04", has_pose=True, x=3.5, y=-1.25, theta=0.5)]
    )
    fleet = MagicMock()
    fleet.get_raw_client = MagicMock(return_value=client)
    eng = task_runtime.TaskEngine(fleet, "kachaka")

    pose = asyncio.run(eng._query_shelf_pose("S_04"))

    assert pose == {"x": 3.5, "y": -1.25, "theta": 0.5}


def test_query_shelf_pose_returns_none_when_unset():
    client = MagicMock()
    client.get_shelves = MagicMock(return_value=[_shelf_stub("S_04", has_pose=False)])
    fleet = MagicMock()
    fleet.get_raw_client = MagicMock(return_value=client)
    eng = task_runtime.TaskEngine(fleet, "kachaka")

    assert asyncio.run(eng._query_shelf_pose("S_04")) is None


def test_failed_return_shelf_without_pose_is_not_a_drop(monkeypatch):
    """The (0,0,0) fallback used to make every failed return_shelf a drop."""
    fleet = MagicMock()
    fleet.get_slot_or_none = MagicMock(return_value=None)
    fleet.get_shelves = AsyncMock(return_value={
        "ok": True, "shelves": [{"id": "S_04", "name": "S_04", "home_location_id": "L_home"}],
    })
    fleet.get_locations = AsyncMock(return_value={
        "ok": True,
        "locations": [{"id": "L_home", "name": "home", "pose": {"x": 8.0, "y": 4.0, "theta": 0.0}}],
    })
    eng = task_runtime.TaskEngine(fleet, "kachaka")

    async def _no_pose(shelf_id):
        return None

    monkeypatch.setattr(eng, "_query_shelf_pose", _no_pose)

    assert asyncio.run(eng._shelf_dropped_en_route("S_04")) is False


def test_failed_return_shelf_far_from_home_is_a_drop(monkeypatch):
    fleet = MagicMock()
    fleet.get_slot_or_none = MagicMock(return_value=None)
    fleet.get_shelves = AsyncMock(return_value={
        "ok": True, "shelves": [{"id": "S_04", "name": "S_04", "home_location_id": "L_home"}],
    })
    fleet.get_locations = AsyncMock(return_value={
        "ok": True,
        "locations": [{"id": "L_home", "name": "home", "pose": {"x": 8.0, "y": 4.0, "theta": 0.0}}],
    })
    eng = task_runtime.TaskEngine(fleet, "kachaka")

    async def _far_pose(shelf_id):
        return {"x": 2.0, "y": 4.0, "theta": 0.0}

    monkeypatch.setattr(eng, "_query_shelf_pose", _far_pose)

    assert asyncio.run(eng._shelf_dropped_en_route("S_04")) is True


# ── interruptible bio_scan wait ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bio_scan_wait_is_interrupted_by_shelf_drop(monkeypatch):
    """A drop during the (minutes-long) scan window must not wait it out."""
    started = asyncio.Event()

    class _SlowClient:
        async def get_valid_scan_data(self, target_bed=None, task_id=None, bed_name=None):
            started.set()
            await asyncio.sleep(30)
            raise AssertionError("scan should have been cancelled")

    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: _SlowClient())

    eng = task_runtime.TaskEngine(MagicMock(), "kachaka")
    eng.current_task_id = "t-1"
    eng.target_bed = "L_101-1"
    eng.shelf_drop_event = asyncio.Event()
    step = TaskStep(step_id="s1", action=StepAction.BIO_SCAN, params={"bed_key": "B_101-1"})

    async def _trip():
        await started.wait()
        eng.shelf_drop_event.set()

    asyncio.create_task(_trip())
    result = await asyncio.wait_for(eng._do_bio_scan(step), timeout=5.0)

    assert result.success is False
    assert "interrupted" in result.error_message


@pytest.mark.asyncio
async def test_bio_scan_returns_outcome_when_no_drop(monkeypatch):
    outcome = MagicMock()
    outcome.valid_record = {"bpm": 60, "rpm": 15}
    outcome.retry_count = 0

    class _Client:
        async def get_valid_scan_data(self, target_bed=None, task_id=None, bed_name=None):
            return outcome

    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: _Client())
    monkeypatch.setattr(task_runtime._bio_scan_evaluator, "evaluate", lambda o: None)

    eng = task_runtime.TaskEngine(MagicMock(), "kachaka")
    eng.current_task_id = "t-1"
    eng.target_bed = "L_101-1"
    eng.shelf_drop_event = asyncio.Event()
    step = TaskStep(step_id="s1", action=StepAction.BIO_SCAN, params={"bed_key": "B_101-1"})

    result = await eng._do_bio_scan(step)

    assert result.success is True
    assert result.data == {"bpm": 60, "rpm": 15}
