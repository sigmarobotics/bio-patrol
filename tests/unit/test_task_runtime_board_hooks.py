"""IT-25 — every point where a bed's result becomes final pushes one whiteboard
message: a finished scan (normal / restless / absent / no_reading) and every
skipped scan (unreachable). A scan cut short by a drop / low-battery abort
pushes nothing itself — the skip path that follows owns that bed. Demo runs
never push.

The real board_publisher gates run here (settings patched, a fake shared
client captures the wire calls), so these tests pin the whole hook chain.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common_types import StepAction, StepStatus, Task, TaskStatus, TaskStep
from services import task_runtime
from services.integration import board_publisher
from services.notifications.evaluator import ScanOutcome
from services.task_runtime import current_tasks, tasks_db


class _Shared:
    connected = True  # a live ZigbeeMQTT

    def __init__(self):
        self.calls = []

    async def publish(self, topic, payload, qos=0, retain=False):
        self.calls.append((topic, payload, qos, retain))
        return True


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    tasks_db.clear()
    current_tasks.clear()
    monkeypatch.setattr(task_runtime, "get_runtime_settings", lambda: {})
    yield
    tasks_db.clear()
    current_tasks.clear()
    board_publisher.configure(None)


@pytest.fixture
def board(monkeypatch):
    cfg = {"whiteboard_enabled": True, "whiteboard_site_id": "banqiao-1f",
           "whiteboard_bed_id_map": {}}
    monkeypatch.setattr(board_publisher, "get_runtime_settings", lambda: cfg)
    shared = _Shared()
    board_publisher.configure(shared)
    return shared


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", AsyncMock(return_value=None))


def _published(shared):
    """[(bed, status, bpm, rpm)] in publish order."""
    return [(p["bed"], p["status"], p["bpm"], p["rpm"]) for _, p, _, _ in shared.calls]


# ── 1. finished scan (_do_bio_scan) ───────────────────────────────────

class _Client:
    def __init__(self, outcome):
        self.outcome = outcome
        self.saved = []

    async def get_valid_scan_data(self, target_bed=None, task_id=None, bed_name=None):
        o = self.outcome
        return o() if callable(o) else o

    def _save_scan_data(self, task_id, data, retry_count, is_valid):
        self.saved.append(data)


def _outcome(valid=None, last=None, seen=40):
    return ScanOutcome(
        task_id="t-1", location_id="L", bed_name="101-1", valid_record=valid,
        retry_count=2, last_record_raw=last,
        last_failure_reason=None if valid else "x", messages_seen=seen,
    )


def _scan(monkeypatch, outcome, *, demo=False):
    client = _Client(outcome)
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: client)
    eng = task_runtime.TaskEngine(MagicMock(), "kachaka")
    eng.current_task_id = "t-1"
    eng.target_bed = "L_101-1"
    eng._board_demo = demo
    step = TaskStep(step_id="s0", action=StepAction.BIO_SCAN.value,
                    params={"bed_key": "101-1"}, status=StepStatus.PENDING)

    async def _go():
        eng.shelf_drop_event = asyncio.Event()
        eng._battery_abort_event = asyncio.Event()
        if callable(outcome):
            client.outcome = outcome(eng)
        return await eng._do_bio_scan(step)
    return asyncio.run(_go())


def test_valid_scan_pushes_normal(monkeypatch, board):
    rec = {"status": 4, "bpm": 58, "rpm": 13}
    _scan(monkeypatch, _outcome(valid=rec, last=rec))
    assert _published(board) == [("101-1", "normal", 58, 13)]
    topic, _, qos, retain = board.calls[0]
    assert topic == "bio-patrol/board/banqiao-1f/bed/101-1"
    assert qos == 1 and retain is True


def test_restless_scan_pushes_last_raw_values(monkeypatch, board):
    _scan(monkeypatch, _outcome(last={"status": 2, "bpm": 74, "rpm": 19}))
    assert _published(board) == [("101-1", "restless", 74, 19)]


def test_absent_scan_pushes_absent(monkeypatch, board):
    _scan(monkeypatch, _outcome(last={"status": 0, "bpm": 0, "rpm": 0}))
    assert _published(board) == [("101-1", "absent", None, None)]


def test_no_data_scan_pushes_no_reading(monkeypatch, board):
    _scan(monkeypatch, _outcome(last=None, seen=0))
    assert _published(board) == [("101-1", "no_reading", None, None)]


def test_invalid_status4_scan_pushes_no_reading(monkeypatch, board):
    _scan(monkeypatch, _outcome(last={"status": 4, "bpm": 0, "rpm": 12}))
    assert _published(board) == [("101-1", "no_reading", None, None)]


def test_scan_cut_by_drop_pushes_nothing(monkeypatch, board):
    def _cut(eng):
        eng.shelf_drop_event.set()
        return None
    result = _scan(monkeypatch, _cut)
    assert result.data.get("interrupted_by") == "shelf_release"
    assert board.calls == []


def test_scan_cut_by_low_battery_pushes_nothing(monkeypatch, board):
    def _cut(eng):
        eng._battery_abort_event.set()
        return None
    _scan(monkeypatch, _cut)
    assert board.calls == []


def test_demo_flag_blocks_scan_push(monkeypatch, board):
    rec = {"status": 4, "bpm": 58, "rpm": 13}
    _scan(monkeypatch, _outcome(valid=rec, last=rec), demo=True)
    assert board.calls == []


def test_scan_that_raises_pushes_no_reading_and_still_raises(monkeypatch, board):
    # e.g. sqlite "database is locked" inside get_valid_scan_data: the bed's
    # result is final (failed step), so the board must not keep last night's row.
    import sqlite3

    def _raise(eng):
        def _r():
            raise sqlite3.OperationalError("database is locked")
        return _r
    with pytest.raises(sqlite3.OperationalError):
        _scan(monkeypatch, _raise)
    assert _published(board) == [("101-1", "no_reading", None, None)]


def test_publisher_failure_does_not_break_scan(monkeypatch, board):
    async def _boom(*a, **k):
        raise RuntimeError("broker gone")
    monkeypatch.setattr(board, "publish", _boom)
    rec = {"status": 4, "bpm": 58, "rpm": 13}
    result = _scan(monkeypatch, _outcome(valid=rec, last=rec))
    assert result.success is True


# ── 2. move failed → conditional skip (run_task) ──────────────────────

def _fleet():
    fleet = MagicMock()
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={
        "poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0,
    })
    fleet.reset_metrics = AsyncMock(return_value=None)
    fleet.move_to_location = AsyncMock(return_value={
        "ok": False, "error": "obstacle", "error_code": 19004,
    })
    return fleet


def _skip_task(mode=None):
    return Task(
        task_id="t-skip", robot_id="kachaka", status=TaskStatus.QUEUED,
        metadata={"mode": mode} if mode else {},
        steps=[
            TaskStep(step_id="move_0", action=StepAction.MOVE_TO_LOCATION.value,
                     params={"location_id": "L_101-1"}, status=StepStatus.PENDING,
                     skip_on_failure=["scan_0"]),
            TaskStep(step_id="scan_0", action=StepAction.BIO_SCAN.value,
                     params={"bed_key": "101-1", "location_id": "L_101-1"},
                     status=StepStatus.PENDING),
        ],
    )


def _run(monkeypatch, task):
    client = _Client(None)
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: client)
    eng = task_runtime.TaskEngine(_fleet(), "kachaka")
    tasks_db[task.task_id] = task
    asyncio.run(eng.run_task(task))
    return client


def test_move_failure_pushes_unreachable(monkeypatch, board):
    client = _run(monkeypatch, _skip_task())
    assert [d["status"] for d in client.saved] == ["N/A"]
    assert _published(board) == [("101-1", "unreachable", None, None)]


def test_demo_run_never_pushes(monkeypatch, board):
    _run(monkeypatch, _skip_task(mode="demo"))
    assert board.calls == []


# ── 3. drop → whole remaining batch skipped (_handle_shelf_drop) ──────

def _drop_task():
    task = Task(
        task_id="t-drop", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
        steps=[
            TaskStep(step_id=f"scan_{i}", action=StepAction.BIO_SCAN.value,
                     params={"bed_key": f"101-{i}", "location_id": f"L_101-{i}"},
                     status=StepStatus.PENDING)
            for i in (1, 2, 3)
        ],
    )
    tasks_db[task.task_id] = task
    current_tasks["kachaka"] = task.task_id
    return task


def _drop(monkeypatch, *, demo=False):
    client = _Client(None)
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: client)
    fleet = MagicMock()
    fleet.cancel_command = AsyncMock(return_value=None)
    fleet.return_home = AsyncMock(return_value=None)
    eng = task_runtime.TaskEngine(fleet, "kachaka")
    eng._verify_shelf_still_docked = AsyncMock(return_value="released")
    eng._board_demo = demo
    task = _drop_task()
    eng.current_task_id = task.task_id
    with patch.object(eng, "_query_shelf_pose", new=AsyncMock(return_value={})), \
         patch.object(eng, "_collect_remaining_beds", return_value=["101-1", "101-2", "101-3"]):
        asyncio.run(eng._handle_shelf_drop(task, step_index=0))
    return task, client


def test_drop_pushes_unreachable_for_every_skipped_bed(monkeypatch, board):
    task, client = _drop(monkeypatch)
    assert all(s.status == StepStatus.SKIPPED for s in task.steps)
    assert len(client.saved) == 3
    assert sorted(_published(board)) == [
        ("101-1", "unreachable", None, None),
        ("101-2", "unreachable", None, None),
        ("101-3", "unreachable", None, None),
    ]


def test_drop_in_demo_pushes_nothing(monkeypatch, board):
    _drop(monkeypatch, demo=True)
    assert board.calls == []


# ── 3b. scan cut twice by false drop signals (run_task) ───────────────

def test_scan_cut_twice_by_false_signal_pushes_unreachable(monkeypatch, board):
    """A false release signal cuts the scan, it is re-run once, the second
    cut records the bed as skipped — that skip is unreachable on the board."""
    task = Task(
        task_id="t-false", robot_id="kachaka", status=TaskStatus.QUEUED,
        steps=[TaskStep(step_id="scan_0", action=StepAction.BIO_SCAN.value,
                        params={"bed_key": "101-1"}, status=StepStatus.PENDING)],
    )
    eng = task_runtime.TaskEngine(_fleet(), "kachaka")

    def _cut():
        eng.shelf_drop_event.set()
        return None

    client = _Client(_cut)
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: client)

    async def _false_signal(task_, step_index, trigger_step=None):
        eng.shelf_drop_event.clear()
        return False

    eng._handle_shelf_drop = _false_signal
    tasks_db[task.task_id] = task
    asyncio.run(eng.run_task(task))
    assert [d["details"] for d in client.saved] == ["量測兩度被誤判的掉落訊號中斷"]
    assert _published(board) == [("101-1", "unreachable", None, None)]
