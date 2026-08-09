"""TODO-018: start_patrol duplicate-submit dedup + low-battery gate.

Two runs on one shelf fight each other on the robot, so a second submit while
one is queued/executing returns the live task instead of creating another. The
battery gate refuses to start a doomed run, but stays fail-open when the robot
cannot be queried at all. Every patrol entry point — manual, demo button,
schedule, resume — goes through start_patrol, so all of them are covered.
"""
import asyncio

import pytest
from fastapi import HTTPException

import dependencies
import routers.patrol as patrol
from common_types import Task, TaskStatus, TaskStep
from services.task_runtime import tasks_db


SETTINGS = {"shelf_id": "S_04", "demo_preset": "", "patrol_min_battery_pct": 30}
PATROL_CFG = {"beds_order": [{"bed_key": "B_101-1", "enabled": True}]}
BEDS_CFG = {"beds": {"B_101-1": {"location_id": "L_101-1"}}}


class _FakeFleet:
    def __init__(self, battery):
        self._battery = battery

    async def get_battery_info(self, robot_id):
        if isinstance(self._battery, Exception):
            raise self._battery
        return self._battery


@pytest.fixture
def patched(monkeypatch):
    """start_patrol with config/IO stubbed; battery is set per-test."""
    submitted = []
    tasks_db.clear()

    monkeypatch.setattr(patrol, "get_runtime_settings", lambda: dict(SETTINGS))
    monkeypatch.setattr(
        patrol, "load_json",
        lambda path, default: BEDS_CFG if "beds" in str(path) else PATROL_CFG,
    )

    async def _submit(task):
        submitted.append(task)

    monkeypatch.setattr(patrol, "submit_task", _submit)

    def _set_battery(battery):
        monkeypatch.setattr(dependencies, "get_fleet", lambda: _FakeFleet(battery))

    _set_battery({"ok": True, "percentage": 88.0, "power_status": "IDLE"})
    yield _set_battery, submitted
    tasks_db.clear()


def _start(mode="patrol"):
    return asyncio.run(patrol.start_patrol(patrol.PatrolStartRequest(mode=mode)))


# ── dedup ────────────────────────────────────────────────────────────────────

def test_second_submit_returns_running_task(patched):
    _, submitted = patched

    first = _start()
    second = _start()

    assert first["status"] == "ok"
    assert second["status"] == "already_running"
    assert second["task_id"] == first["task_id"]
    assert len(submitted) == 1
    assert len(tasks_db) == 1


def test_in_progress_task_also_blocks(patched):
    _, submitted = patched

    first = _start()
    tasks_db[first["task_id"]].status = TaskStatus.IN_PROGRESS

    assert _start()["status"] == "already_running"
    assert len(submitted) == 1


def test_other_mode_is_also_deduped(patched):
    """One robot, one shelf: a demo pressed mid-patrol would run the same shelf
    right after the patrol, so it is a duplicate too. The reply names the mode
    that is actually running, not the one that was asked for."""
    _, submitted = patched

    patrol_run = _start(mode="patrol")
    demo = _start(mode="demo")

    assert demo["status"] == "already_running"
    assert demo["task_id"] == patrol_run["task_id"]
    assert demo["mode"] == "patrol"
    assert len(submitted) == 1


def test_finished_task_does_not_block(patched):
    _, submitted = patched

    first = _start()
    tasks_db[first["task_id"]].status = TaskStatus.DONE

    second = _start()
    assert second["status"] == "ok"
    assert second["task_id"] != first["task_id"]
    assert len(submitted) == 2


def test_task_carries_mode_metadata(patched):
    _, submitted = patched

    _start(mode="demo")
    assert submitted[0].metadata == {"mode": "demo"}


def test_unrelated_task_without_metadata_does_not_block(patched):
    """Only patrol-family runs carry a mode. A generic /api/tasks submission has
    none and must not swallow a patrol start via a None == None match. (Scheduled
    runs go through start_patrol, so they are tagged and DO block.)"""
    _, submitted = patched
    tasks_db["other"] = Task(
        task_id="other",
        robot_id="kachaka",
        steps=[TaskStep(step_id="s", action="wait", params={})],
        status=TaskStatus.IN_PROGRESS,
    )

    assert _start()["status"] == "ok"
    assert len(submitted) == 1


# ── battery gate ─────────────────────────────────────────────────────────────

def test_low_battery_rejected(patched):
    set_battery, submitted = patched
    set_battery({"ok": True, "percentage": 12.0, "power_status": "IDLE"})

    with pytest.raises(HTTPException) as exc:
        _start()

    assert exc.value.status_code == 400
    assert "12" in exc.value.detail and "30" in exc.value.detail
    assert submitted == []
    assert tasks_db == {}


def test_battery_at_threshold_is_allowed(patched):
    set_battery, submitted = patched
    set_battery({"ok": True, "percentage": 30.0, "power_status": "IDLE"})

    assert _start()["status"] == "ok"
    assert len(submitted) == 1


def test_battery_query_failure_fails_open(patched):
    set_battery, submitted = patched
    set_battery(RuntimeError("robot unreachable"))

    assert _start()["status"] == "ok"
    assert len(submitted) == 1


def test_missing_percentage_field_fails_open(patched):
    set_battery, submitted = patched
    set_battery({"ok": False})

    assert _start()["status"] == "ok"
    assert len(submitted) == 1


# ── the other entry points inherit both guards ───────────────────────────────

def _run_scheduled():
    """Fire the APScheduler job body directly, as the 23:00 trigger would."""
    from services.scheduler import scheduler_service
    asyncio.run(scheduler_service._run_patrol("night"))


def test_scheduled_run_is_tagged_and_blocks_a_manual_start(patched):
    """The scheduled run used to build its own Task, so it carried no mode and a
    manual start beside it queued a second full route behind the live one."""
    _, submitted = patched

    _run_scheduled()

    assert len(submitted) == 1
    assert submitted[0].metadata == {"mode": "patrol"}
    assert _start()["status"] == "already_running"
    assert len(submitted) == 1


def test_manual_run_blocks_a_scheduled_run(patched):
    _, submitted = patched
    manual = _start()

    _run_scheduled()

    assert len(submitted) == 1
    assert list(tasks_db) == [manual["task_id"]]


def test_scheduled_run_respects_battery_gate(patched):
    """The unattended night run is the one that most needs the gate; it used to
    be the only patrol path without it. The refusal must not crash the job."""
    set_battery, submitted = patched
    set_battery({"ok": True, "percentage": 9.0, "power_status": "IDLE"})

    _run_scheduled()

    assert submitted == []


def test_resumed_run_is_tagged_and_blocks_a_manual_start(patched, monkeypatch):
    """A resume (including the Zigbee shelf_resume button) is a patrol run too —
    starting another one lands two runs on the same shelf."""
    _, submitted = patched

    class _Result:
        success = True
        error_code = 0

    class _Client:
        def reset_shelf_pose(self, shelf_id):
            return _Result()

    class _Fleet:
        def get_raw_client(self, robot_id):
            return _Client()

    monkeypatch.setattr(dependencies, "get_fleet", lambda: _Fleet())
    tasks_db["dropped"] = Task(
        task_id="dropped",
        robot_id="kachaka",
        steps=[TaskStep(step_id="s", action="bio_scan", params={})],
        status=TaskStatus.SHELF_DROPPED,
        metadata={
            "shelf_drop": True,
            "shelf_id": "S_04",
            "remaining_beds": [{"bed_key": "B_101-1", "location_id": "L_101-1"}],
        },
    )

    asyncio.run(patrol.resume_patrol(patrol.ResumePatrolRequest(task_id="dropped")))

    assert submitted[0].metadata == {"mode": "patrol"}
    assert _start()["status"] == "already_running"
    assert len(submitted) == 1
