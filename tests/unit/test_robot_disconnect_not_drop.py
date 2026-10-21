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
from common_types import Task, TaskStatus, TaskStep, StepAction, StepStatus
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
# raw get_moving_shelf_id answers used by the verify reads
DOCKED = "S_04"
NO_SHELF = ""


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

def _watcher_engine(responses, *, final="S_04", verify_reads=None):
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
    # The watcher asks the robot (raw client) before raising; by default it
    # cannot get through (the signal stands as "unreachable").
    verify_client = MagicMock()
    verify_client.get_moving_shelf_id = MagicMock(
        side_effect=UNAVAILABLE if verify_reads is None else list(verify_reads) * 8
    )
    fleet.get_raw_client = MagicMock(return_value=verify_client)
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
    eng = _watcher_engine(["S_04", "S_04", None, None], verify_reads=[NO_SHELF])

    await eng._watch_shelf_state()

    assert eng.shelf_drop_event.is_set()
    assert eng._disconnect_suspected is False


@pytest.mark.asyncio
async def test_watcher_blip_with_no_shelf_carried_is_nothing_to_report(no_sleep):
    """A blip during the final return_home (shelf already placed) that ends
    with the robot answering: "released" only says it carries none now."""
    eng = _watcher_engine([None, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, None],
                          verify_reads=[NO_SHELF, NO_SHELF])

    await eng._watch_shelf_state()

    assert not eng.shelf_drop_event.is_set()
    assert eng._disconnect_suspected is False


@pytest.mark.asyncio
async def test_watcher_blip_with_the_shelf_carried_and_then_none_is_a_drop(no_sleep):
    eng = _watcher_engine(["S_04", UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, None],
                          verify_reads=[NO_SHELF, NO_SHELF])

    await eng._watch_shelf_state()

    assert eng.shelf_drop_event.is_set()
    assert eng._disconnect_suspected is False


@pytest.mark.asyncio
async def test_watcher_ignores_an_empty_read_the_robot_denies(no_sleep):
    """A transient empty moving-shelf read: asked again, the robot still
    carries the shelf, so nothing is raised."""
    eng = _watcher_engine(["S_04", "S_04", None, "S_04"], verify_reads=[DOCKED])

    await eng._watch_shelf_state()

    assert not eng.shelf_drop_event.is_set()


@pytest.mark.asyncio
async def test_watcher_keeps_watching_after_a_denied_empty_read(no_sleep):
    """The denied transient must not blind the watcher: the shelf really
    falling on the next poll is still a drop."""
    eng = _watcher_engine(["S_04", "S_04", None, None, None], final=None,
                          verify_reads=[DOCKED, NO_SHELF, NO_SHELF])

    await eng._watch_shelf_state()

    assert eng.shelf_drop_event.is_set()


@pytest.mark.asyncio
async def test_watcher_ignores_a_blip_the_robot_outlives(no_sleep):
    """板橋 2026-10-19: three failed polls, then the robot answers with the
    shelf still docked — no pause, no suspicion."""
    eng = _watcher_engine(["S_04", UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, "S_04"],
                          verify_reads=[UNAVAILABLE, DOCKED])

    await eng._watch_shelf_state()

    assert not eng.shelf_drop_event.is_set()
    assert eng._disconnect_suspected is False


# ── drop handler ─────────────────────────────────────────────────────────────

def _engine(battery, command_state, *, shelf_pose=None, controller_state=None,
            verify="released"):
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
    # The handler first asks the robot whether it still carries the shelf;
    # "released" is the classic drop these tests were written for.
    eng._verify_shelf_still_docked = AsyncMock(return_value=verify)
    return eng


def _moved():
    """A completed move_shelf: the run is carrying the shelf."""
    return TaskStep(step_id="m0", action=StepAction.MOVE_SHELF,
                    params={"shelf_id": "S_04", "location_id": "B_101-1"},
                    status=StepStatus.SUCCESS, skip_on_failure=["s1"])


def _task():
    return Task(
        task_id="t-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[_moved(),
               TaskStep(step_id="s1", action=StepAction.BIO_SCAN,
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
    asyncio.run(eng._handle_shelf_drop(task, 1))
    assert len(events) == 1
    return task, events[0]


def test_unreachable_robot_reports_disconnect_not_drop(monkeypatch):
    eng = _engine(UNREACHABLE_READ, UNREACHABLE_READ, verify="unreachable")

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
    """Still unreachable at the handler's verify: the watcher's suspicion
    carries into classification even if a flapping link lets one of the
    two classify reads through — 失聯 pause, not a CRITICAL drop."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="unreachable")
    eng._disconnect_suspected = True

    task, event = _run_drop(monkeypatch, eng)

    assert event.severity == Severity.WARN
    assert task.metadata["disconnect"] is True
    eng.fleet.return_home.assert_not_awaited()


def test_a_robot_that_answers_the_verify_drops_the_suspicion(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="released")
    eng._disconnect_suspected = True

    task, event = _run_drop(monkeypatch, eng)

    assert event.severity == Severity.CRITICAL
    assert task.metadata["disconnect"] is False


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
    eng = _engine(UNREACHABLE_READ, UNREACHABLE_READ, verify="unreachable")

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
    eng = _engine(UNREACHABLE_READ, UNREACHABLE_READ, verify="unreachable")

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


# ── false release signal: the robot still carries the shelf ──────────────────
#
# 板橋 2026-10-19: a 4-second WiFi blip raised the watcher's disconnect pause;
# the robot answered again before the run consumed it, the handler read a
# healthy robot and reported a CRITICAL 貨架掉落 with the shelf still docked.
# return_home then failed 11501 「車上有家具不能充電」 and the robot sat in
# the corridor all night. The handler now asks the robot first.

def _verify_engine(reads):
    """Engine whose raw client answers get_moving_shelf_id from ``reads``."""
    client = MagicMock()
    client.get_moving_shelf_id = MagicMock(side_effect=list(reads))
    fleet = MagicMock()
    fleet.get_raw_client = MagicMock(return_value=client)
    return task_runtime.TaskEngine(fleet, "kachaka")


@pytest.mark.asyncio
async def test_verify_rides_out_a_blip(no_sleep):
    eng = _verify_engine([UNAVAILABLE, DOCKED])
    assert await eng._verify_shelf_still_docked() == "docked"


@pytest.mark.asyncio
async def test_verify_needs_two_empty_reads_for_a_release(no_sleep):
    """One empty moving-shelf read is the transient the watcher can be fooled
    by; the shelf reappearing on the next read is not a release."""
    eng = _verify_engine([NO_SHELF, DOCKED])
    assert await eng._verify_shelf_still_docked() == "docked"
    eng = _verify_engine([NO_SHELF, NO_SHELF])
    assert await eng._verify_shelf_still_docked() == "released"


@pytest.mark.asyncio
async def test_verify_empty_then_silent_is_not_a_release(no_sleep):
    """One empty read followed by a blip is a pause, not a drop."""
    eng = _verify_engine([NO_SHELF] + [UNAVAILABLE] * (task_runtime.SHELF_VERIFY_POLLS - 1))
    assert await eng._verify_shelf_still_docked() == "unreachable"


@pytest.mark.asyncio
async def test_verify_empty_reads_must_be_consecutive(no_sleep):
    eng = _verify_engine([NO_SHELF, UNAVAILABLE, NO_SHELF])
    assert await eng._verify_shelf_still_docked() == "unreachable"


@pytest.mark.asyncio
async def test_verify_gives_up_on_a_robot_that_never_answers(no_sleep):
    eng = _verify_engine([UNAVAILABLE] * task_runtime.SHELF_VERIFY_POLLS)
    assert await eng._verify_shelf_still_docked() == "unreachable"


@pytest.mark.asyncio
async def test_watcher_counts_three_fresh_failures_after_a_withdrawn_pause(no_sleep):
    """The handler verifies the shelf through its own reads and withdraws the
    suspicion; the watcher's next failed poll must not re-raise the pause by
    itself (its streak was still >= 3 before the reset)."""
    eng = _watcher_engine(["S_04", UNAVAILABLE, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE])
    raw_next = eng.fleet._robots["kachaka"].conn.client.get_moving_shelf_id.side_effect
    served = {"n": 0}

    def _next():
        served["n"] += 1
        if served["n"] == 5:
            # the handler verified "docked" in between
            eng.shelf_drop_event.clear()
            eng._disconnect_suspected = False
        return raw_next()

    eng.fleet._robots["kachaka"].conn.client.get_moving_shelf_id.side_effect = _next

    await eng._watch_shelf_state()

    assert not eng.shelf_drop_event.is_set()
    assert eng._disconnect_suspected is False


# ── _await_or_drop: a raised signal still cuts the call short ────────────────

@pytest.mark.asyncio
async def test_await_or_drop_still_cuts_a_real_release_short():
    eng = task_runtime.TaskEngine(MagicMock(), "kachaka")
    eng.shelf_drop_event = asyncio.Event()
    eng._battery_abort_event = asyncio.Event()

    async def scan():
        await asyncio.sleep(1.0)
        return "reading"

    async def trip():
        await asyncio.sleep(0.01)
        eng.shelf_drop_event.set()

    asyncio.ensure_future(trip())
    assert await eng._await_or_drop(scan()) is None
    assert eng.shelf_drop_event.is_set()


def test_false_release_signal_resumes_the_run(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="docked")
    eng.shelf_drop_event = asyncio.Event()
    eng.shelf_drop_event.set()
    eng._disconnect_suspected = True
    events = []

    async def _capture(event):
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)
    task = _task()

    handled = asyncio.run(eng._handle_shelf_drop(task, 0))

    assert handled is False
    assert events == []
    assert not eng.shelf_drop_event.is_set()
    assert eng._disconnect_suspected is False
    assert task.status == TaskStatus.IN_PROGRESS
    eng.fleet.cancel_command.assert_not_awaited()
    eng.fleet.return_home.assert_not_awaited()


def test_a_real_drop_is_handled_and_reported(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="released")

    task, event = _run_drop(monkeypatch, eng)

    assert event.severity == Severity.CRITICAL
    assert task.status == TaskStatus.SHELF_DROPPED
    # once on entry, once after return_home
    assert eng._verify_shelf_still_docked.await_count == 2


def test_stale_unreachable_suspicion_yields_to_a_robot_that_answers(monkeypatch):
    """The watcher raised while the robot was unreachable; by the step
    boundary it answers again carrying the shelf — resume, not 失聯."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="docked")
    eng._disconnect_suspected = True
    eng.shelf_drop_event = asyncio.Event()
    eng.shelf_drop_event.set()
    events = []

    async def _capture(event):
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)

    assert asyncio.run(eng._handle_shelf_drop(_task(), 0)) is False
    assert events == []
    assert eng._disconnect_suspected is False


def test_drop_metadata_keeps_the_run_mode(monkeypatch):
    """A cleanup run's "mode" must survive the drop metadata, or a retracted
    drop (CANCELLED) makes run_task queue another cleanup, forever."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="released")
    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", AsyncMock())
    monkeypatch.setattr(task_runtime.TaskEngine, "_record_skipped_scan",
                        lambda self, *a, **kw: None)
    task = _task()
    task.metadata = {"mode": "cleanup"}

    asyncio.run(eng._handle_shelf_drop(task, 1))

    assert task.metadata["mode"] == "cleanup"
    assert task.metadata["shelf_drop"] is True


def test_return_shelf_trigger_is_not_reverified(monkeypatch):
    """A failed placement with the shelf still on the robot is its own
    problem — _do_return_shelf already verified it, so no second opinion."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    # one answer only: the post-return_home read. An entry verify would
    # exhaust the script.
    eng._verify_shelf_still_docked = AsyncMock(side_effect=["released"])
    events = []

    async def _capture(event):
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)
    monkeypatch.setattr(task_runtime.TaskEngine, "_record_skipped_scan",
                        lambda self, *a, **kw: None)
    task = _task()
    trigger = TaskStep(step_id="r1", action=StepAction.RETURN_SHELF,
                       params={"shelf_id": "S_04"})

    handled = asyncio.run(eng._handle_shelf_drop(task, 0, trigger_step=trigger))

    assert handled is True
    assert eng._verify_shelf_still_docked.await_count == 1
    assert len(events) == 1


def _two_bed_task():
    return Task(
        task_id="t-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[_moved(),
               TaskStep(step_id="s1", action=StepAction.BIO_SCAN,
                        params={"bed_key": "B_101-1"}),
               TaskStep(step_id="s2", action=StepAction.BIO_SCAN,
                        params={"bed_key": "B_101-2"})],
    )


def _run_drop_all(monkeypatch, eng, task, step_index=1, trigger_step=None):
    events, skipped = [], []

    async def _capture(event):
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)
    monkeypatch.setattr(task_runtime.TaskEngine, "_record_skipped_scan",
                        lambda self, step, details, **kw: skipped.append(details))
    monkeypatch.setattr(task_runtime.TaskEngine, "_unrecord_skipped_scans",
                        lambda self, beds, details: skipped.append(("unrecord", tuple(beds), details)))
    handled = asyncio.run(eng._handle_shelf_drop(task, step_index, trigger_step=trigger_step))
    return events, skipped, handled


def test_firmware_refusing_return_home_retracts_the_drop(monkeypatch):
    """11501/11019 is the firmware saying the shelf is still docked, whatever
    the reads said (2026-10-02: reboot, reads empty, shelf still latched):
    the run resumes, the ward is told, 續巡 no longer offers this run."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    eng.fleet.return_home = AsyncMock(return_value={
        "ok": False, "error_code": 11501, "error": "error_code=11501: Charging canceled",
    })
    eng._verify_shelf_still_docked = AsyncMock(side_effect=["released", "released"])
    task = _two_bed_task()

    events, skipped, handled = _run_drop_all(monkeypatch, eng, task)

    assert handled is False
    assert [e.severity for e in events] == [Severity.CRITICAL, Severity.WARN]
    assert events[1].raw["false_drop"] is True
    assert "仍在機器人身上" in events[1].title
    assert "巡房繼續" in events[1].body
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.metadata["shelf_drop"] is False
    assert task.metadata["remaining_beds"] == []
    # the N/A rows were written before return_home (durable), then removed
    # and the steps put back to PENDING so the resumed run takes real readings
    assert skipped == ["貨架掉落，巡房中斷"] * 2 + [
        ("unrecord", ("B_101-1", "B_101-2"), "貨架掉落，巡房中斷")]
    # the scans back to PENDING, and the run re-drives from their move
    assert [st.status for st in task.steps] == [StepStatus.PENDING] * 3
    assert eng._resume_step_index == 0


def test_shelf_still_docked_after_return_home_retracts_the_drop(monkeypatch):
    """With 「握持家具時充電」 ON return_home succeeds carrying the shelf —
    the robot's own read, not the refusal, is what decides."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    eng._verify_shelf_still_docked = AsyncMock(side_effect=["released", "docked"])
    task = _two_bed_task()

    events, skipped, handled = _run_drop_all(monkeypatch, eng, task)

    assert handled is False
    assert [e.severity for e in events] == [Severity.CRITICAL, Severity.WARN]
    assert task.status == TaskStatus.IN_PROGRESS
    assert [st.status for st in task.steps] == [StepStatus.PENDING] * 3


def test_a_retracted_drop_redrives_from_the_beds_move(monkeypatch):
    """return_home may have moved the robot (to the charger, or toward it
    before a 11501 refusal): the run re-drives from the bed's move_shelf."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    eng._verify_shelf_still_docked = AsyncMock(side_effect=["released", "docked"])
    task = Task(
        task_id="t-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[TaskStep(step_id="m1", action=StepAction.MOVE_SHELF,
                        params={"shelf_id": "S_04", "location_id": "B_101-1"},
                        status=StepStatus.SUCCESS, skip_on_failure=["s1"]),
               TaskStep(step_id="s1", action=StepAction.BIO_SCAN,
                        params={"bed_key": "B_101-1"})],
    )

    events, skipped, handled = _run_drop_all(monkeypatch, eng, task, step_index=1)

    assert handled is False
    assert eng._resume_step_index == 0
    assert task.steps[0].status == StepStatus.PENDING
    assert task.steps[1].status == StepStatus.PENDING
    assert "重新前往該床" in events[1].body


def test_a_retracted_drop_with_every_bed_measured_does_not_redrive(monkeypatch):
    """Loop-top signal on the final return_shelf step, nothing put back:
    the run resumes where it was instead of revisiting a measured bed."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    eng._verify_shelf_still_docked = AsyncMock(side_effect=["released", "docked"])
    task = Task(
        task_id="t-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[TaskStep(step_id="m1", action=StepAction.MOVE_SHELF,
                        params={"shelf_id": "S_04", "location_id": "B_101-1"},
                        status=StepStatus.SUCCESS),
               TaskStep(step_id="s1", action=StepAction.BIO_SCAN,
                        params={"bed_key": "B_101-1"}, status=StepStatus.SUCCESS),
               TaskStep(step_id="r1", action=StepAction.RETURN_SHELF,
                        params={"shelf_id": "S_04"})],
    )

    events, skipped, handled = _run_drop_all(monkeypatch, eng, task, step_index=2)

    assert handled is False
    assert eng._resume_step_index is None
    assert task.steps[1].status == StepStatus.SUCCESS
    assert "重新前往" not in events[1].body


def test_a_third_retraction_in_a_run_leaves_the_drop_standing(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    eng._verify_shelf_still_docked = AsyncMock(side_effect=["released", "docked"])
    eng._false_drops = 2
    task = _two_bed_task()

    events, skipped, handled = _run_drop_all(monkeypatch, eng, task)

    assert handled is True
    assert task.status == TaskStatus.SHELF_DROPPED
    assert [e.severity for e in events] == [Severity.CRITICAL]


def test_a_scan_trigger_reports_the_bed_it_is_parked_at(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="released")
    eng.target_bed = "B_104-2"
    task = _two_bed_task()
    trigger = task.steps[1]

    events, skipped, handled = _run_drop_all(monkeypatch, eng, task, trigger_step=trigger)

    assert events[0].bed_key == "B_104-2"
    assert task.metadata["bed_key"] == "B_104-2"


def test_a_new_drop_after_a_retraction_is_not_labelled_false(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="released")
    task = _two_bed_task()
    task.metadata = {"mode": "patrol", "false_drop": True}

    _run_drop_all(monkeypatch, eng, task)

    assert "false_drop" not in task.metadata
    assert task.metadata["shelf_drop"] is True


def test_failed_placement_with_the_shelf_still_latched_ends_for_cleanup(monkeypatch):
    """A return_shelf trigger: resuming would end the run 完成 with the shelf
    on the robot, so the run ends CANCELLED and the wrap-up's cleanup parks
    the shelf."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    eng.fleet.return_home = AsyncMock(return_value={
        "ok": False, "error_code": 11501, "error": "error_code=11501: Charging canceled",
    })
    # only the post-return_home read: a return_shelf trigger is not re-verified
    eng._verify_shelf_still_docked = AsyncMock(side_effect=["released"])
    events = []

    async def _capture(event):
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)
    monkeypatch.setattr(task_runtime.TaskEngine, "_record_skipped_scan",
                        lambda self, *a, **kw: None)
    task = _task()
    trigger = TaskStep(step_id="r1", action=StepAction.RETURN_SHELF,
                       params={"shelf_id": "S_04"})

    handled = asyncio.run(eng._handle_shelf_drop(task, 0, trigger_step=trigger))

    assert handled is True
    assert task.status == TaskStatus.CANCELLED
    assert task.metadata["shelf_drop"] is False
    assert [e.severity for e in events] == [Severity.CRITICAL, Severity.WARN]
    assert "收工流程" in events[1].body


def test_retraction_yields_to_an_operator_stop(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    task = _two_bed_task()

    async def _verify():
        if eng._verify_shelf_still_docked.await_count == 2:
            task.status = TaskStatus.CANCELLED   # 停止 pressed during return_home
            return "docked"
        return "released"

    eng._verify_shelf_still_docked = AsyncMock(side_effect=_verify)

    events, skipped, handled = _run_drop_all(monkeypatch, eng, task)

    assert handled is True
    assert task.status == TaskStatus.CANCELLED
    assert [e.severity for e in events] == [Severity.CRITICAL]
    # the shelf is on the robot: resume-latest must not offer this run
    assert task.metadata["shelf_drop"] is False
    assert task.metadata["remaining_beds"] == []


def test_cancel_during_the_verify_window_stands(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    task = _two_bed_task()

    async def _verify():
        task.status = TaskStatus.CANCELLED
        return "released"

    eng._verify_shelf_still_docked = AsyncMock(side_effect=_verify)

    events, skipped, handled = _run_drop_all(monkeypatch, eng, task)

    assert handled is True
    assert events == []
    assert task.status == TaskStatus.CANCELLED


def test_shelf_already_placed_means_nothing_to_report(monkeypatch):
    """Blip on the final return_home, shelf already placed by the run's own
    return_shelf: the robot comes back carrying none — that is not a drop."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="released")
    task = Task(
        task_id="t-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[TaskStep(step_id="m1", action=StepAction.MOVE_SHELF,
                        params={"shelf_id": "S_04", "location_id": "B_101-1"},
                        status=StepStatus.SUCCESS),
               TaskStep(step_id="r1", action=StepAction.RETURN_SHELF,
                        params={"shelf_id": "S_04"}, status=StepStatus.SUCCESS),
               TaskStep(step_id="h1", action=StepAction.RETURN_HOME, params={})],
    )
    events = []

    async def _capture(event):
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)

    handled = asyncio.run(eng._handle_shelf_drop(task, 2))

    assert handled is False
    assert events == []
    assert task.status == TaskStatus.IN_PROGRESS


def test_release_before_the_first_dock_is_nothing_to_report(monkeypatch):
    """Outage during the approach to the first dock: the robot comes back
    carrying none — the shelf is still at home, not dropped."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="released")
    task = Task(
        task_id="t-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[TaskStep(step_id="m0", action=StepAction.MOVE_SHELF,
                        params={"shelf_id": "S_04", "location_id": "B_101-1"},
                        status=StepStatus.FAIL),
               TaskStep(step_id="s1", action=StepAction.BIO_SCAN,
                        params={"bed_key": "B_101-1"})],
    )
    events, skipped, handled = _run_drop_all(monkeypatch, eng, task, step_index=1)

    assert handled is False
    assert events == []


def test_a_move_shelf_trigger_always_counts_as_carrying(monkeypatch):
    """The controller's monitor fired inside the first move_shelf: the dock
    happened, the run's steps just do not show it yet."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="released")
    task = Task(
        task_id="t-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[TaskStep(step_id="m0", action=StepAction.MOVE_SHELF,
                        params={"shelf_id": "S_04", "location_id": "B_101-1"},
                        skip_on_failure=["s1"]),
               TaskStep(step_id="s1", action=StepAction.BIO_SCAN,
                        params={"bed_key": "B_101-1"})],
    )
    events, skipped, handled = _run_drop_all(monkeypatch, eng, task, step_index=0,
                                             trigger_step=task.steps[0])

    assert handled is True
    assert [e.severity for e in events] == [Severity.CRITICAL]


def test_shelf_picked_up_again_after_placing_still_reports(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="released")
    task = Task(
        task_id="t-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[TaskStep(step_id="r1", action=StepAction.RETURN_SHELF,
                        params={"shelf_id": "S_04"}, status=StepStatus.SUCCESS),
               TaskStep(step_id="m1", action=StepAction.MOVE_SHELF,
                        params={"shelf_id": "S_04", "location_id": "B_101-1"},
                        status=StepStatus.SUCCESS),
               TaskStep(step_id="s1", action=StepAction.BIO_SCAN,
                        params={"bed_key": "B_101-1"})],
    )
    events, skipped, handled = _run_drop_all(monkeypatch, eng, task, step_index=2)

    assert handled is True
    assert [e.severity for e in events] == [Severity.CRITICAL]


def test_other_return_home_failures_leave_the_drop_standing(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD, verify="released")
    eng.fleet.return_home = AsyncMock(return_value={
        "ok": False, "error_code": 11502, "error": "error_code=11502: Failed to charge",
    })

    task = _two_bed_task()
    events, skipped, handled = _run_drop_all(monkeypatch, eng, task)

    assert handled is True
    assert [e.severity for e in events] == [Severity.CRITICAL]
    assert task.status == TaskStatus.SHELF_DROPPED
    assert task.metadata["shelf_drop"] is True
    # loop-top signal: the pending scan at step_index is unprocessed too
    assert skipped == ["貨架掉落，巡房中斷"] * 2
    assert task.steps[1].status == StepStatus.SKIPPED
    assert task.steps[2].status == StepStatus.SKIPPED


def test_a_cleanup_run_retries_its_own_placement(monkeypatch):
    """Nobody queues a cleanup for a cleanup: a retracted placement failure
    re-runs its return_shelf (bounded by the retraction budget)."""
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    eng.fleet.return_home = AsyncMock(return_value={
        "ok": False, "error_code": 11501, "error": "error_code=11501: Charging canceled",
    })
    eng._verify_shelf_still_docked = AsyncMock(side_effect=["released"])
    task = Task(
        task_id="c-1", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        metadata={"mode": "cleanup"},
        steps=[TaskStep(step_id="cleanup_return_shelf", action=StepAction.RETURN_SHELF,
                        params={"shelf_id": "S_04"}),
               TaskStep(step_id="cleanup_return_home", action=StepAction.RETURN_HOME, params={})],
    )
    events, skipped, handled = _run_drop_all(monkeypatch, eng, task, step_index=0,
                                             trigger_step=task.steps[0])

    assert handled is False
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.metadata["mode"] == "cleanup"
    assert eng._resume_step_index == 0
    assert task.steps[0].status == StepStatus.PENDING
    assert "重試貨架歸位" in events[1].body


def test_a_low_battery_pause_is_not_resumed_by_a_docked_shelf(monkeypatch):
    low = {"ok": True, "percentage": 8.0, "power_status": "2"}
    returning = {"ok": True, "state": "2", "command": "return_home_command", "is_running": True}
    eng = _engine(low, returning)
    eng._verify_shelf_still_docked = AsyncMock(side_effect=["released", "docked"])
    task = _two_bed_task()

    events, skipped, handled = _run_drop_all(monkeypatch, eng, task)

    assert task.metadata["low_battery"] is True
    assert handled is True
    assert task.status == TaskStatus.CANCELLED


def test_a_refusal_from_a_robot_that_then_vanishes_leaves_the_drop_standing(monkeypatch):
    eng = _engine(HEALTHY_BATTERY, IDLE_CMD)
    eng.fleet.return_home = AsyncMock(return_value={
        "ok": False, "error_code": 11501, "error": "error_code=11501: Charging canceled",
    })
    eng._verify_shelf_still_docked = AsyncMock(side_effect=["released", "unreachable"])
    task = _two_bed_task()

    events, skipped, handled = _run_drop_all(monkeypatch, eng, task)

    assert handled is True
    assert task.status == TaskStatus.SHELF_DROPPED
