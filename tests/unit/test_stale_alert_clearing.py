"""IT-16 — 新營/板榮: stale shelf_dropped alerts piled up faster than staff
could clear them.

An offline weekend fires one schedule per slot, each run collapses into a
SHELF_DROPPED(disconnect) task, and recover-shelf used to clear exactly one
per press. These tests pin the three clearing paths: a successful recovery
(manual endpoint or the run-opening reset_shelf step) sweeps every standing
alert, and a newer disconnect alert supersedes older disconnect alerts while
leaving real drops in place.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import dependencies
import routers.patrol as patrol
from common_types import StepAction, StepStatus, Task, TaskStatus, TaskStep
from services import task_runtime
from services.task_runtime import clear_shelf_dropped_tasks, tasks_db


UNREACHABLE_READ = {"ok": False, "error": "UNAVAILABLE: ...", "retryable": True}


@pytest.fixture(autouse=True)
def _clean_tasks_db():
    tasks_db.clear()
    yield
    tasks_db.clear()


def _dropped(task_id: str, *, disconnect: bool | None = False) -> Task:
    # disconnect=None models a pre-IT-16 task whose metadata never got the key.
    metadata = {"shelf_drop": True}
    if disconnect is not None:
        metadata["disconnect"] = disconnect
    return Task(task_id=task_id, robot_id="kachaka", steps=[],
                status=TaskStatus.SHELF_DROPPED, metadata=metadata)


# ── the sweep helper itself ──────────────────────────────────────────────────

def test_clear_all_marks_every_dropped_task_done():
    tasks_db["a"] = _dropped("a", disconnect=True)
    tasks_db["b"] = _dropped("b", disconnect=False)
    tasks_db["c"] = Task(task_id="c", robot_id="kachaka", steps=[],
                         status=TaskStatus.IN_PROGRESS)

    assert clear_shelf_dropped_tasks(reason="test") == 2
    assert tasks_db["a"].status == TaskStatus.DONE
    assert tasks_db["b"].status == TaskStatus.DONE
    assert tasks_db["c"].status == TaskStatus.IN_PROGRESS


def test_only_disconnect_leaves_real_drops_standing():
    tasks_db["off"] = _dropped("off", disconnect=True)
    tasks_db["real"] = _dropped("real", disconnect=False)
    tasks_db["legacy"] = _dropped("legacy", disconnect=None)
    tasks_db["legacy"].metadata = None  # metadata may be absent entirely

    assert clear_shelf_dropped_tasks(only_disconnect=True, reason="test") == 1
    assert tasks_db["off"].status == TaskStatus.DONE
    assert tasks_db["real"].status == TaskStatus.SHELF_DROPPED
    assert tasks_db["legacy"].status == TaskStatus.SHELF_DROPPED


# ── manual recover-shelf clears the whole backlog ────────────────────────────

def _recover_with(monkeypatch, reset_result):
    client = MagicMock()
    client.reset_shelf_pose = MagicMock(return_value=reset_result)
    fleet = MagicMock()
    fleet.get_raw_client = MagicMock(return_value=client)
    monkeypatch.setattr(dependencies, "get_fleet", lambda: fleet)
    return asyncio.run(
        patrol.recover_shelf(patrol.RecoverShelfRequest(shelf_id="S_04")))


def test_recover_shelf_clears_every_dropped_task(monkeypatch):
    tasks_db["a"] = _dropped("a", disconnect=True)
    tasks_db["b"] = _dropped("b", disconnect=True)
    tasks_db["c"] = _dropped("c", disconnect=False)

    res = _recover_with(monkeypatch, SimpleNamespace(success=True))

    assert res["status"] == "ok"
    assert all(tasks_db[k].status == TaskStatus.DONE for k in ("a", "b", "c"))


def test_failed_recover_clears_nothing(monkeypatch):
    tasks_db["a"] = _dropped("a", disconnect=True)

    res = _recover_with(monkeypatch,
                        SimpleNamespace(success=False, error_code=11102))

    assert res["status"] == "error"
    assert tasks_db["a"].status == TaskStatus.SHELF_DROPPED


# ── the run-opening reset_shelf step sweeps stale alerts ─────────────────────

def _reset_engine(reset_result) -> task_runtime.TaskEngine:
    fleet = MagicMock()
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={
        "poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0,
    })
    fleet.reset_metrics = AsyncMock(return_value=None)
    fleet.reset_shelf_pose = AsyncMock(return_value=reset_result)
    return task_runtime.TaskEngine(fleet, "kachaka")


def _reset_task() -> Task:
    return Task(
        task_id="t-run", robot_id="kachaka", status=TaskStatus.QUEUED,
        steps=[TaskStep(step_id="reset_shelf",
                        action=StepAction.RESET_SHELF_POSE.value,
                        params={"shelf_id": "S_04"}, status=StepStatus.PENDING)],
    )


def _run_reset(engine, task):
    with patch.object(task_runtime.dispatcher, "dispatch",
                      new=AsyncMock(return_value=None)):
        return asyncio.run(engine.run_task(task))


def test_successful_reset_step_clears_offline_alerts_only():
    """The reset is a pure pose write — it proves the robot answers, so
    offline alerts are stale, but it never verifies the shelf physically and
    must not eat a real CRITICAL drop (the shelf-to-NS button also runs it)."""
    tasks_db["stale-off"] = _dropped("stale-off", disconnect=True)
    tasks_db["stale-real"] = _dropped("stale-real", disconnect=False)
    engine = _reset_engine({"ok": True})
    task = _reset_task()
    tasks_db[task.task_id] = task

    result = _run_reset(engine, task)

    assert result.status == TaskStatus.DONE
    assert tasks_db["stale-off"].status == TaskStatus.DONE
    assert tasks_db["stale-real"].status == TaskStatus.SHELF_DROPPED


def test_failed_reset_step_clears_nothing():
    tasks_db["stale-off"] = _dropped("stale-off", disconnect=True)
    engine = _reset_engine({"ok": False, "error": "TIMEOUT"})
    task = _reset_task()
    tasks_db[task.task_id] = task

    _run_reset(engine, task)

    assert tasks_db["stale-off"].status == TaskStatus.SHELF_DROPPED


# ── a newer disconnect supersedes older disconnect alerts ────────────────────

def test_new_disconnect_supersedes_old_disconnect_only(monkeypatch):
    tasks_db["old-off"] = _dropped("old-off", disconnect=True)
    tasks_db["old-real"] = _dropped("old-real", disconnect=False)

    fleet = MagicMock()
    fleet.get_battery_info = AsyncMock(return_value=UNREACHABLE_READ)
    fleet.get_command_state = AsyncMock(return_value=UNREACHABLE_READ)
    fleet.cancel_command = AsyncMock(return_value={"ok": True})
    fleet.return_home = AsyncMock(return_value={"ok": True})
    fleet.get_controller_state = AsyncMock(return_value={
        "pose_x": 3.0, "pose_y": -1.0, "pose_theta": 0.25, "last_updated": 1.0,
    })
    fleet.get_raw_client = MagicMock(side_effect=RuntimeError("no robot"))
    eng = task_runtime.TaskEngine(fleet, "kachaka")
    eng._disconnect_suspected = True
    eng._query_shelf_pose = AsyncMock(return_value=None)
    monkeypatch.setattr(task_runtime.dispatcher, "dispatch",
                        AsyncMock(return_value=None))
    monkeypatch.setattr(task_runtime.TaskEngine, "_record_skipped_scan",
                        lambda self, *a, **kw: None)

    task = Task(
        task_id="t-new", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[TaskStep(step_id="s1", action=StepAction.BIO_SCAN,
                        params={"bed_key": "B_101-1"})],
    )
    tasks_db[task.task_id] = task
    asyncio.run(eng._handle_shelf_drop(task, 0))

    assert task.metadata["disconnect"] is True
    assert task.status == TaskStatus.SHELF_DROPPED     # current alert stands
    assert tasks_db["old-off"].status == TaskStatus.DONE
    assert tasks_db["old-real"].status == TaskStatus.SHELF_DROPPED
