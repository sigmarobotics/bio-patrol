"""BIZ-019 F-26: a cleanup run (mode=cleanup) measures nothing, so it no
longer sends the ward its own 「✅ 巡房完成 / 本次巡房 0 床」. It speaks up only
when it did not get the robot home — once, and not on top of the 11501
「請協助將貨架歸位」 alert that already asked for a person.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from common_types import StepAction, StepStatus, Task, TaskStatus, TaskStep
from services import task_runtime
from services.notifications import Severity, Source
from services.task_runtime import current_tasks, tasks_db

INCOMPLETE = "⚠️ 收工未完成，請確認貨架與機器人位置"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    tasks_db.clear()
    current_tasks.clear()
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: None)
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


def _cleanup(return_home):
    fleet = MagicMock()
    fleet.get_metrics = AsyncMock(return_value={
        "poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0,
    })
    fleet.reset_metrics = AsyncMock(return_value=None)
    fleet.return_home = AsyncMock(side_effect=return_home)
    task = Task(task_id="c-1", robot_id="kachaka", status=TaskStatus.QUEUED,
                metadata={"mode": "cleanup"},
                steps=[TaskStep(step_id="cleanup_return_home",
                                action=StepAction.RETURN_HOME.value, params={},
                                status=StepStatus.PENDING)])
    tasks_db[task.task_id] = task
    asyncio.run(task_runtime.TaskEngine(fleet, "kachaka").run_task(task))
    return task


def test_done_cleanup_dispatches_nothing(dispatched):
    task = _cleanup(lambda robot_id: {"ok": True})

    assert task.status == TaskStatus.DONE
    assert dispatched == []


def test_failed_cleanup_sends_one_incomplete_warning(dispatched):
    task = _cleanup(lambda robot_id: {"ok": False, "error_code": 10001, "error": "blocked"})

    assert task.status == TaskStatus.FAILED
    assert [(e.severity, e.source, e.title) for e in dispatched] == [
        (Severity.WARN, Source.SHELF_DROP, INCOMPLETE)]


def test_cancelled_cleanup_sends_one_incomplete_warning(dispatched):
    def _cancelled_on_the_way(robot_id):
        tasks_db["c-1"].status = TaskStatus.CANCELLED
        return {"ok": False, "error": "cancelled"}

    task = _cleanup(_cancelled_on_the_way)

    assert task.status == TaskStatus.CANCELLED
    assert [e.title for e in dispatched] == [INCOMPLETE]


def test_refused_charge_alert_is_not_doubled(dispatched):
    task = _cleanup(lambda robot_id: {"ok": False, "error_code": 11501,
                                      "error": "furniture docked"})

    assert task.status == TaskStatus.FAILED
    assert [(e.severity, e.title) for e in dispatched] == [
        (Severity.CRITICAL, "⚠️ 貨架歸位失敗，仍在機器人身上")]
