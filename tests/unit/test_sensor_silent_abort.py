"""IT-23 — 板橋榮家 10/12–10/15: the Wisleep pad runs off a power bank and went
silent mid-run on three of four nights, yet the patrol waited out every bed and
reported "巡房完成" with zero readings. A pad that is alive publishes every
~2.5s even with nobody on it, so a whole scan window with no message at all
means the pad is dead. After sensor_silent_abort_beds such beds in a row the
run cancels itself (shelf home via the usual cleanup) and asks staff to check
the power bank. These tests pin when that fires and when it must not.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from common_types import StepAction, StepStatus, Task, TaskStatus, TaskStep
from services import task_runtime
from services.notifications import Severity, Source
from services.notifications.evaluator import ScanOutcome
from services.task_runtime import current_tasks, tasks_db


@pytest.fixture(autouse=True)
def _clean_state():
    tasks_db.clear()
    current_tasks.clear()
    yield
    tasks_db.clear()
    current_tasks.clear()


@pytest.fixture
def settings(monkeypatch):
    cfg = {"sensor_silent_abort_beds": 3}
    monkeypatch.setattr(task_runtime, "get_runtime_settings", lambda: cfg)
    return cfg


@pytest.fixture
def dispatched(monkeypatch):
    events = []

    async def _capture(event):
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)
    return events


def _silent(bed):
    return ScanOutcome(
        task_id="t-1", location_id="L", bed_name=bed, valid_record=None,
        retry_count=0, last_record_raw=None,
        last_failure_reason="未收到感測器資料（MQTT無連線或無數據）",
        messages_seen=0,
    )


def _restless(bed):
    return ScanOutcome(
        task_id="t-1", location_id="L", bed_name=bed, valid_record=None,
        retry_count=3, last_record_raw={"status": 2}, last_failure_reason="躁動",
        messages_seen=40,
    )


class _Client:
    """Hands out one queued outcome per scan; a callable entry runs first
    (e.g. to trip the drop event mid-scan) and returns the outcome."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)

    async def get_valid_scan_data(self, target_bed=None, task_id=None, bed_name=None):
        item = self.outcomes.pop(0)
        return item() if callable(item) else item


def _setup(monkeypatch, outcomes, *, n_beds=None, status=TaskStatus.IN_PROGRESS,
           mode=None):
    n_beds = n_beds or len(outcomes)
    task = Task(
        task_id="t-1", robot_id="kachaka", status=status,
        metadata={"mode": mode} if mode else {},
        steps=[TaskStep(step_id=f"s{i}", action=StepAction.BIO_SCAN.value,
                        params={"bed_key": f"B_10{i}-1"}, status=StepStatus.PENDING)
               for i in range(n_beds)],
    )
    tasks_db[task.task_id] = task
    current_tasks["kachaka"] = task.task_id
    client = _Client(outcomes)
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: client)
    fleet = MagicMock()
    eng = task_runtime.TaskEngine(fleet, "kachaka")
    eng.current_task_id = task.task_id
    eng.target_bed = "L_101-1"
    return task, eng


def _scan(eng, task, count=None):
    """Run the bio_scan steps back to back in one loop (the engine's events
    bind to the loop that first waits on them)."""
    async def _go():
        eng.shelf_drop_event = asyncio.Event()
        eng._battery_abort_event = asyncio.Event()
        results = []
        for step in task.steps[:count]:
            results.append(await eng._do_bio_scan(step))
        return results
    return asyncio.run(_go())


def _aborts(events):
    return [e for e in events if e.source == Source.SENSOR_SILENT_ABORT]


def test_source_value():
    assert Source.SENSOR_SILENT_ABORT.value == "sensor_silent_abort"


def test_three_silent_beds_cancel_and_notify(monkeypatch, settings, dispatched):
    task, eng = _setup(monkeypatch, [_silent("B1"), _silent("B2"), _silent("B3")],
                       n_beds=5)
    results = _scan(eng, task, 3)

    assert all(r.success is False for r in results)
    assert task.status == TaskStatus.CANCELLED
    assert task.metadata["sensor_silent_abort"] is True
    assert task.metadata["sensor_silent_beds"] == 3
    assert task.metadata["announce_after_home"] is True
    # No command in flight — run_task breaks at the step boundary instead.
    eng.fleet.cancel_command.assert_not_called()

    aborts = _aborts(dispatched)
    assert len(aborts) == 1
    event = aborts[0]
    assert event.severity == Severity.WARN
    assert event.task_id == "t-1"
    assert event.title == "🔌 感測器無回應，巡房自動收工"
    assert "連續 3 床沒有收到感測墊訊息" in event.body
    assert "行動電源" in event.body
    assert "已量測 0/5 床" in event.body


def test_two_silent_beds_do_not_abort(monkeypatch, settings, dispatched):
    task, eng = _setup(monkeypatch, [_silent("B1"), _silent("B2")], n_beds=5)
    _scan(eng, task, 2)

    assert task.status == TaskStatus.IN_PROGRESS
    assert _aborts(dispatched) == []


def test_bed_with_messages_resets_streak(monkeypatch, settings, dispatched):
    task, eng = _setup(monkeypatch, [_silent("B1"), _silent("B2"), _restless("B3"),
                                     _silent("B4"), _silent("B5")])
    _scan(eng, task)

    assert task.status == TaskStatus.IN_PROGRESS
    assert eng._silent_streak == 2
    assert _aborts(dispatched) == []


def test_limit_zero_disables(monkeypatch, settings, dispatched):
    settings["sensor_silent_abort_beds"] = 0
    task, eng = _setup(monkeypatch, [_silent(f"B{i}") for i in range(5)])
    _scan(eng, task)

    assert task.status == TaskStatus.IN_PROGRESS
    assert _aborts(dispatched) == []


def test_interrupted_scan_neither_counts_nor_resets(monkeypatch, settings, dispatched):
    task, eng = _setup(monkeypatch, [_silent("B1"), _silent("B2"), None, _silent("B4")])
    _scan(eng, task, 3)

    assert eng._silent_streak == 2
    assert task.status == TaskStatus.IN_PROGRESS
    assert _aborts(dispatched) == []

    # The next silent bed is the third counted one.
    async def _last():
        eng.shelf_drop_event = asyncio.Event()
        eng._battery_abort_event = asyncio.Event()
        await eng._do_bio_scan(task.steps[3])
    asyncio.run(_last())
    assert task.status == TaskStatus.CANCELLED


def test_drop_during_scan_is_not_counted(monkeypatch, settings, dispatched):
    """A drop owns the wrap-up — an abort here would leave only a WARN for a
    real drop."""
    holder = {}

    def _silent_with_drop():
        holder["eng"].shelf_drop_event.set()
        return _silent("B3")

    task, eng = _setup(monkeypatch, [_silent("B1"), _silent("B2"), _silent_with_drop])
    holder["eng"] = eng
    _scan(eng, task)

    assert eng._silent_streak == 2
    assert task.status == TaskStatus.IN_PROGRESS
    assert "sensor_silent_abort" not in task.metadata
    assert _aborts(dispatched) == []


def test_drop_after_scan_blocks_abort(monkeypatch, settings):
    """Drop tripped while the per-bed alert was going out: drop wins."""
    events = []
    holder = {}

    async def _capture(event):
        events.append(event)
        if event.source == Source.BIO_SCAN_FAILURE and event.bed_key == "B3":
            holder["eng"].shelf_drop_event.set()

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)
    task, eng = _setup(monkeypatch, [_silent("B1"), _silent("B2"), _silent("B3")])
    holder["eng"] = eng
    _scan(eng, task)

    assert task.status == TaskStatus.IN_PROGRESS
    assert _aborts(events) == []


@pytest.mark.parametrize("status", [TaskStatus.SHELF_DROPPED, TaskStatus.DONE])
def test_task_not_in_progress_is_left_alone(monkeypatch, settings, dispatched, status):
    task, eng = _setup(monkeypatch, [_silent(f"B{i}") for i in range(3)], status=status)
    _scan(eng, task)

    assert task.status == status
    assert _aborts(dispatched) == []


def test_cleanup_task_is_never_aborted(monkeypatch, settings, dispatched):
    task, eng = _setup(monkeypatch, [_silent(f"B{i}") for i in range(3)], mode="cleanup")
    _scan(eng, task)

    assert task.status == TaskStatus.IN_PROGRESS
    assert _aborts(dispatched) == []


def test_dispatch_failure_still_cancels(monkeypatch, settings):
    events = []

    async def _flaky(event):
        if event.source == Source.SENSOR_SILENT_ABORT:
            raise RuntimeError("telegram down")
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _flaky)
    task, eng = _setup(monkeypatch, [_silent(f"B{i}") for i in range(3)])
    results = _scan(eng, task)

    assert len(results) == 3
    assert task.status == TaskStatus.CANCELLED
    assert task.metadata["sensor_silent_abort"] is True


def test_fires_once_per_run(monkeypatch, settings, dispatched):
    task, eng = _setup(monkeypatch, [_silent(f"B{i}") for i in range(5)])
    _scan(eng, task)

    assert task.status == TaskStatus.CANCELLED
    assert len(_aborts(dispatched)) == 1


def test_per_bed_alert_precedes_abort(monkeypatch, settings, dispatched):
    task, eng = _setup(monkeypatch, [_silent("B1"), _silent("B2"), _silent("B3")])
    _scan(eng, task)

    sources = [e.source for e in dispatched]
    assert sources == [Source.BIO_SCAN_FAILURE] * 3 + [Source.SENSOR_SILENT_ABORT]
    assert dispatched[2].bed_key == "B3"


def test_unreachable_bed_leaves_streak_alone(monkeypatch, settings, dispatched):
    """走不到 is normal (design gate) but says nothing about the pad: a bed whose
    move_shelf failed and whose bio_scan run_task skips neither counts nor
    resets — silent, silent, skip, silent still reaches the limit of 3, so a
    dead pad plus a room that TIMEOUTs on every other bed is still caught."""
    from unittest.mock import AsyncMock

    task, eng = _setup(monkeypatch, [_silent("B1"), _silent("B2"), _silent("B4")], n_beds=4)
    move = TaskStep(step_id="m2", action=StepAction.MOVE_SHELF.value,
                    params={"shelf_id": "S1", "location_id": "L_102"},
                    status=StepStatus.PENDING, skip_on_failure=["s2"])
    task.steps.insert(2, move)
    task.metadata = {"mode": "patrol"}
    eng.fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    eng.fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    eng.fleet.get_metrics = AsyncMock(return_value={
        "poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0,
    })
    eng.fleet.reset_metrics = AsyncMock(return_value=None)
    eng.fleet.move_shelf = AsyncMock(
        return_value={"ok": False, "error_code": 19004, "error": "unreachable"})

    asyncio.run(eng.run_task(task))

    assert task.steps[3].status == StepStatus.SKIPPED
    assert eng._silent_streak == 3
    assert task.status == TaskStatus.CANCELLED
    assert task.metadata["sensor_silent_beds"] == 3
    assert len(_aborts(dispatched)) == 1


# ── IT-24: say why once the robot is back on the charger ─────────────────────

def _run_fleet():
    fleet = MagicMock()
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={
        "poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0,
    })
    fleet.reset_metrics = AsyncMock(return_value=None)
    fleet.return_home = AsyncMock(return_value={"ok": True})
    return fleet


def _cleanup(monkeypatch, *, flag=True, home_ok=True, connected=True, status=TaskStatus.QUEUED):
    meta = {"mode": "cleanup", **({"announce_after_home": True} if flag else {})}
    task = Task(task_id="c-1", robot_id="kachaka", status=status, metadata=meta,
                steps=[TaskStep(step_id="cleanup_return_home",
                                action=StepAction.RETURN_HOME.value, params={},
                                status=StepStatus.PENDING)])
    tasks_db[task.task_id] = task
    sensor = None if connected is None else types.SimpleNamespace(connected=connected)
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: sensor)
    fleet = _run_fleet()
    if not home_ok:
        fleet.return_home = AsyncMock(
            return_value={"ok": False, "error_code": 10001, "error": "blocked"})
    return task, task_runtime.TaskEngine(fleet, "kachaka")


@pytest.mark.parametrize("connected,sound", [
    (True, "sensor_silent_zh"),
    (False, "sensor_pc_offline_zh"),
    (None, "sensor_pc_offline_zh"),
])
def test_cleanup_home_announces_by_sensor_link(monkeypatch, dispatched, announced,
                                               connected, sound):
    """Pad silent on a live broker link → the power-bank line; link down (or no
    client at all) → the sensor-PC line. 1s pause after docking."""
    task, eng = _cleanup(monkeypatch, connected=connected)

    asyncio.run(eng.run_task(task))

    assert task.status == TaskStatus.DONE
    assert announced == [(sound, 1.0, False)]


def test_cleanup_home_failure_announces_nothing(monkeypatch, dispatched, announced):
    task, eng = _cleanup(monkeypatch, home_ok=False)

    asyncio.run(eng.run_task(task))

    assert task.status == TaskStatus.FAILED
    assert announced == []


def test_cancelled_cleanup_announces_nothing(monkeypatch, dispatched, announced):
    task, eng = _cleanup(monkeypatch)

    async def _cancelled_on_the_way(robot_id):
        task.status = TaskStatus.CANCELLED
        return {"ok": False, "error": "cancelled"}

    eng.fleet.return_home = AsyncMock(side_effect=_cancelled_on_the_way)
    asyncio.run(eng.run_task(task))

    assert task.status == TaskStatus.CANCELLED
    assert announced == []


def test_ordinary_cleanup_announces_nothing(monkeypatch, dispatched, announced):
    """An operator's 停止 queues a cleanup without the flag — nothing to say."""
    task, eng = _cleanup(monkeypatch, flag=False)

    asyncio.run(eng.run_task(task))

    assert task.status == TaskStatus.DONE
    assert announced == []


def test_announce_failure_keeps_the_summary(monkeypatch, dispatched):
    async def _boom(name, *, pre_delay_s=0.0, wait=False):
        raise RuntimeError("deploy blew up")

    monkeypatch.setattr(task_runtime.announcer, "announce", _boom)
    task, eng = _cleanup(monkeypatch)

    asyncio.run(eng.run_task(task))

    assert task.status == TaskStatus.DONE
    assert [e.source for e in dispatched] == [Source.TASK_SUMMARY]


def _capture_cleanup(monkeypatch):
    queued = []

    async def _submit(robot_id, metadata_extra=None):
        queued.append(metadata_extra)

    monkeypatch.setattr(task_runtime, "submit_cleanup_task", _submit)
    return queued


def test_silent_abort_hands_the_flag_to_its_cleanup(monkeypatch, settings, dispatched):
    queued = _capture_cleanup(monkeypatch)
    task, eng = _setup(monkeypatch, [_silent("B1"), _silent("B2"), _silent("B3")],
                       n_beds=4)
    task.status = TaskStatus.QUEUED
    task.metadata = {"mode": "patrol"}
    eng.fleet = _run_fleet()

    asyncio.run(eng.run_task(task))

    assert task.status == TaskStatus.CANCELLED
    assert queued == [{"announce_after_home": True}]


def test_operator_cancel_queues_a_plain_cleanup(monkeypatch, dispatched):
    queued = _capture_cleanup(monkeypatch)
    task = Task(task_id="t-2", robot_id="kachaka", status=TaskStatus.QUEUED,
                metadata={"mode": "patrol"},
                steps=[TaskStep(step_id="w", action=StepAction.WAIT.value,
                                params={"seconds": "0"}, status=StepStatus.PENDING)])
    tasks_db[task.task_id] = task
    eng = task_runtime.TaskEngine(_run_fleet(), "kachaka")

    async def _cancel_then_wait(step):
        task.status = TaskStatus.CANCELLED
        return task_runtime.StepResult(success=True, timestamp="t")

    eng._action_handlers[StepAction.WAIT.value] = _cancel_then_wait
    asyncio.run(eng.run_task(task))

    assert queued == [None]


def test_cleanup_mode_cannot_be_overridden(monkeypatch):
    submitted = []

    class _Fleet:
        async def get_moving_shelf(self, robot_id):
            return {"shelf_id": None}

    async def _submit(task):
        submitted.append(task)

    import dependencies
    monkeypatch.setattr(dependencies, "get_fleet", lambda: _Fleet())
    monkeypatch.setattr(task_runtime, "submit_task", _submit)

    asyncio.run(task_runtime.submit_cleanup_task(
        "kachaka", metadata_extra={"mode": "patrol", "announce_after_home": True}))

    assert submitted[0].metadata == {"mode": "cleanup", "announce_after_home": True}
