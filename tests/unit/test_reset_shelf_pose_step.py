"""IT-13 — the patrol's opening reset_shelf_pose step is advisory.

The shelf is parked at home before every run, so resetting the robot's pose
estimate is a cheap correction, not a precondition. If the reset itself errors
out the patrol must still run: it is listed in NON_CRITICAL_ACTIONS, and this
pins that the task neither fails nor stops at step 0.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from common_types import (
    StepAction, StepStatus, Task, TaskStatus, TaskStep,
)
from services import task_runtime


def _fleet() -> MagicMock:
    fleet = MagicMock()
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={
        "poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0,
    })
    fleet.reset_metrics = AsyncMock(return_value=None)
    return fleet


def _task() -> Task:
    return Task(
        task_id="t-reset",
        robot_id="kachaka",
        steps=[
            TaskStep(
                step_id="reset_shelf",
                action=StepAction.RESET_SHELF_POSE.value,
                params={"shelf_id": "S_04"},
                status=StepStatus.PENDING,
            ),
            TaskStep(
                step_id="s-1",
                action=StepAction.WAIT.value,
                params={"seconds": "0"},
                status=StepStatus.PENDING,
            ),
        ],
        status=TaskStatus.QUEUED,
    )


def test_failed_reset_does_not_fail_the_task():
    fleet = _fleet()
    fleet.reset_shelf_pose = AsyncMock(return_value={"ok": False, "error": "TIMEOUT"})
    engine = task_runtime.TaskEngine(fleet, "kachaka")
    task = _task()

    with patch.object(task_runtime.dispatcher, "dispatch", new=AsyncMock(return_value=None)):
        result = asyncio.run(engine.run_task(task))

    assert result.status == TaskStatus.DONE
    assert task.steps[0].status == StepStatus.FAIL
    # The rest of the patrol still ran.
    assert task.steps[1].status == StepStatus.SUCCESS


def test_successful_reset_runs_before_the_rest_of_the_patrol():
    fleet = _fleet()
    fleet.reset_shelf_pose = AsyncMock(return_value={"ok": True})
    engine = task_runtime.TaskEngine(fleet, "kachaka")
    task = _task()

    with patch.object(task_runtime.dispatcher, "dispatch", new=AsyncMock(return_value=None)):
        result = asyncio.run(engine.run_task(task))

    fleet.reset_shelf_pose.assert_awaited_once_with("kachaka", "S_04")
    assert result.status == TaskStatus.DONE
    assert [s.status for s in task.steps] == [StepStatus.SUCCESS, StepStatus.SUCCESS]
