"""IT-17 — 板橋榮家 2026-08-19: the battery drained to 0.89% mid-patrol and the
firmware put the shelf down in the corridor before driving itself to the
charger. The 30% start gate cannot catch a run that starts just above it, so
the state watcher now aborts the run while there is still charge to carry the
shelf home. These tests pin when the abort fires and, just as importantly,
when it must not.

2026-09-16 新營: PFR say this pack's BMU has no proper regulation, so motor
start/stop swings the reported percentage — their own app shows only
high/medium/low. All 11 field aborts fired while the robot was driving and
none during a stationary dwell, although it stood still for 76% of a run. The
abort therefore only counts samples taken standing still, and only after
BATTERY_ABORT_CONSECUTIVE of them; these tests pin both gates.
"""
from __future__ import annotations

import asyncio
import time
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


def _standing_still(eng: task_runtime.TaskEngine) -> None:
    """Put the engine in a settled stationary window (no motion step in flight)."""
    eng._stationary_since = time.monotonic() - task_runtime.BATTERY_SETTLE_SECONDS - 1


def _abort_after_confirmation(eng: task_runtime.TaskEngine) -> bool:
    """Run the check until it either aborts or exhausts the streak budget."""
    for _ in range(task_runtime.BATTERY_ABORT_CONSECUTIVE):
        fired = asyncio.run(eng._maybe_abort_low_battery())
        if fired:
            return True
    return False


def _reading(pct, *, ok=True, power_status="3") -> dict:
    return {"return_value": {"ok": ok, "percentage": pct,
                             "power_status": power_status}}


def test_below_threshold_cancels_and_notifies(dispatched):
    task = _patrol_task()
    eng = _engine(_reading(8))
    _standing_still(eng)

    assert _abort_after_confirmation(eng) is True
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
    _standing_still(eng)

    assert _abort_after_confirmation(eng) is True
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
    _standing_still(eng)

    assert _abort_after_confirmation(eng) is True
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
    _standing_still(eng)
    eng._low_battery_streak = task_runtime.BATTERY_ABORT_CONSECUTIVE - 1

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
    _standing_still(eng)

    assert _abort_after_confirmation(eng) is True
    assert task.status == TaskStatus.CANCELLED
    eng.fleet.cancel_command.assert_awaited_once_with("kachaka")


def test_shelf_release_in_cancel_window_does_not_overwrite_cancelled(dispatched):
    """PR #39 review: after the abort cancels the run, the firmware's own
    put-down (or cancel_command noise) raises the drop event — the handler
    must leave CANCELLED alone, or run_task never queues the cleanup that
    carries the shelf home."""
    task = _patrol_task()
    task.status = TaskStatus.CANCELLED
    eng = _engine(_reading(8))

    asyncio.run(eng._handle_shelf_drop(task, 0))

    assert task.status == TaskStatus.CANCELLED
    assert dispatched == []
    eng.fleet.cancel_command.assert_not_awaited()


# ── Motion gate + confirmation streak (2026-09-16 新營) ──────────────────────

def test_low_reading_while_moving_never_aborts(dispatched):
    """The load-sag case. Every field abort to date fired on a reading like
    this one, taken with the motors under load — on PFR's own account the
    percentage is not trustworthy then, so it must not end the run however
    often it repeats."""
    task = _patrol_task()
    eng = _engine(_reading(4))
    # _stationary_since left None: the robot is driving.

    for _ in range(10):
        assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert task.status == TaskStatus.IN_PROGRESS
    assert eng._low_battery_streak == 0
    assert dispatched == []
    eng.fleet.cancel_command.assert_not_awaited()


def test_single_stationary_low_reading_waits_for_confirmation(dispatched):
    task = _patrol_task()
    eng = _engine(_reading(8))
    _standing_still(eng)

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert task.status == TaskStatus.IN_PROGRESS
    assert eng._low_battery_streak == 1
    assert dispatched == []


def test_reading_inside_the_settle_window_is_not_counted(dispatched):
    """The dwell has started but the stopping transient has not died down."""
    task = _patrol_task()
    eng = _engine(_reading(8))
    eng._stationary_since = time.monotonic()  # just arrived

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert eng._low_battery_streak == 0
    assert task.status == TaskStatus.IN_PROGRESS


def test_a_recovered_reading_resets_the_streak(dispatched):
    """A sag followed by a normal reading is a sag, not a flat battery — the
    next low sample must start counting from zero again."""
    task = _patrol_task()
    eng = _engine(_reading(8))
    _standing_still(eng)

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert eng._low_battery_streak == 1

    eng.fleet.get_battery_info = AsyncMock(
        return_value={"ok": True, "percentage": 47, "power_status": "3"})
    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert eng._low_battery_streak == 0

    eng.fleet.get_battery_info = AsyncMock(
        return_value={"ok": True, "percentage": 8, "power_status": "3"})
    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert task.status == TaskStatus.IN_PROGRESS
    assert dispatched == []


def test_charging_resets_the_streak(dispatched):
    task = _patrol_task()
    eng = _engine(_reading(8))
    _standing_still(eng)
    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert eng._low_battery_streak == 1

    eng.fleet.get_battery_info = AsyncMock(
        return_value={"ok": True, "percentage": 8, "power_status": "1"})
    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert eng._low_battery_streak == 0
    assert task.status == TaskStatus.IN_PROGRESS


def test_every_sample_is_logged_with_its_motion_state(caplog):
    """The sample log is the discharge curve — without it there is no way to
    separate a load sag from a real drain after the fact."""
    _patrol_task()
    eng = _engine(_reading(63))

    with caplog.at_level("INFO", logger="kachaka.task_runtime"):
        asyncio.run(eng._maybe_abort_low_battery())
        _standing_still(eng)
        asyncio.run(eng._maybe_abort_low_battery())

    samples = [r.message for r in caplog.records if "[BATTERY]" in r.message]
    assert len(samples) == 2
    assert "state=moving" in samples[0]
    assert "state=still" in samples[1]
    assert "63.0%" in samples[0]


# ── Where the stationary window opens and closes (PR #42 review) ────────────

def _step(action: str) -> TaskStep:
    return TaskStep(step_id="s", action=action, params={}, status=StepStatus.PENDING)


def _run_step(eng: task_runtime.TaskEngine, action: str, handler):
    eng._action_handlers[action] = handler
    return asyncio.run(eng._execute_step(_step(action)))


@pytest.mark.parametrize("action", sorted(task_runtime.MOTION_ACTIONS))
def test_motion_steps_close_the_window_and_reopen_it_after(action):
    eng = _engine(_reading(50))
    _standing_still(eng)
    seen = []

    async def _handler(step):
        seen.append(eng._stationary_since)
        return StepResult(success=True, error_code=0, error_message="",
                          data={}, timestamp="")

    _run_step(eng, action, _handler)
    assert seen == [None]                     # closed while the robot drives
    assert eng._stationary_since is not None   # reopened once it stops


@pytest.mark.parametrize("action", [StepAction.BIO_SCAN.value, StepAction.WAIT.value,
                                    StepAction.PLAY_SOUND.value, StepAction.SPEAK.value,
                                    StepAction.RESET_SHELF_POSE.value])
def test_non_motion_steps_leave_the_window_open(action):
    """A demo run has WAIT steps and no bio_scan at all; a bed whose move failed
    has its scan skipped entirely. Both must still be able to abort — they are
    the runs that burn the most charge (2026-08-19 板橋榮家)."""
    eng = _engine(_reading(50))
    _standing_still(eng)
    opened = eng._stationary_since
    seen = []

    async def _handler(step):
        seen.append(eng._stationary_since)
        return StepResult(success=True, error_code=0, error_message="",
                          data={}, timestamp="")

    _run_step(eng, action, _handler)
    assert seen == [opened]
    assert eng._stationary_since == opened


def test_a_failing_motion_step_still_reopens_the_window():
    """A move that raises must not leave the window shut — with it shut the
    abort is dead for the rest of the run."""
    eng = _engine(_reading(50))
    _standing_still(eng)

    async def _boom(step):
        raise RuntimeError("move blew up")

    res = _run_step(eng, StepAction.MOVE_SHELF.value, _boom)
    assert res.success is False
    assert eng._stationary_since is not None


def test_window_opened_during_the_battery_read_does_not_count():
    """get_battery_info hops to a thread and retries, so on a weak link it can
    outlast the move it started under. A reading taken mid-drive must not be
    credited as stationary just because the robot stopped before it returned."""
    _patrol_task()
    eng = _engine(_reading(4))

    async def _read_while_the_move_finishes(robot_id):
        # sampled under motor load; the move completes before the read returns
        eng._stationary_since = time.monotonic() - task_runtime.BATTERY_SETTLE_SECONDS - 1
        return {"ok": True, "percentage": 4, "power_status": "3"}

    eng.fleet.get_battery_info = AsyncMock(side_effect=_read_while_the_move_finishes)
    eng._stationary_since = None  # driving when the read starts

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert eng._low_battery_streak == 0


def test_window_that_closed_during_the_battery_read_does_not_count():
    """The mirror case: stationary at the start of the read, driving again by
    the time it returns."""
    _patrol_task()
    eng = _engine(_reading(4))
    _standing_still(eng)

    async def _read_while_the_next_move_starts(robot_id):
        eng._stationary_since = None
        return {"ok": True, "percentage": 4, "power_status": "3"}

    eng.fleet.get_battery_info = AsyncMock(side_effect=_read_while_the_next_move_starts)

    assert asyncio.run(eng._maybe_abort_low_battery()) is False
    assert eng._low_battery_streak == 0


def test_abort_wakes_a_dwell_that_is_still_waiting(dispatched):
    """The abort now fires with no robot command in flight, so cancel_command
    has nothing to cancel — what has to stop is the scan window the engine is
    sitting in, or run_task only notices at the next step boundary, up to a
    full bio_scan window later."""
    task = _patrol_task()
    eng = _engine(_reading(8))
    _standing_still(eng)

    async def _scenario():
        eng.shelf_drop_event = asyncio.Event()
        eng._battery_abort_event = asyncio.Event()
        never = asyncio.Event()

        async def _endless_scan():
            await never.wait()
            return "scan finished"

        scan = asyncio.ensure_future(eng._await_or_drop(_endless_scan()))
        await asyncio.sleep(0)
        eng._low_battery_streak = task_runtime.BATTERY_ABORT_CONSECUTIVE - 1
        fired = await eng._maybe_abort_low_battery()
        return fired, await asyncio.wait_for(scan, timeout=1)

    fired, outcome = asyncio.run(_scenario())
    assert fired is True
    assert outcome is None                      # the dwell was cut short
    assert task.status == TaskStatus.CANCELLED
