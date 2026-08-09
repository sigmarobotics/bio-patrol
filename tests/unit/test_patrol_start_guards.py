"""TODO-018: start_patrol duplicate-submit dedup + low-battery gate.

Two runs of the same mode on one shelf fight each other on the robot, so a
second submit while one is queued/executing returns the live task instead of
creating another. The battery gate refuses to start a doomed run, but stays
fail-open when the robot cannot be queried at all.
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


def test_other_mode_is_not_deduped(patched):
    _, submitted = patched

    _start(mode="patrol")
    demo = _start(mode="demo")

    assert demo["status"] == "ok"
    assert len(submitted) == 2


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
    """Scheduler-created tasks carry no mode — they must not swallow a manual
    start via a None == None match."""
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
