"""BIZ-019 F-03: a patrol whose final return_shelf fails while the robot still
carries the shelf used to end DONE with 「✅ 巡房完成」, no alert and no cleanup —
the robot sat in the corridor unable to charge (the 板橋 2026-10-19 end state).
Now the run ends FAILED, warns once, keeps the readings in a 「貨架未歸位」
summary and queues the cleanup that parks the named shelf. A shelf that turns
out released after the placement settles keeps today's ending.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from common_types import StepAction, StepStatus, Task, TaskStatus, TaskStep
from services import task_runtime
from services.notifications import Severity, Source
from services.task_runtime import current_tasks, tasks_db


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    tasks_db.clear()
    current_tasks.clear()
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: None)
    monkeypatch.setattr(task_runtime, "RETURN_SHELF_SETTLE_INTERVAL", 0)
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


@pytest.fixture
def queued(monkeypatch):
    calls = []

    async def _submit(robot_id, metadata_extra=None, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(task_runtime, "submit_cleanup_task", _submit)
    return calls


def _fleet(*, moving_shelf, running=(False,)):
    fleet = MagicMock()
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={
        "poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0,
    })
    fleet.reset_metrics = AsyncMock(return_value=None)
    fleet.return_shelf = AsyncMock(return_value={
        "ok": False, "error_code": 19004, "error": "destination blocked"})
    fleet.get_command_state = AsyncMock(side_effect=[
        {"ok": True, "is_running": r} for r in running])
    fleet.get_moving_shelf = AsyncMock(return_value={"ok": True, "shelf_id": moving_shelf})
    # _shelf_dropped_en_route asks the slot first: the robot still reports a
    # moving shelf right after the failure, so no drop is raised.
    slot = MagicMock()
    slot.conn.client.get_moving_shelf_id = MagicMock(return_value="S04")
    fleet.get_slot_or_none = MagicMock(return_value=slot)
    fleet._robots = {}  # no 1 Hz state watcher in these runs
    return fleet


def _run(fleet, mode="patrol"):
    task = Task(task_id="20261028220000-ret001", robot_id="kachaka",
                status=TaskStatus.QUEUED, metadata={"mode": mode},
                steps=[TaskStep(step_id="action_0", action=StepAction.WAIT.value,
                                params={"seconds": "0"}, status=StepStatus.PENDING),
                       TaskStep(step_id="return_1", action=StepAction.RETURN_SHELF.value,
                                params={"shelf_id": "S04"}, status=StepStatus.PENDING)])
    tasks_db[task.task_id] = task
    asyncio.run(task_runtime.TaskEngine(fleet, "kachaka").run_task(task))
    return task


def test_latched_shelf_fails_the_run_warns_and_parks_it(dispatched, queued):
    task = _run(_fleet(moving_shelf="S04"))

    assert task.status == TaskStatus.FAILED
    assert task.metadata["cleanup_shelf_id"] == "S04"
    assert task.metadata["return_shelf_failed"] is True
    assert [(e.severity, e.source) for e in dispatched] == [
        (Severity.WARN, Source.SHELF_DROP), (Severity.INFO, Source.TASK_SUMMARY)]
    assert dispatched[0].title == "⚠️ 貨架歸位失敗，仍在機器人身上"
    summaries = [e for e in dispatched if e.source == Source.TASK_SUMMARY]
    assert len(summaries) == 1
    assert summaries[0].title == "⚠️ 巡房完成，貨架未歸位"
    assert queued == [{"force_shelf_id": "S04"}]


def test_waits_out_a_running_placement_before_reading(dispatched, queued):
    fleet = _fleet(moving_shelf="S04", running=(True, True, False))
    task = _run(fleet)

    assert fleet.get_command_state.await_count == 3
    fleet.get_moving_shelf.assert_awaited_once()
    fleet.return_shelf.assert_awaited_once()  # never retried
    assert task.status == TaskStatus.FAILED


def test_released_after_the_wait_keeps_done(dispatched, queued):
    task = _run(_fleet(moving_shelf=None, running=(True, False)))

    assert task.status == TaskStatus.DONE
    assert "return_shelf_failed" not in task.metadata
    assert [e.source for e in dispatched] == [Source.TASK_SUMMARY]
    assert dispatched[0].title == "✅ 巡房完成"
    assert queued == []


def test_demo_run_is_parked_too(dispatched, queued):
    task = _run(_fleet(moving_shelf="S04"), mode="demo")

    assert task.status == TaskStatus.FAILED
    assert queued == [{"force_shelf_id": "S04"}]


def test_link_lost_during_settle_is_unknown_not_stranded(dispatched, queued):
    """Connection loss inside the settle window = shelf state unknown
    (2026-08-10 lesson): no stranded WARN, no cleanup against an unreachable
    robot (code-review #56)."""
    fleet = _fleet(moving_shelf="S04")
    fleet.get_command_state = AsyncMock(side_effect=RuntimeError("UNAVAILABLE: failed to connect"))
    task = _run(fleet)

    assert task.metadata.get("return_shelf_failed") is None
    assert queued == []
    assert not any(e.title == "⚠️ 貨架歸位失敗，仍在機器人身上" for e in dispatched)


def test_moving_shelf_ok_false_connection_error_is_unknown(dispatched, queued):
    fleet = _fleet(moving_shelf="S04")
    fleet.get_moving_shelf = AsyncMock(return_value={"ok": False, "error": "UNAVAILABLE: timed out"})
    task = _run(fleet)

    assert task.metadata.get("return_shelf_failed") is None
    assert queued == []
