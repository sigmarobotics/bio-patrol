"""IT-17 — 板橋榮家 2026-08-19: the battery drained to 0.89% mid-patrol and the
firmware put the shelf down in the corridor before driving itself to the
charger. The 30% start gate cannot catch a run that starts just above it, so
the state watcher now aborts the run while there is still charge to carry the
shelf home. These tests pin when the abort fires and, just as importantly,
when it must not.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import settings.config as settings_config
from common_types import StepAction, StepStatus, Task, TaskStatus, TaskStep
from services import task_runtime
from services.notifications import Severity, Source
from services.task_runtime import current_tasks, tasks_db


@pytest.fixture(autouse=True)
def _clean_state():
    tasks_db.clear()
    current_tasks.clear()
    yield
    tasks_db.clear()
    current_tasks.clear()


@pytest.fixture
def dispatched(monkeypatch):
    events = []

    async def _capture(event):
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)
    return events


@pytest.fixture(autouse=True)
def _threshold(monkeypatch):
    """Default threshold; individual tests override via monkeypatch."""
    monkeypatch.setattr(settings_config, "get_runtime_settings",
                        lambda: {"patrol_abort_battery_pct": 10})


def _engine(battery, *, cancel_ok=True) -> task_runtime.TaskEngine:
    fleet = MagicMock()
    fleet.get_battery_info = AsyncMock(**battery)
    fleet.cancel_command = AsyncMock(return_value={"ok": cancel_ok})
    return task_runtime.TaskEngine(fleet, "kachaka")


def _patrol_task(mode: str | None = None) -> Task:
    metadata = {"mode": mode} if mode else {}
    task = Task(
        task_id="t-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        metadata=metadata,
        steps=[
            TaskStep(step_id="s1", action=StepAction.BIO_SCAN.value,
                     params={"bed_key": "B_101-1"}, status=StepStatus.SUCCESS),
            TaskStep(step_id="s2", action=StepAction.BIO_SCAN.value,
                     params={"bed_key": "B_101-2"}, status=StepStatus.PENDING),
        ],
    )
    tasks_db[task.task_id] = task
    current_tasks["kachaka"] = task.task_id
    return task


def _reading(pct, *, ok=True, power_status="3") -> dict:
    return {"return_value": {"ok": ok, "percentage": pct,
                             "power_status": power_status}}


def test_below_threshold_cancels_and_notifies(dispatched):
    task = _patrol_task()
    eng = _engine(_reading(8))

    assert asyncio.run(eng._maybe_abort_low_battery()) is True
    assert task.status == TaskStatus.CANCELLED
    assert task.metadata["battery_abort"] is True
    assert task.metadata["battery_abort_pct"] == 8
    eng.fleet.cancel_command.assert_awaited_once_with("kachaka")

    assert len(dispatched) == 1
    event = dispatched[0]
    assert event.source == Source.LOW_BATTERY_ABORT
    assert event.severity == Severity.WARN
    assert event.task_id == "t-1"
    assert "已量測 1/2 床" in event.body


def test_above_threshold_leaves_run_alone(dispatched):
    task = _patrol_task()
    eng = _engine(_reading(55))

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert task.status == TaskStatus.IN_PROGRESS
    assert dispatched == []
    eng.fleet.cancel_command.assert_not_awaited()


def test_exactly_at_threshold_aborts(dispatched):
    task = _patrol_task()
    eng = _engine(_reading(10))

    assert asyncio.run(eng._maybe_abort_low_battery()) is True
    assert task.status == TaskStatus.CANCELLED


def test_charging_robot_is_left_to_the_firmware(dispatched):
    task = _patrol_task()
    eng = _engine(_reading(5, power_status="1"))

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert task.status == TaskStatus.IN_PROGRESS
    assert dispatched == []


def test_cleanup_task_never_cancels_itself(dispatched):
    task = _patrol_task(mode="cleanup")
    eng = _engine(_reading(3))

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert task.status == TaskStatus.IN_PROGRESS
    assert dispatched == []


def test_unreadable_battery_does_not_abort(dispatched):
    task = _patrol_task()
    eng = _engine({"side_effect": RuntimeError("UNAVAILABLE")})

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert task.status == TaskStatus.IN_PROGRESS
    assert dispatched == []


def test_threshold_zero_disables_the_abort(monkeypatch, dispatched):
    monkeypatch.setattr(settings_config, "get_runtime_settings",
                        lambda: {"patrol_abort_battery_pct": 0})
    task = _patrol_task()
    eng = _engine(_reading(2))

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert task.status == TaskStatus.IN_PROGRESS
    assert dispatched == []


def test_abort_fires_only_once(dispatched):
    _patrol_task()
    eng = _engine(_reading(8))

    assert asyncio.run(eng._maybe_abort_low_battery()) is True
    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert len(dispatched) == 1
    assert eng.fleet.cancel_command.await_count == 1


def test_run_finishing_during_battery_read_is_not_cancelled(dispatched):
    """The battery read yields; if the final step completes in that window the
    task is DONE and must not be flipped to CANCELLED (spurious cleanup +
    「已取消」notice for a fully completed patrol)."""
    task = _patrol_task()
    eng = _engine(_reading(8))

    async def _finishes_meanwhile(robot_id):
        task.status = TaskStatus.DONE
        return {"ok": True, "percentage": 8, "power_status": "3"}

    eng.fleet.get_battery_info = AsyncMock(side_effect=_finishes_meanwhile)

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert task.status == TaskStatus.DONE
    assert dispatched == []
    eng.fleet.cancel_command.assert_not_awaited()


def test_disabled_threshold_skips_the_battery_rpc(monkeypatch, dispatched):
    """threshold 0 must not keep polling the robot for a feature that is off."""
    monkeypatch.setattr(settings_config, "get_runtime_settings",
                        lambda: {"patrol_abort_battery_pct": 0})
    _patrol_task()
    eng = _engine(_reading(2))

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    eng.fleet.get_battery_info.assert_not_awaited()


def test_dispatch_failure_still_cancels(monkeypatch):
    monkeypatch.setattr(task_runtime.dispatcher, "dispatch",
                        AsyncMock(side_effect=RuntimeError("sink down")))
    task = _patrol_task()
    eng = _engine(_reading(4))

    assert asyncio.run(eng._maybe_abort_low_battery()) is True
    assert task.status == TaskStatus.CANCELLED
    eng.fleet.cancel_command.assert_awaited_once_with("kachaka")
