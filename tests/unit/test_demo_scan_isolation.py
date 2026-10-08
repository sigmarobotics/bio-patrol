"""IT-21 CORNER-063: synthetic demo data never reaches sensor_data.db.

The demo scan goes through the real evaluators and the real dispatcher, but
its rows land only in demo_data.db — the dashboard and history views (which
read sensor_scan_data) must not see a single demo reading.
"""
from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from common_types import StepAction, StepStatus, Task, TaskStatus, TaskStep
from services import demo_data, task_runtime
from services.bio_sensor_mqtt import BioSensorMQTTClient
from services.notifications.events import Source
from settings.defaults import DEFAULT_SETTINGS


def _count(path, table, task_id=None):
    conn = sqlite3.connect(path)
    if task_id is None:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    else:
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE task_id = ?", (task_id,)).fetchone()[0]
    conn.close()
    return n


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Real sensor DB (with one pre-existing row) + temp demo DB + captured dispatch."""
    sensor = BioSensorMQTTClient(db_path=str(tmp_path / "sensor_data.db"))
    sensor._save_scan_data("real-task", {"location_id": "loc", "bed_name": "101-1",
                                         "status": 4, "bpm": 70, "rpm": 16}, 0, True)
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", lambda: sensor)
    demo_path = str(tmp_path / "demo_data.db")
    monkeypatch.setattr(demo_data, "DB_PATH", demo_path)
    # The switch stays OFF: the demo must evaluate vitals regardless.
    monkeypatch.setattr(task_runtime, "get_runtime_settings",
                        lambda: {**DEFAULT_SETTINGS, "vitals_alert_enabled": False})
    captured = []

    async def _capture(event):
        captured.append(event)

    monkeypatch.setattr(task_runtime.dispatcher, "dispatch", _capture)
    return sensor.db_path, demo_path, captured


def _engine(task_id="20261008100000-demo01"):
    engine = task_runtime.TaskEngine(MagicMock(), "kachaka")
    engine.shelf_drop_event = asyncio.Event()
    engine.current_task_id = task_id
    return engine


def _scan(i, scenario, seconds=0):
    return TaskStep(step_id=f"action_{i}", action=StepAction.DEMO_SCAN.value,
                    params={"bed_key": f"S{i + 1}", "location_id": f"loc-{i + 1}",
                            "scenario": scenario, "seconds": seconds})


def test_three_demo_scans_touch_only_the_demo_db(env):
    sensor_path, demo_path, _ = env
    engine = _engine()

    async def _run():
        return [await engine._do_demo_scan(_scan(i, sc))
                for i, sc in enumerate(["normal", "abnormal", "absent"])]

    results = asyncio.run(_run())

    assert _count(sensor_path, "sensor_scan_data") == 1          # unchanged
    assert _count(demo_path, "demo_scan_data", engine.current_task_id) == 3
    assert [r.success for r in results] == [True, True, False]


def test_demo_scans_run_the_real_evaluators_and_mark_events_demo(env):
    _, _, captured = env
    engine = _engine()

    async def _run():
        for i, sc in enumerate(["normal", "abnormal", "absent"]):
            await engine._do_demo_scan(_scan(i, sc))

    asyncio.run(_run())

    assert [(e.source, e.bed_key) for e in captured] == [
        (Source.VITALS_OUT_OF_BAND, "S2"),
        (Source.BIO_SCAN_FAILURE, "S3"),
    ]
    assert all(e.demo for e in captured)
    assert "偵測不到人" in captured[1].body
    assert all(e.task_id == engine.current_task_id for e in captured)


def test_demo_scan_interrupted_by_a_shelf_drop_writes_nothing(env):
    _, demo_path, captured = env
    engine = _engine()

    async def _run():
        engine.shelf_drop_event.set()
        return await engine._do_demo_scan(_scan(0, "abnormal", seconds=30))

    result = asyncio.run(_run())
    assert result.success is False
    assert demo_data.scan_rows(engine.current_task_id) == []
    assert captured == []


def _fleet():
    fleet = MagicMock()
    fleet.get_shelves = AsyncMock(return_value={"ok": True, "shelves": []})
    fleet.get_locations = AsyncMock(return_value={"ok": True, "locations": []})
    fleet.get_metrics = AsyncMock(return_value={
        "poll_count": 0, "poll_rtt_list": [], "poll_success_count": 0,
    })
    fleet.reset_metrics = AsyncMock(return_value=None)
    return fleet


def test_demo_run_summary_counts_demo_scans_from_the_demo_db(env):
    sensor_path, _, captured = env
    task = Task(
        task_id="20261008100000-demo02", robot_id="kachaka",
        steps=[_scan(i, sc) for i, sc in enumerate(["normal", "abnormal", "absent"])],
        status=TaskStatus.QUEUED, metadata={"mode": "demo"},
    )
    engine = task_runtime.TaskEngine(_fleet(), "kachaka")
    result = asyncio.run(engine.run_task(task))

    assert result.status == TaskStatus.DONE
    assert [s.status for s in result.steps] == [
        StepStatus.SUCCESS, StepStatus.SUCCESS, StepStatus.FAIL,
    ]
    summary = [e for e in captured if e.source == Source.TASK_SUMMARY]
    assert len(summary) == 1
    assert summary[0].demo is True
    assert "本次巡房 3 床" in summary[0].body
    assert "正常量測 2 床" in summary[0].body
    assert "無量測值 1 床：S3" in summary[0].body
    assert _count(sensor_path, "sensor_scan_data") == 1


def test_patrol_run_summary_is_not_marked_demo(env):
    _, _, captured = env
    task = Task(
        task_id="20261008100000-pat001", robot_id="kachaka",
        steps=[TaskStep(step_id="s-1", action="wait", params={"seconds": "0"})],
        status=TaskStatus.QUEUED, metadata={"mode": "patrol"},
    )
    asyncio.run(task_runtime.TaskEngine(_fleet(), "kachaka").run_task(task))
    summary = [e for e in captured if e.source == Source.TASK_SUMMARY]
    assert len(summary) == 1 and summary[0].demo is False
