import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from common_types import Task, TaskStep, TaskStatus, StepAction, StepStatus


@pytest.mark.asyncio
async def test_engine_no_longer_starts_shelf_monitor_task():
    from services.task_runtime import TaskEngine

    fleet = MagicMock()
    fleet.get_shelves.return_value = {"ok": True, "shelves": []}
    fleet.get_locations.return_value = {"ok": True, "locations": []}
    eng = TaskEngine(fleet, "kachaka")
    assert not hasattr(eng, "_shelf_monitor_task") or eng._shelf_monitor_task is None
    assert not hasattr(eng, "_monitor_shelf")


@pytest.mark.asyncio
async def test_shelf_drop_event_triggers_handle_shelf_drop(monkeypatch):
    """Setting shelf_drop_event during a long step routes through _handle_shelf_drop."""
    fleet = MagicMock()
    fleet._robots = {}      # no slot — engine builds its own event
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.cancel_command = AsyncMock(return_value={"ok": True})
    fleet.return_home = AsyncMock(return_value={"ok": True})
    fleet.get_metrics = AsyncMock(return_value={"poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0})
    fleet.reset_metrics = AsyncMock()

    from services.task_runtime import TaskEngine
    eng = TaskEngine(fleet, "kachaka")
    handled: list[int] = []

    async def fake_handle(task, idx, trigger_step=None, error_code=0):
        handled.append(idx)
        task.status = TaskStatus.SHELF_DROPPED
        return True

    monkeypatch.setattr(eng, "_handle_shelf_drop", fake_handle)

    task = Task(
        task_id="t1", robot_id="kachaka", status=TaskStatus.QUEUED,
        steps=[TaskStep(step_id="s1", action=StepAction.WAIT, params={"seconds": "1.5"})],
    )

    async def _trip():
        await asyncio.sleep(0.2)
        eng.shelf_drop_event.set()

    asyncio.create_task(_trip())
    await eng.run_task(task)
    assert handled == [0]


@pytest.mark.asyncio
async def test_state_watcher_fires_on_shelf_id_transition(monkeypatch):
    """CORNER-034: drop while NOT in a move_shelf command — watcher must catch
    it via direct SDK read (controller's cached state is stale post-move_shelf)."""
    responses = ["S_04", "S_04", None, None]   # docked, docked, gone, gone

    fake_client = MagicMock()
    fake_client.get_moving_shelf_id = MagicMock(side_effect=responses)
    fake_conn = MagicMock(); fake_conn.client = fake_client

    slot = MagicMock()
    slot.conn = fake_conn
    slot.shelf_drop_event = asyncio.Event()

    fleet = MagicMock(); fleet._robots = {"kachaka": slot}
    # The watcher asks the robot before raising: two empty reads confirm it.
    fleet.get_raw_client = MagicMock(return_value=MagicMock(get_moving_shelf_id=MagicMock(return_value="")))
    from services import task_runtime
    monkeypatch.setattr(task_runtime, "SHELF_VERIFY_INTERVAL", 0.0)

    from services.task_runtime import TaskEngine
    eng = TaskEngine(fleet, "kachaka")
    eng.shelf_drop_event = slot.shelf_drop_event
    eng._state_watcher_stop = False

    watcher = asyncio.create_task(eng._watch_shelf_state())
    await asyncio.sleep(8.0)       # >= 3 polling cycles at 3s — covers docked->None transition
    eng._state_watcher_stop = True
    watcher.cancel()
    try:
        await watcher
    except asyncio.CancelledError:
        pass

    assert slot.shelf_drop_event.is_set()
    assert fake_client.get_moving_shelf_id.call_count >= 3


@pytest.mark.asyncio
async def test_false_release_reruns_the_interrupted_scan_once(monkeypatch):
    """A scan cut short by a release signal the robot then denies is re-run
    once rather than silently lost (板橋 2026-10-19 pattern, seen from the
    run loop)."""
    from services.task_runtime import TaskEngine, StepResult

    fleet = MagicMock()
    fleet._robots = {}
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={"poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0})
    fleet.reset_metrics = AsyncMock()
    eng = TaskEngine(fleet, "kachaka")
    calls: list[str] = []

    async def fake_execute(step):
        calls.append(step.step_id)
        if len(calls) == 1:
            eng.shelf_drop_event.set()
            return StepResult(success=False, error_code=-1,
                              error_message="Bio scan interrupted by shelf release",
                              data={"interrupted_by": "shelf_release"}, timestamp="t")
        return StepResult(success=True, error_code=0, error_message="",
                          data={}, timestamp="t")

    async def fake_handle(task, idx, trigger_step=None, error_code=0):
        eng.shelf_drop_event.clear()
        return False

    monkeypatch.setattr(eng, "_execute_step", fake_execute)
    monkeypatch.setattr(eng, "_handle_shelf_drop", fake_handle)

    task = Task(
        task_id="t2", robot_id="kachaka", status=TaskStatus.QUEUED,
        steps=[TaskStep(step_id="a0", action=StepAction.BIO_SCAN, params={"bed_key": "101-1"})],
    )
    await eng.run_task(task)

    assert calls == ["a0", "a0"]
    assert task.status == TaskStatus.DONE
    assert task.steps[0].status.value == "success"


@pytest.mark.asyncio
async def test_second_false_release_on_the_same_bed_records_the_skip(monkeypatch):
    """The re-run budget is one; a second cut scan is recorded as skipped so
    the bed does not vanish from the night's record."""
    from services.task_runtime import TaskEngine, StepResult

    fleet = MagicMock()
    fleet._robots = {}
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={"poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0})
    fleet.reset_metrics = AsyncMock()
    eng = TaskEngine(fleet, "kachaka")
    calls: list[str] = []
    skipped: list[str] = []

    async def fake_execute(step):
        calls.append(step.step_id)
        eng.shelf_drop_event.set()
        return StepResult(success=False, error_code=-1,
                          error_message="Bio scan interrupted by shelf release",
                          data={"interrupted_by": "shelf_release"}, timestamp="t")

    async def fake_handle(task, idx, trigger_step=None, error_code=0):
        eng.shelf_drop_event.clear()
        return False

    monkeypatch.setattr(eng, "_execute_step", fake_execute)
    monkeypatch.setattr(eng, "_handle_shelf_drop", fake_handle)
    monkeypatch.setattr(eng, "_record_skipped_scan",
                        lambda step, details, **kw: skipped.append(details))

    task = Task(
        task_id="t3", robot_id="kachaka", status=TaskStatus.QUEUED,
        steps=[TaskStep(step_id="a0", action=StepAction.BIO_SCAN, params={"bed_key": "101-1"})],
    )
    await eng.run_task(task)

    assert calls == ["a0", "a0"]
    assert len(skipped) == 1
    assert task.status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_a_skipped_scan_is_not_the_drop_handlers_current_bed(monkeypatch):
    """A scan whose move failed is handled by the skip branch; a release
    signal pending at that loop top is handled at the next real step."""
    from services.task_runtime import TaskEngine, StepResult

    fleet = MagicMock()
    fleet._robots = {}
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={"poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0})
    fleet.reset_metrics = AsyncMock()
    eng = TaskEngine(fleet, "kachaka")
    handled_at: list[int] = []

    async def fake_execute(step):
        if step.step_id == "m0":
            eng.shelf_drop_event.set()
            return StepResult(success=False, error_code=19004, error_message="blocked", data={}, timestamp="t")
        return StepResult(success=True, error_code=0, error_message="", data={}, timestamp="t")

    async def fake_handle(task, idx, trigger_step=None, error_code=0):
        handled_at.append(idx)
        eng.shelf_drop_event.clear()
        return False

    monkeypatch.setattr(eng, "_execute_step", fake_execute)
    monkeypatch.setattr(eng, "_handle_shelf_drop", fake_handle)
    monkeypatch.setattr(eng, "_record_skipped_scan", lambda *a, **kw: None)

    task = Task(
        task_id="t5", robot_id="kachaka", status=TaskStatus.QUEUED,
        steps=[TaskStep(step_id="m0", action=StepAction.MOVE_SHELF,
                        params={"shelf_id": "S_04", "location_id": "B_101-1"},
                        skip_on_failure=["a0"]),
               TaskStep(step_id="a0", action=StepAction.BIO_SCAN, params={"bed_key": "101-1"}),
               TaskStep(step_id="w1", action=StepAction.WAIT, params={"seconds": "0"})],
    )
    await eng.run_task(task)

    # handled once after the failed move (trigger path); never at the
    # skipped scan's loop top
    assert handled_at == [0]
    assert task.steps[1].status == StepStatus.SKIPPED
