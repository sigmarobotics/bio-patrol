"""CORNER-018 — Dispatcher failure must not propagate into task_runtime.

If dispatcher.dispatch raises (e.g. internal bug, queue full), the patrol
flow must not crash. shelf-drop and task-summary call sites both need to
swallow the exception and keep the patrol moving / completing.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from common_types import Task, TaskStatus, TaskStep, StepStatus
from services import task_runtime


def _trigger_step():
    return TaskStep(
        step_id="s-1",
        action="move_shelf",
        params={"location_id": "101-1", "shelf_id": "S_04"},
    )


def test_shelf_drop_handler_swallows_dispatcher_exception():
    """_handle_shelf_drop must not propagate dispatcher.dispatch failures —
    a broken sink/dispatcher cannot block returning the robot home."""
    fleet = MagicMock()
    fleet.cancel_command = AsyncMock(return_value=None)
    fleet.return_home = AsyncMock(return_value=None)
    engine = task_runtime.TaskEngine(fleet, "kachaka")

    task = Task(task_id="t-1", robot_id="kachaka", steps=[], status=TaskStatus.IN_PROGRESS)

    async def boom(_event):
        raise RuntimeError("dispatcher exploded")

    with patch.object(task_runtime.dispatcher, "dispatch", side_effect=boom), \
         patch.object(engine, "_query_shelf_pose", new=AsyncMock(return_value={})), \
         patch.object(engine, "_collect_remaining_beds", return_value=[]):
        # Must complete without raising — operator-critical: robot still goes home.
        asyncio.run(engine._handle_shelf_drop(task, step_index=0, trigger_step=_trigger_step()))

    assert task.status == TaskStatus.SHELF_DROPPED
    fleet.return_home.assert_awaited_once()


def test_run_task_summary_dispatch_failure_does_not_change_task_status():
    """The task-summary dispatch sits in run_task's `finally` block. A raise
    there would mask the patrol's real exit reason — the call site already
    has try/except, this test pins that contract."""
    fleet = MagicMock()
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={
        "poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0,
    })
    fleet.reset_metrics = AsyncMock(return_value=None)
    engine = task_runtime.TaskEngine(fleet, "kachaka")

    task = Task(
        task_id="t-2",
        robot_id="kachaka",
        steps=[TaskStep(step_id="s-1", action="wait", params={"seconds": "0"}, status=StepStatus.PENDING)],
        status=TaskStatus.QUEUED,
    )

    async def boom(_event):
        raise RuntimeError("dispatcher exploded")

    with patch.object(task_runtime.dispatcher, "dispatch", side_effect=boom):
        result = asyncio.run(engine.run_task(task))

    # Patrol completed normally despite dispatcher failure on the final summary.
    assert result.status == TaskStatus.DONE
