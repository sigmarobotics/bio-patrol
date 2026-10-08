"""IT-21 ARCH-035: /api/demo/* — read only demo_data.db, the task store and the
robot connection state; never the real sensor DB."""
from __future__ import annotations

import random
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from common_types import StepAction, StepResult, StepStatus, Task, TaskStatus, TaskStep, get_now
from dependencies import get_fleet
from main import app
from services import demo_data, task_runtime
from services.notifications.events import AnomalyEvent, Severity, Source
from settings.defaults import DEFAULT_SETTINGS

RUN = "20261008100000-demo01"
CFG = dict(DEFAULT_SETTINGS)


def _no_sensor_db(*_a, **_k):
    raise AssertionError("demo API must not touch the real sensor DB")


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(demo_data, "DB_PATH", str(tmp_path / "demo_data.db"))
    monkeypatch.setattr("dependencies.get_bio_sensor_client", _no_sensor_db)
    monkeypatch.setattr(task_runtime, "get_bio_sensor_client", _no_sensor_db)
    monkeypatch.setattr("routers.demo.get_runtime_settings", lambda: dict(CFG))
    saved = dict(task_runtime.tasks_db)
    task_runtime.tasks_db.clear()
    fleet = MagicMock()
    fleet.get_connection_state_dict.return_value = {"state": "connected", "offline_pending": False}
    fleet.get_controller_state = AsyncMock(return_value={
        "pose_x": 1.0, "pose_y": 2.0, "pose_theta": 0.5, "last_updated": 123.0,
    })
    app.dependency_overrides[get_fleet] = lambda: fleet
    yield TestClient(app), fleet
    app.dependency_overrides.clear()
    task_runtime.tasks_db.clear()
    task_runtime.tasks_db.update(saved)


def _seed_run(task_id=RUN):
    rng = random.Random(7)
    for i, scenario in enumerate(["normal", "abnormal", "absent"]):
        rec = demo_data.synth_record(scenario, CFG, rng)
        demo_data.save_scan(task_id, f"S{i + 1}", f"loc-{i + 1}", scenario, rec, CFG)
    demo_data.save_notification(AnomalyEvent(
        severity=Severity.WARN, source=Source.VITALS_OUT_OF_BAND, title="⚠️ S2 心跳呼吸異常",
        body="床位：S2", bed_key="S2", task_id=task_id, demo=True))
    demo_data.save_notification(AnomalyEvent(
        severity=Severity.INFO, source=Source.TASK_SUMMARY, title="✅ 巡房完成",
        body="本次巡房 3 床", task_id=task_id, demo=True))


def test_live_idle_shows_the_last_demo_run_from_the_demo_db(env):
    client, _ = env
    _seed_run()
    body = client.get("/api/demo/live").json()
    assert body["task_id"] == RUN
    assert body["running"] is False
    assert [(s["bed_key"], s["state"]) for s in body["seats"]] == [
        ("S1", "normal"), ("S2", "abnormal"), ("S3", "absent"),
    ]
    assert body["seats"][1]["bpm"] > CFG["vitals_hr_high"]
    assert body["progress"] == {"done": 3, "total": 3}
    assert body["robot"]["connected"] is True
    assert body["robot"]["pose"] == {"x": 1.0, "y": 2.0, "theta": 0.5}
    assert body["thresholds"] == {"hr_low": 50, "hr_high": 120, "rr_low": 10, "rr_high": 30}


def test_live_with_nothing_ever_run(env):
    client, _ = env
    body = client.get("/api/demo/live").json()
    assert body["task_id"] is None
    assert body["seats"] == []
    assert body["running"] is False


def test_live_reports_a_disconnected_robot(env):
    client, fleet = env
    fleet.get_connection_state_dict.return_value = {"state": "disconnected", "offline_pending": True}
    body = client.get("/api/demo/live").json()
    assert body["robot"]["connected"] is False
    assert body["robot"]["state"] == "disconnected"


def test_live_overlays_a_running_demo_task(env):
    client, _ = env
    arrived = (get_now() - timedelta(seconds=4)).isoformat()
    task = Task(task_id="20261008110000-demo02", robot_id="kachaka", status=TaskStatus.IN_PROGRESS,
                metadata={"mode": "demo"}, steps=[
        TaskStep(step_id="move_0", action=StepAction.MOVE_SHELF.value,
                 params={"shelf_id": "S_04", "location_id": "loc-1"}, status=StepStatus.SUCCESS,
                 result=StepResult(success=True, timestamp=arrived)),
        TaskStep(step_id="action_0", action=StepAction.DEMO_SCAN.value, status=StepStatus.EXECUTING,
                 params={"bed_key": "S1", "location_id": "loc-1", "scenario": "normal", "seconds": 15}),
        TaskStep(step_id="move_1", action=StepAction.MOVE_SHELF.value,
                 params={"shelf_id": "S_04", "location_id": "loc-2"}),
        TaskStep(step_id="action_1", action=StepAction.DEMO_SCAN.value,
                 params={"bed_key": "S2", "location_id": "loc-2", "scenario": "abnormal", "seconds": 15}),
    ])
    task_runtime.tasks_db[task.task_id] = task
    _seed_run()  # an older finished run must not leak into the live one
    body = client.get("/api/demo/live").json()
    assert body["task_id"] == task.task_id
    assert body["running"] is True
    s1, s2 = body["seats"]
    assert s1["state"] == "measuring"
    assert s1["seconds"] == 15
    assert s1["started_at"] == arrived
    assert 9 <= s1["remaining_seconds"] <= 11.5
    assert s2["state"] == "pending"
    assert body["progress"] == {"done": 0, "total": 2}


def test_history_returns_synthetic_trend_and_average(env):
    client, _ = env
    demo_data.ensure_history(["S1", "S2"], CFG)
    _seed_run()
    body = client.get("/api/demo/history/S2").json()
    assert body["bed_key"] == "S2"
    assert len(body["points"]) == 31                     # 30 days + today's run
    valid = [p for p in body["points"] if p["is_valid"]]
    assert body["average"]["bpm"] == round(sum(p["bpm"] for p in valid) / len(valid), 1)
    assert body["average"]["rpm"] == round(sum(p["rpm"] for p in valid) / len(valid), 1)
    # Only today's point is out of band.
    assert [p["bpm"] > CFG["vitals_hr_high"] for p in body["points"]].count(True) == 1
    assert body["points"][-1]["bpm"] > CFG["vitals_hr_high"]


def test_history_of_unknown_seat_is_empty(env):
    client, _ = env
    body = client.get("/api/demo/history/NOPE").json()
    assert body["points"] == [] and body["average"] == {"bpm": None, "rpm": None}


def test_notifications_by_task_and_latest(env):
    client, _ = env
    _seed_run("20261008090000-old001")
    _seed_run()
    items = client.get(f"/api/demo/notifications?task_id={RUN}").json()["items"]
    assert [i["title"] for i in items] == [
        "🧪 DEMO・合成資料 ⚠️ S2 心跳呼吸異常", "🧪 DEMO・合成資料 ✅ 巡房完成",
    ]
    assert items[0]["source"] == "vitals_out_of_band"
    latest = client.get("/api/demo/notifications").json()
    assert latest["task_id"] == RUN and len(latest["items"]) == 2
