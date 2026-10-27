"""An out-of-range schedule time ("24:00", "22:60") must not take the other
patrols down with it.

CronTrigger rejects hour 24 / minute 60 by raising, and reload_from_json had
already removed every patrol_ job by then: the valid 22:00 entry vanished too,
and on startup the exception left lifespan, so the container restart-looped.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

import routers.schedule as schedule_router
from services.notifications import dispatcher
from services.scheduler import TaskSchedulerService, scheduler_service

BAD = {"id": "bad", "enabled": True, "time": "24:00", "type": "daily"}
GOOD = {"id": "night", "enabled": True, "time": "22:00", "type": "daily"}


@pytest.fixture
def events(monkeypatch):
    captured = []

    async def _fake_dispatch(event):
        captured.append(event)

    monkeypatch.setattr(dispatcher, "dispatch", _fake_dispatch)
    return captured


def test_reload_skips_out_of_range_time_and_keeps_valid_job(events, monkeypatch, tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps({"schedules": [dict(BAD), dict(GOOD)]}), encoding="utf-8")
    monkeypatch.setattr("settings.config.SCHEDULE_FILE", str(path))
    svc = TaskSchedulerService()

    asyncio.run(svc.reload_from_json())

    assert [j.id for j in svc.scheduler.get_jobs()] == ["patrol_night"]
    assert [e.raw["schedule_id"] for e in events] == ["bad"]
    assert events[0].title == "⚠️ 排程設定有誤，不會執行"


def test_save_rejects_out_of_range_time(events, monkeypatch, tmp_path):
    path = tmp_path / "schedule.json"
    original = json.dumps({"schedules": [dict(GOOD)]})
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(schedule_router, "SCHEDULE_FILE", str(path))
    reloads = []

    async def _reload():
        reloads.append(1)

    monkeypatch.setattr(scheduler_service, "reload_from_json", _reload)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(schedule_router.save_schedule(
            {"schedules": [{**GOOD, "time": "22:60"}]}))

    assert exc.value.status_code == 400
    assert "22:60" in exc.value.detail
    assert path.read_text(encoding="utf-8") == original
    assert reloads == [] and events == []


def test_save_ignores_bad_time_on_disabled_entry(events, monkeypatch, tmp_path):
    """A disabled legacy entry with a bad time never becomes a job, so it must
    not block saving changes to the rest of the list (code-review #54)."""
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps({"schedules": [dict(GOOD)]}), encoding="utf-8")
    monkeypatch.setattr(schedule_router, "SCHEDULE_FILE", str(path))

    async def _reload():
        pass

    monkeypatch.setattr(scheduler_service, "reload_from_json", _reload)

    body = {"schedules": [dict(GOOD), {"id": "legacy", "enabled": False, "time": "24:00", "type": "daily"}]}
    asyncio.run(schedule_router.save_schedule(body))

    assert json.loads(path.read_text(encoding="utf-8")) == body
