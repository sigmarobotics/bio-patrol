"""IT-23: a scheduled patrol refused for a silent / unreachable sensor pad is
retried instead of losing the whole night.

The pad runs on a power bank; a nurse swapping it 10 minutes after the 22:30
slot should still get a patrol. Retries are one-shot DateTrigger jobs named
patrol_<sid>_retry (so reload_from_json's patrol_ sweep covers them); once
they are used up the night is given up with an explicit notice. Every other
refusal (battery, no beds, already running) keeps today's single notice.
"""
import asyncio
import json
from datetime import datetime, timedelta

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from fastapi import HTTPException

import routers.patrol as patrol
from common_types import Task, TaskStatus, TaskStep
from services.bio_sensor_mqtt import SENSOR_OFFLINE_PREFIX, SENSOR_SILENT_PREFIX
from services.notifications import Source, dispatcher
from services.scheduler import MISFIRE_GRACE_SECONDS, TaskSchedulerService
from services.task_runtime import tasks_db

SILENT = f"{SENSOR_SILENT_PREFIX}：30 秒內沒有收到感測墊訊息，請檢查感測棚車上的行動電源是否沒電或沒接"
OFFLINE = f"{SENSOR_OFFLINE_PREFIX}：請檢查網路或憑證設定"


@pytest.fixture
def env(monkeypatch, tmp_path):
    captured = []

    async def _fake_dispatch(event):
        captured.append(event)

    monkeypatch.setattr(dispatcher, "dispatch", _fake_dispatch)

    schedule_file = tmp_path / "schedule.json"
    schedule_file.write_text(json.dumps(
        {"schedules": [{"id": "night", "enabled": True, "time": "22:30", "type": "daily"}]}
    ))
    monkeypatch.setattr("settings.config.SCHEDULE_FILE", str(schedule_file))

    state = {
        "events": captured,
        "settings": {"sensor_precheck_retry_count": 3, "sensor_precheck_retry_minutes": 10},
        "starts": 0,
        "exc": HTTPException(status_code=503, detail=SILENT),
        "svc": TaskSchedulerService(),
    }
    monkeypatch.setattr("settings.config.get_runtime_settings", lambda: dict(state["settings"]))

    async def _fake_start(req):
        state["starts"] += 1
        if state["exc"] is not None:
            raise state["exc"]
        return {"status": "ok", "task_id": "T_1", "mode": "patrol", "beds_count": 3}

    monkeypatch.setattr(patrol, "start_patrol", _fake_start)
    tasks_db.clear()
    yield state
    tasks_db.clear()


def _run(env, attempt=0):
    asyncio.run(env["svc"]._run_patrol("night", attempt))


def _jobs(env):
    return {j.id: j for j in env["svc"].scheduler.get_jobs()}


def test_silent_sensor_schedules_a_retry(env):
    before = datetime.now()
    _run(env)

    assert len(env["events"]) == 1
    event = env["events"][0]
    assert event.source == Source.SCHEDULE_NOT_RUN
    assert event.title == "🔌 感測器無回應，排程巡房未啟動"
    assert "排程時間：22:30" in event.body
    assert SILENT in event.body
    assert "10 分鐘後第 1/3 次重試" in event.body

    job = _jobs(env)["patrol_night_retry"]
    assert isinstance(job.trigger, DateTrigger)
    assert list(job.args) == ["night", 1]
    assert job.misfire_grace_time == MISFIRE_GRACE_SECONDS == 900
    run_at = job.trigger.run_date.replace(tzinfo=None)
    assert before + timedelta(minutes=10) <= run_at <= datetime.now() + timedelta(minutes=10)


def test_mqtt_offline_also_retries(env):
    env["exc"] = HTTPException(status_code=503, detail=OFFLINE)

    _run(env)

    assert OFFLINE in env["events"][0].body
    assert "patrol_night_retry" in _jobs(env)


def test_second_retry_counts_up(env):
    _run(env, attempt=1)

    assert "10 分鐘後第 2/3 次重試" in env["events"][0].body
    assert list(_jobs(env)["patrol_night_retry"].args) == ["night", 2]


def test_retries_used_up_gives_up_for_the_night(env):
    _run(env, attempt=3)

    assert len(env["events"]) == 1
    event = env["events"][0]
    assert event.title == "🔌 感測器仍無回應，今晚放棄巡房"
    assert "已重試 3 次" in event.body
    assert _jobs(env) == {}


def test_retry_count_zero_gives_up_at_once(env):
    env["settings"]["sensor_precheck_retry_count"] = 0

    _run(env)

    assert env["events"][0].title == "🔌 感測器仍無回應，今晚放棄巡房"
    assert _jobs(env) == {}


def test_interval_zero_is_clamped_to_one_minute(env):
    env["settings"]["sensor_precheck_retry_minutes"] = 0
    before = datetime.now()

    _run(env)

    assert "1 分鐘後第 1/3 次重試" in env["events"][0].body
    run_at = _jobs(env)["patrol_night_retry"].trigger.run_date.replace(tzinfo=None)
    assert before + timedelta(minutes=1) <= run_at <= datetime.now() + timedelta(minutes=1)


def test_missing_settings_use_defaults(env):
    env["settings"] = {}

    _run(env)

    assert "10 分鐘後第 1/3 次重試" in env["events"][0].body


def test_battery_refusal_is_not_retried(env):
    env["exc"] = HTTPException(status_code=400, detail="Battery too low to start patrol: 25% < 30%")

    _run(env)

    assert env["events"][0].title == "⚠️ 排程巡房未啟動"
    assert _jobs(env) == {}


def test_retry_entry_with_a_live_patrol_returns_quietly(env):
    tasks_db["live"] = Task(
        task_id="live", robot_id="kachaka",
        steps=[TaskStep(step_id="s", action="bio_scan", params={})],
        status=TaskStatus.IN_PROGRESS, metadata={"mode": "patrol"},
    )

    _run(env, attempt=1)

    assert env["starts"] == 0
    assert env["events"] == []
    assert _jobs(env) == {}


def test_retry_entry_without_a_live_patrol_starts(env):
    env["exc"] = None

    _run(env, attempt=2)

    assert env["starts"] == 1
    assert env["events"] == []


def test_reload_cancelling_a_pending_retry_is_notified(env):
    svc = env["svc"]

    async def _noop(*_args):
        pass

    svc.scheduler.add_job(_noop, DateTrigger(run_date=datetime.now() + timedelta(minutes=10)),
                          id="patrol_night_retry", args=["night", 1])

    asyncio.run(svc.reload_from_json())

    assert "patrol_night_retry" not in _jobs(env)
    assert isinstance(_jobs(env)["patrol_night"].trigger, CronTrigger)
    assert len(env["events"]) == 1
    assert "今晚待重試的巡房已取消" in env["events"][0].title


def test_reload_without_a_pending_retry_is_quiet(env):
    asyncio.run(env["svc"].reload_from_json())
    asyncio.run(env["svc"].reload_from_json())

    assert env["events"] == []
    assert list(_jobs(env)) == ["patrol_night"]


def test_retry_losing_the_race_to_a_manual_start_is_quiet(env, monkeypatch):
    """A manual start holding the start lock in its sensor gate has no task in
    tasks_db yet, so the retry's entry guard passes; start_patrol then reports
    already_running. Same silent drop as the entry guard — no 未啟動 alert in
    the middle of a running patrol."""
    async def _already_running(req):
        env["starts"] += 1
        return {"status": "already_running", "task_id": "T_manual", "mode": "patrol"}

    monkeypatch.setattr(patrol, "start_patrol", _already_running)

    _run(env, attempt=1)

    assert env["starts"] == 1
    assert env["events"] == []
    assert _jobs(env) == {}


def test_retry_blocked_by_a_non_patrol_run_is_reported(env, monkeypatch):
    """A cleanup (shelf return) or demo live at retry time is not the night's
    patrol, yet it ends the retry chain — the operator who was promised a
    retry gets told, instead of waiting on nothing."""
    async def _cleanup_running(req):
        env["starts"] += 1
        return {"status": "already_running", "task_id": "T_cleanup", "mode": "cleanup"}

    monkeypatch.setattr(patrol, "start_patrol", _cleanup_running)

    _run(env, attempt=1)

    assert env["starts"] == 1
    assert [e.title for e in env["events"]] == ["⚠️ 排程巡房未啟動"]
    assert "T_cleanup" in env["events"][0].body
    assert _jobs(env) == {}
