"""2026-08-10 新營: a robot that left the network was reported as a shelf drop.

The robot went offline mid-patrol; the reads that failed were read as "the
shelf is gone", so staff got a CRITICAL 貨架掉落 alarm, an overlay with no
marker telling them to follow the marker, and a bare 500 when they pressed
歸位. These tests pin the three fixes: read failures are a disconnect (never a
drop), a real drop still raises CRITICAL, and the unknown-pose / unreachable
cases surface as such in the metadata and in the recover endpoint.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import dependencies
import routers.patrol as patrol
from common_types import Task, TaskStatus, TaskStep, StepAction
from services import task_runtime
from services.notifications import Severity
from services.notifications.events import Source
from services.task_runtime import classify_shelf_release
from utils.grpc_errors import is_connection_error


UNAVAILABLE = RuntimeError(
    "UNAVAILABLE: failed to connect to all addresses; No route to host"
)
HEALTHY_BATTERY = {"ok": True, "percentage": 74.0, "power_status": "2"}
IDLE_CMD = {"ok": True, "state": "1", "command": None, "is_running": False}
UNREACHABLE_READ = {"ok": False, "error": str(UNAVAILABLE), "retryable": True}


# ── connection-error recogniser ──────────────────────────────────────────────

def test_connection_errors_are_recognised():
    assert is_connection_error(UNAVAILABLE)
    assert is_connection_error("UNAVAILABLE: ...")
    assert is_connection_error("failed to connect to all addresses")
    assert is_connection_error(OSError("No route to host"))


def test_status_code_alone_is_enough():
    """The raw SDK path raises grpc.RpcError, whose text may carry no marker."""

    class _RpcError(Exception):
        def code(self):
            return SimpleNamespace(name="UNAVAILABLE")

    err = _RpcError("robot said no")
    assert not any(m in str(err) for m in ("unavailable", "connect", "socket"))
    assert is_connection_error(err)


def test_robot_errors_are_not_connection_errors():
    assert not is_connection_error(None)
    assert not is_connection_error(ValueError("shelf S_99 not found"))
    assert not is_connection_error("error_code=11005: shelf pose mismatch")
    # A robot that answers and then times out is online — treating that as a
    # disconnect would downgrade a real drop to "offline".
    assert not is_connection_error(TimeoutError("move_shelf timed out after 240s"))


# ── classifier ───────────────────────────────────────────────────────────────

def test_disconnect_beats_drop_even_with_healthy_reads():
    """A real drop that coincides with a disconnect is reported as unknown."""
    assert classify_shelf_release(
        HEALTHY_BATTERY, IDLE_CMD, disconnected=True
    ) == "disconnected"


def test_connected_robot_classification_is_unchanged():
    assert classify_shelf_release(HEALTHY_BATTERY, IDLE_CMD) == "drop"
    assert classify_shelf_release(HEALTHY_BATTERY, IDLE_CMD, disconnected=False) == "drop"


# ── state watcher ────────────────────────────────────────────────────────────

def _watcher_engine(responses, *, final="S_04"):
    """TaskEngine whose watcher reads ``responses`` then stops the loop.

    ``final`` is the read that ends the loop: a neutral "shelf still docked"
    by default, so ending the script cannot itself look like a release. Pass
    the failure instead when the run must end with the robot still unreachable
    — a successful read is what clears the disconnect suspicion.
    """
    fake_client = MagicMock()

    def _next():
        if not responses:
            eng._state_watcher_stop = True
            if isinstance(final, Exception):
                raise final
            return final
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    fake_client.get_moving_shelf_id = MagicMock(side_effect=_next)
    slot = MagicMock()
    slot.conn = MagicMock(client=fake_client)
    fleet = MagicMock()
    fleet._robots = {"kachaka": slot}
    eng = task_runtime.TaskEngine(fleet, "kachaka")
    eng.shelf_drop_event = asyncio.Event()
    eng._state_watcher_stop = False
    return eng


@pytest.fixture
def no_sleep(monkeypatch):
    """Run the 3s watcher cadence at full speed."""
    real_sleep = asyncio.sleep

    async def _fast(_delay):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast)


@pytest.mark.asyncio
async def test_repeated_read_failures_are_a_disconnect_not_a_drop(no_sleep):
    eng = _watcher_engine(["S_04", UNAVAILABLE, UNAVAILABLE], final=UNAVAILABLE)

    await eng._watch_shelf_state()

    assert eng._disconnect_suspected is True
    # The run still pauses — via the same event, so the existing flow applies.
    assert eng.shelf_drop_event.is_set()


@pytest.mark.asyncio
async def test_single_transient_read_failure_does_not_trigger(no_sleep):
    eng = _watcher_engine(["S_04", UNAVAILABLE, "S_04", "S_04"])

    await eng._watch_shelf_state()

    assert eng._disconnect_suspected is False
    assert not eng.shelf_drop_event.is_set()


@pytest.mark.asyncio
async def test_two_failures_below_threshold_do_not_trigger(no_sleep):
    eng = _watcher_engine(["S_04", UNAVAILABLE, UNAVAILABLE, "S_04"])

    await eng._watch_shelf_state()

    assert eng._disconnect_suspected is False
    assert not eng.shelf_drop_event.is_set()


@pytest.mark.asyncio
async def test_disconnect_suspicion_clears_when_the_robot_answers_again(no_sleep):
    """The flag is per-incident, not per-run: a sticky one downgrades a later
    real drop to "offline" for the rest of the patrol."""
    eng = _watcher_engine(["S_04", UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, "S_04"])

    await eng._watch_shelf_state()

    assert eng._disconnect_suspected is False
    # The pause raised while it was unreachable still stands.
    assert eng.shelf_drop_event.is_set()


@pytest.mark.asyncio
async def test_real_drop_still_fires_from_the_watcher(no_sleep):
    """A successful read reporting no shelf is still a drop, not a disconnect."""
    eng = _watcher_engine(["S_04", "S_04", None, None])

    await eng._watch_shelf_state()

    assert eng.shelf_drop_event.is_set()
    assert eng._disconnect_suspected is False


# ── drop handler ─────────────────────────────────────────────────────────────

def _engine(battery, command_state, *, shelf_pose=None, controller_state=None):
    fleet = MagicMock()
    fleet.get_battery_info = AsyncMock(return_value=battery)
    fleet.get_command_state = AsyncMock(return_value=command_state)
    fleet.cancel_command = AsyncMock(return_value={"ok": True})
    fleet.return_home = AsyncMock(return_value={"ok": True})
    fleet.get_controller_state = AsyncMock(
        return_value=controller_state
        if controller_state is not None
        else {"pose_x": 3.0, "pose_y": -1.0, "pose_theta": 0.25, "last_updated": 1.0}
    )
    fleet.get_raw_client = MagicMock(side_effect=RuntimeError("no robot"))
    eng = task_runtime.TaskEngine(fleet, "kachaka")
    eng.current_task_id = "t-1"
    eng._query_shelf_pose = AsyncMock(return_value=shelf_pose)
    return eng


def _task():
    return Task(
        task_id="t-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[TaskStep(step_id="s1", action=StepAction.BIO_SCAN,
                        params={"bed_key": "B_101-1"})],
    )


def _run_drop(monkeypatch, eng):
    events = []

    async def _capture(event):
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)
    monkeypatch.setattr(task_runtime.TaskEngine, "_record_skipped_scan",
                        lambda self, *a, **kw: None)
    task = _task()
    asyncio.run(eng._handle_shelf_drop(task, 0))
    assert len(events) == 1
    return task, events[0]


def test_unreachable_robot_reports_disconnect_not_drop(monkeypatch):
    eng = _engine(UNREACHABLE_READ, UNREACHABLE_READ)

    task, event = _run_drop(monkeypatch, eng)

    assert event.severity == Severity.WARN
    assert event.source == Source.ROBOT_OFFLINE
    assert "失聯" in event.title
    assert "貨架掉落" not in event.title
    assert "狀態未知" in event.body
    assert task.metadata["disconnect"] is True
    # The pause flow is unchanged, so resume still finds the run.
    assert task.status == TaskStatus.SHELF_DROPPED
    assert task.metadata["shelf_drop"] is True


def test_watcher_disconnect_flag_reaches_the_notification(monkeypatch):
    """Even if the robot answers again by the time the handler runs."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    eng._disconnect_suspected = True

    task, event = _run_drop(monkeypatch, eng)

    assert event.severity == Severity.WARN
    assert task.metadata["disconnect"] is True


def test_reachable_robot_drop_is_still_critical(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD,
                  shelf_pose={"x": 1.0, "y": 2.0, "theta": 0.0})

    task, event = _run_drop(monkeypatch, eng)

    assert event.severity == Severity.CRITICAL
    assert event.source == Source.SHELF_DROP
    assert "貨架掉落" in event.title
    assert task.metadata["disconnect"] is False
    assert task.metadata["pose_unknown"] is False
    assert task.metadata["last_known_robot_pose"] is None


def test_disconnected_drop_skips_return_home(monkeypatch):
    """An unreachable robot takes no commands — sending it home would only
    burn the controller's 240s timeout inside the drop handler."""
    eng = _engine(UNREACHABLE_READ, UNREACHABLE_READ)

    _run_drop(monkeypatch, eng)

    eng.fleet.return_home.assert_not_awaited()


def test_reachable_drop_still_sends_the_robot_home(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD,
                  shelf_pose={"x": 1.0, "y": 2.0, "theta": 0.0})

    _run_drop(monkeypatch, eng)

    eng.fleet.return_home.assert_awaited_once()


def test_one_failed_read_alone_is_not_a_disconnect(monkeypatch):
    """Battery unreadable but the robot still answers -> unchanged drop path."""
    eng = _engine(UNREACHABLE_READ, IDLE_CMD)

    task, event = _run_drop(monkeypatch, eng)

    assert event.severity == Severity.CRITICAL
    assert task.metadata["disconnect"] is False


def test_unknown_pose_carries_last_known_robot_pose(monkeypatch):
    eng = _engine(UNREACHABLE_READ, UNREACHABLE_READ)

    task, _ = _run_drop(monkeypatch, eng)

    assert task.metadata["shelf_pose"] is None
    assert task.metadata["pose_unknown"] is True
    assert task.metadata["last_known_robot_pose"] == {
        "x": 3.0, "y": -1.0, "theta": 0.25,
    }


def test_unknown_pose_without_any_cached_pose(monkeypatch):
    """A controller that never polled has no position to offer."""
    eng = _engine(UNREACHABLE_READ, UNREACHABLE_READ,
                  controller_state={"pose_x": 0.0, "pose_y": 0.0,
                                    "pose_theta": 0.0, "last_updated": 0.0})

    task, _ = _run_drop(monkeypatch, eng)

    assert task.metadata["pose_unknown"] is True
    assert task.metadata["last_known_robot_pose"] is None


# ── failed return_shelf while offline ────────────────────────────────────────

def test_unverifiable_return_shelf_while_offline_flags_disconnect():
    """Still stops the run, but as "state unknown" rather than a drop."""
    fleet = MagicMock()
    slot = MagicMock()
    slot.conn.client.get_moving_shelf_id = MagicMock(side_effect=UNAVAILABLE)
    fleet.get_slot_or_none = MagicMock(return_value=slot)
    eng = task_runtime.TaskEngine(fleet, "kachaka")

    assert asyncio.run(eng._shelf_dropped_en_route("S_04")) is True
    assert eng._disconnect_suspected is True


def test_unverifiable_return_shelf_for_other_errors_stays_a_drop():
    fleet = MagicMock()
    slot = MagicMock()
    slot.conn.client.get_moving_shelf_id = MagicMock(
        side_effect=ValueError("shelf S_04 not registered")
    )
    fleet.get_slot_or_none = MagicMock(return_value=slot)
    eng = task_runtime.TaskEngine(fleet, "kachaka")

    assert asyncio.run(eng._shelf_dropped_en_route("S_04")) is True
    assert eng._disconnect_suspected is False


# ── recover-shelf endpoint ───────────────────────────────────────────────────

def _fleet_raising(exc):
    client = MagicMock()
    client.reset_shelf_pose = MagicMock(side_effect=exc)
    fleet = MagicMock()
    fleet.get_raw_client = MagicMock(return_value=client)
    return fleet


def _recover(monkeypatch, exc):
    monkeypatch.setattr(dependencies, "get_fleet", lambda: _fleet_raising(exc))
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(patrol.recover_shelf(patrol.RecoverShelfRequest(shelf_id="S_04")))
    return excinfo.value


def test_recover_shelf_returns_503_when_robot_unreachable(monkeypatch):
    err = _recover(monkeypatch, UNAVAILABLE)

    assert err.status_code == 503
    assert "機器人失聯" in err.detail


def test_recover_shelf_keeps_500_for_real_errors(monkeypatch):
    err = _recover(monkeypatch, ValueError("shelf S_04 not registered"))

    assert err.status_code == 500
    assert "機器人失聯" not in str(err.detail)


# ── resume endpoint ──────────────────────────────────────────────────────────

def _resume(monkeypatch, exc):
    task = Task(
        task_id="t-drop", robot_id="kachaka", status=TaskStatus.SHELF_DROPPED,
        steps=[],
        metadata={
            "shelf_drop": True,
            "shelf_id": "S_04",
            "remaining_beds": [{"bed_key": "B_101-1", "location_id": "L1"}],
        },
    )
    patrol.tasks_db["t-drop"] = task
    monkeypatch.setattr(dependencies, "get_fleet", lambda: _fleet_raising(exc))
    try:
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(patrol.resume_patrol(patrol.ResumePatrolRequest(task_id="t-drop")))
    finally:
        patrol.tasks_db.pop("t-drop", None)
    return excinfo.value


def test_resume_returns_503_when_robot_unreachable(monkeypatch):
    """Same failure as recover-shelf, so the same answer — not a bare 500."""
    err = _resume(monkeypatch, UNAVAILABLE)

    assert err.status_code == 503
    assert "機器人失聯" in err.detail


def test_resume_keeps_500_for_real_errors(monkeypatch):
    err = _resume(monkeypatch, ValueError("shelf S_04 not registered"))

    assert err.status_code == 500
    assert "機器人失聯" not in str(err.detail)


# ── robot never registered (offline at app boot) ─────────────────────────────
# 2026-08-11 新營實測：機器人在 app 開機時已離線 → 註冊失敗 → RobotNotRegistered
# 走了裸 500 而非 503 人話。單機設備上「未註冊」唯一成因就是機器人不在線。

def test_recover_shelf_returns_503_when_robot_never_registered(monkeypatch):
    from services.fleet_api import RobotNotRegistered

    err = _recover(monkeypatch, RobotNotRegistered("Robot kachaka not registered"))

    assert err.status_code == 503
    assert "機器人失聯" in err.detail


def test_resume_returns_503_when_robot_never_registered(monkeypatch):
    from services.fleet_api import RobotNotRegistered

    err = _resume(monkeypatch, RobotNotRegistered("Robot kachaka not registered"))

    assert err.status_code == 503
    assert "機器人失聯" in err.detail
