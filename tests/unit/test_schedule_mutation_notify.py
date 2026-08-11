"""TODO-020, the original incident: the 23:00 patrol was silently switched off
in the UI and nobody noticed until the next morning.

Turning a schedule off removes its APScheduler job, so `_run_patrol` — and every
notice inside it — never runs. The only moment anyone can be told is the save
itself, so POST/DELETE emit the notice, and a reload that fails to apply the new
schedule.json says so too.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

import routers.schedule as schedule_router
from services.notifications import Source
from services.notifications import dispatcher
from services.scheduler import scheduler_service

NIGHT = {"id": "night", "enabled": True, "time": "23:00", "type": "daily"}
MORNING = {"id": "morning", "enabled": True, "time": "08:00", "type": "daily"}


@pytest.fixture
def events(monkeypatch, tmp_path):
    """Capture dispatched events; isolate schedule.json and the live scheduler."""
    captured = []

    async def _fake_dispatch(event):
        captured.append(event)

    async def _noop_reload():
        pass

    monkeypatch.setattr(dispatcher, "dispatch", _fake_dispatch)
    monkeypatch.setattr(scheduler_service, "reload_from_json", _noop_reload)

    path = tmp_path / "schedule.json"
    path.write_text(json.dumps({"schedules": [dict(NIGHT), dict(MORNING)]}), encoding="utf-8")
    monkeypatch.setattr(schedule_router, "SCHEDULE_FILE", str(path))
    return captured


def _post(body: dict):
    return asyncio.run(schedule_router.save_schedule(body))


def _delete(schedule_id: str):
    return asyncio.run(schedule_router.delete_schedule_entry(schedule_id))


def test_disabling_a_schedule_is_notified(events):
    _post({"schedules": [{**NIGHT, "enabled": False}, dict(MORNING)]})

    assert len(events) == 1
    assert events[0].source == Source.SCHEDULE_NOT_RUN
    assert events[0].title == "🛑 排程巡房已關閉"
    assert "23:00" in events[0].body
    assert events[0].raw["schedule_id"] == "night"


def test_dropping_a_schedule_from_the_posted_list_is_notified(events):
    _post({"schedules": [dict(MORNING)]})

    assert [e.raw["schedule_id"] for e in events] == ["night"]


def test_saving_an_unchanged_schedule_is_quiet(events):
    _post({"schedules": [dict(NIGHT), dict(MORNING)]})

    assert events == []


def test_editing_the_time_is_quiet(events):
    """Still enabled, still scheduled — the job just moves."""
    _post({"schedules": [{**NIGHT, "time": "22:30"}, dict(MORNING)]})

    assert events == []


def test_adding_a_schedule_is_quiet(events):
    _post({"schedules": [dict(NIGHT), dict(MORNING),
                         {"id": "noon", "enabled": True, "time": "12:00", "type": "daily"}]})

    assert events == []


def test_deleting_an_enabled_schedule_is_notified(events):
    _delete("night")

    assert len(events) == 1
    assert events[0].title == "🛑 排程巡房已關閉"
    assert events[0].raw == {"schedule_id": "night", "time": "23:00"}


def test_deleting_an_already_disabled_schedule_is_quiet(events):
    """It was not going to run anyway — no news."""
    _post({"schedules": [{**NIGHT, "enabled": False}, dict(MORNING)]})
    events.clear()

    _delete("night")

    assert events == []


def test_deleting_an_unknown_schedule_notifies_nothing(events):
    with pytest.raises(HTTPException):
        _delete("nope")

    assert events == []


def test_unparseable_time_on_an_enabled_schedule_is_notified(events, monkeypatch, tmp_path):
    """Enabled in the UI but no job can be built — it would never fire, quietly."""
    from services.scheduler import TaskSchedulerService

    path = tmp_path / "bad-schedule.json"
    path.write_text(json.dumps(
        {"schedules": [{"id": "night", "enabled": True, "time": "23時", "type": "daily"}]}
    ), encoding="utf-8")
    monkeypatch.setattr("settings.config.SCHEDULE_FILE", str(path))

    asyncio.run(TaskSchedulerService().reload_from_json())

    assert [e.title for e in events] == ["⚠️ 排程設定有誤，不會執行"]


def test_failed_reload_is_notified(events, monkeypatch):
    """Saved but not applied — the UI shows the new schedule, the scheduler
    does not have it."""
    async def _boom():
        raise RuntimeError("scheduler gone")

    monkeypatch.setattr(scheduler_service, "reload_from_json", _boom)

    _post({"schedules": [dict(NIGHT), dict(MORNING)]})

    assert [e.title for e in events] == ["⚠️ 排程重新載入失敗"]
