"""IT-22 (板榮): walk-only demo runs and per-bed vitals in the run summary.

A site without synthetic demo (``demo_synthetic`` off, the default) demos by
walking the route and waiting — no data, no alerts — and its summary must say
DEMO so nobody mistakes it for a night patrol. A patrol summary carries every
valid and restless bed's readings, which Telegram renders collapsed.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import routers.patrol as patrol
from common_types import StepAction, StepStatus, Task, TaskStatus, TaskStep
from routers.patrol import build_patrol_steps
from services import task_runtime
from services.bio_sensor_mqtt import BioSensorMQTTClient
from services.notifications.events import AnomalyEvent, Severity, Source, display_title
from services.notifications.recipients import StaticResolver
from services.notifications.sinks.mqtt import _serialize_event
from services.notifications.sinks.telegram import TelegramSink
from services.task_runtime import tasks_db
from settings.defaults import DEFAULT_SETTINGS

BEDS = [{"bed_key": "112-1", "location_id": "B_112-1"},
        {"bed_key": "112-2", "location_id": "B_112-2"}]


# ── step builder ─────────────────────────────────────────────────────────────

def test_walk_only_demo_waits_at_each_bed_and_parks_at_the_last():
    steps = build_patrol_steps(BEDS, shelf_id="S04", mode="demo",
                               final_wait_seconds=300, demo_synthetic=False)
    assert [s.action for s in steps] == [
        StepAction.RESET_SHELF_POSE.value,
        StepAction.MOVE_SHELF.value, StepAction.WAIT.value,
        StepAction.MOVE_SHELF.value, StepAction.WAIT.value,
        StepAction.RETURN_SHELF.value,
    ]
    waits = [s for s in steps if s.action == StepAction.WAIT.value]
    assert [s.params for s in waits] == [
        {"bed_key": "112-1", "seconds": 5},
        {"bed_key": "112-2", "seconds": 300},
    ]


def test_demo_synthetic_is_off_by_default():
    assert DEFAULT_SETTINGS["demo_synthetic"] is False


# ── start_patrol ─────────────────────────────────────────────────────────────

@pytest.fixture
def start(monkeypatch):
    settings = {"shelf_id": "S04", "demo_preset": "", "patrol_min_battery_pct": 0}
    submitted = []
    tasks_db.clear()
    monkeypatch.setattr(patrol, "get_runtime_settings", lambda: dict(settings))
    monkeypatch.setattr(
        patrol, "load_json",
        lambda path, default: (
            {"beds": {b["bed_key"]: {"location_id": b["location_id"]} for b in BEDS}}
            if "beds" in str(path)
            else {"beds_order": [{"bed_key": b["bed_key"], "enabled": True} for b in BEDS]}
        ),
    )
    monkeypatch.setattr(patrol, "_robot_offline", lambda cfg: False)

    async def _submit(task):
        submitted.append(task)

    monkeypatch.setattr(patrol, "submit_task", _submit)
    history = MagicMock()
    monkeypatch.setattr(patrol.demo_data, "ensure_history", history)
    yield settings, submitted, history
    tasks_db.clear()


def _start():
    return asyncio.run(patrol.start_patrol(patrol.PatrolStartRequest(mode="demo")))


def test_walk_only_demo_start_builds_waits_and_writes_no_demo_history(start):
    _, submitted, history = start
    _start()
    task = submitted[0]
    assert task.metadata == {"mode": "demo", "demo_synthetic": False}
    assert StepAction.DEMO_SCAN.value not in [s.action for s in task.steps]
    history.assert_not_called()


def test_synthetic_demo_start_keeps_the_it21_demo(start):
    settings, submitted, history = start
    settings["demo_synthetic"] = True
    _start()
    task = submitted[0]
    assert task.metadata == {"mode": "demo", "demo_synthetic": True}
    assert StepAction.DEMO_SCAN.value in [s.action for s in task.steps]
    history.assert_called_once()


# ── run summary ──────────────────────────────────────────────────────────────

def _fleet():
    fleet = MagicMock()
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={
        "poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0,
    })
    fleet.reset_metrics = AsyncMock(return_value=None)
    return fleet


@pytest.fixture
def captured(monkeypatch, tmp_path):
    sensor = BioSensorMQTTClient(db_path=str(tmp_path / "sensor_data.db"))
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: sensor)
    monkeypatch.setattr(task_runtime, "get_runtime_settings", lambda: dict(DEFAULT_SETTINGS))
    events = []

    async def _capture(event):
        events.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)
    return sensor, events


def _summary(events):
    found = [e for e in events if e.source == Source.TASK_SUMMARY]
    assert len(found) == 1
    return found[0]


def test_walk_only_demo_summary_lists_the_route_and_says_demo(captured):
    _, events = captured
    steps = build_patrol_steps(BEDS, shelf_id="S04", mode="demo",
                               final_wait_seconds=0, demo_synthetic=False)
    # Only the per-bed waits: the run's robot steps are out of scope here.
    waits = [s for s in steps if s.action == StepAction.WAIT.value]
    for w in waits:
        w.params["seconds"] = 0
    task = Task(task_id="20261012100000-walk01", robot_id="kachaka", steps=waits,
                status=TaskStatus.QUEUED,
                metadata={"mode": "demo", "demo_synthetic": False})
    asyncio.run(task_runtime.TaskEngine(_fleet(), "kachaka").run_task(task))

    summary = _summary(events)
    assert summary.demo is True and summary.demo_synthetic is False
    assert display_title(summary) == "🧪 DEMO ✅ 巡房完成"
    assert summary.body == "展示路線 2 床：112-1 → 112-2\n展示模式不量測"
    assert summary.readings is None


def test_walk_only_demo_summary_names_beds_not_reached(captured):
    _, events = captured
    steps = build_patrol_steps(BEDS, shelf_id="S04", mode="demo",
                               final_wait_seconds=0, demo_synthetic=False)
    # The moves and waits only: reset/return need the shelf machinery.
    run = [s for s in steps if s.action in (StepAction.MOVE_SHELF.value,
                                            StepAction.WAIT.value)]
    for s in run:
        if s.action == StepAction.WAIT.value:
            s.params["seconds"] = 0
    fleet = _fleet()
    fleet.move_shelf = AsyncMock(side_effect=[
        {"ok": True},
        {"ok": False, "error_code": 19004, "error": "unreachable"},
    ])
    task = Task(task_id="20261012100000-walk02", robot_id="kachaka", steps=run,
                status=TaskStatus.QUEUED,
                metadata={"mode": "demo", "demo_synthetic": False})
    asyncio.run(task_runtime.TaskEngine(fleet, "kachaka").run_task(task))

    assert _summary(events).body == (
        "展示路線 2 床：112-1 → 112-2\n機器人無法到位 1 床：112-2\n展示模式不量測"
    )


def test_patrol_summary_carries_valid_and_restless_readings(captured):
    sensor, events = captured
    tid = "20261012223000-pat001"
    sensor._save_scan_data(tid, {"location_id": "B_101-1", "bed_name": "101-1",
                                 "status": 4, "bpm": 58, "rpm": 13}, 0, True)
    sensor._save_scan_data(tid, {"location_id": "B_104-3", "bed_name": "104-3",
                                 "status": 2, "bpm": 78, "rpm": 20}, 4, False)
    sensor._save_scan_data(tid, {"location_id": "B_101-2", "bed_name": "101-2",
                                 "status": 2, "bpm": 0, "rpm": 0}, 4, False)
    sensor._save_scan_data(tid, {"location_id": "B_105-1", "bed_name": "105-1",
                                 "status": 0, "bpm": 0, "rpm": 0}, 4, False)
    task = Task(task_id=tid, robot_id="kachaka",
                steps=[TaskStep(step_id="s-1", action="wait", params={"seconds": "0"})],
                status=TaskStatus.QUEUED, metadata={"mode": "patrol"})
    asyncio.run(task_runtime.TaskEngine(_fleet(), "kachaka").run_task(task))

    summary = _summary(events)
    assert summary.demo is False
    assert summary.readings == {
        "valid": [["101-1", 58, 13]],
        "restless": [["101-2", 0, 0], ["104-3", 78, 20]],
    }
    # The text summary is unchanged by the readings.
    assert "躁動通報 2 床：101-2、104-3" in summary.body
    assert "無量測值 1 床：105-1" in summary.body


# ── Telegram rendering ───────────────────────────────────────────────────────

def _summary_event(**kw):
    return AnomalyEvent(severity=Severity.INFO, source=Source.TASK_SUMMARY,
                        title="✅ 巡房完成", body="本次巡房 3 床", **kw)


def test_telegram_renders_readings_in_a_collapsed_quote():
    rendered = TelegramSink(StaticResolver())._format(_summary_event(readings={
        "valid": [["101-1", 58, 13], ["101-3", 66, 20]],
        "restless": [["104-3", 78, 20], ["101-2", 0, 0]],
    }))
    assert rendered == (
        "<b>✅ 巡房完成</b>\n\n本次巡房 3 床"
        "\n\n<b>量測數值</b>（點開展開）\n<blockquote expandable>"
        "101-1　心跳 58　呼吸 13\n"
        "101-3　心跳 66　呼吸 20\n"
        "── 躁動 ──\n"
        "104-3　心跳 78　呼吸 20\n"
        "101-2　無讀值"
        "</blockquote>"
    )


@pytest.mark.parametrize("readings", [None, {"valid": [], "restless": []}])
def test_telegram_omits_the_block_without_readings(readings):
    rendered = TelegramSink(StaticResolver())._format(_summary_event(readings=readings))
    assert rendered == "<b>✅ 巡房完成</b>\n\n本次巡房 3 床"


def test_telegram_escapes_bed_names_inside_the_quote():
    rendered = TelegramSink(StaticResolver())._format(_summary_event(readings={
        "valid": [["A&B<1>", 70, 16]], "restless": [],
    }))
    assert "A&amp;B&lt;1&gt;　心跳 70　呼吸 16" in rendered


def test_synthetic_demo_keeps_its_title_prefix():
    e = _summary_event(demo=True)
    assert display_title(e) == "🧪 DEMO・合成資料 ✅ 巡房完成"


def test_mqtt_egress_wire_format_unchanged():
    payload = _serialize_event(_summary_event(readings={"valid": [["101-1", 58, 13]]}))
    assert "readings" not in payload
    assert "demo_synthetic" not in payload
