"""TODO-020: a scheduled patrol that never starts must page someone.

The 23:00 run is unattended — an empty bed list, a flat battery or a still-live
task used to leave nothing but a log line nobody reads until morning. All three
non-start paths now emit an AnomalyEvent, so the notice reaches whichever of
Telegram / LINE / MQTT the site actually has enabled.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

import routers.patrol as patrol
from services.notifications import Source, dispatcher
from services.scheduler import scheduler_service


@pytest.fixture
def events(monkeypatch, tmp_path):
    """Capture dispatched events; point the schedule lookup at a 23:00 entry."""
    captured = []

    async def _fake_dispatch(event):
        captured.append(event)

    monkeypatch.setattr(dispatcher, "dispatch", _fake_dispatch)

    schedule_file = tmp_path / "schedule.json"
    schedule_file.write_text(json.dumps(
        {"schedules": [{"id": "night", "enabled": True, "time": "23:00", "type": "daily"}]}
    ))
    monkeypatch.setattr("settings.config.SCHEDULE_FILE", str(schedule_file))
    return captured


def _run():
    asyncio.run(scheduler_service._run_patrol("night"))


def _stub_start(monkeypatch, result=None, exc=None):
    async def _fake_start(req):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(patrol, "start_patrol", _fake_start)


def _only(events):
    assert len(events) == 1
    event = events[0]
    assert event.source == Source.SCHEDULE_NOT_RUN
    assert event.title == "⚠️ 排程巡房未啟動"
    assert "排程時間：23:00" in event.body
    return event


def test_battery_refusal_is_notified(events, monkeypatch):
    _stub_start(monkeypatch, exc=HTTPException(
        status_code=400, detail="Battery too low to start patrol: 25% < 30%"))

    _run()

    assert "原因：電量 25% 低於門檻 30%" in _only(events).body


def test_no_enabled_beds_is_notified(events, monkeypatch):
    _stub_start(monkeypatch, exc=HTTPException(
        status_code=400, detail="No enabled beds in patrol config"))

    _run()

    assert "原因：巡邏設定中沒有啟用的床位" in _only(events).body


def test_unknown_refusal_falls_back_to_raw_detail(events, monkeypatch):
    _stub_start(monkeypatch, exc=HTTPException(status_code=400, detail="Something new"))

    _run()

    assert "原因：Something new" in _only(events).body


def test_already_running_is_notified(events, monkeypatch):
    _stub_start(monkeypatch, result={"status": "already_running", "task_id": "T_1",
                                     "mode": "patrol", "beds_count": 3})

    _run()

    body = _only(events).body
    assert "已有巡房任務執行中" in body and "T_1" in body


def test_unexpected_error_is_notified(events, monkeypatch):
    """Sinks escape for their own wire format (TelegramSink is HTML mode), so the
    producer passes the raw exception text through."""
    _stub_start(monkeypatch, exc=RuntimeError("<AioRpcError UNAVAILABLE>"))

    _run()

    body = _only(events).body
    assert "未預期錯誤" in body and "<AioRpcError UNAVAILABLE>" in body


def test_successful_start_is_not_notified(events, monkeypatch):
    _stub_start(monkeypatch, result={"status": "ok", "task_id": "T_2",
                                     "mode": "patrol", "beds_count": 3})

    _run()

    assert events == []


def test_missing_schedule_entry_still_carries_a_time(events, monkeypatch):
    """A schedule deleted between trigger and run must not drop the notice."""
    _stub_start(monkeypatch, exc=HTTPException(status_code=400, detail="Something new"))

    asyncio.run(scheduler_service._run_patrol("unknown_id"))

    assert len(events) == 1
    assert events[0].body.startswith("排程時間：")
    assert "原因：Something new" in events[0].body


def test_dispatcher_failure_does_not_break_the_patrol_path(events, monkeypatch):
    """The notice is best-effort — a broken dispatcher must not turn a refusal
    into an unhandled error inside the scheduler job."""
    async def _boom(event):
        raise RuntimeError("dispatcher down")

    monkeypatch.setattr(dispatcher, "dispatch", _boom)
    _stub_start(monkeypatch, exc=HTTPException(status_code=400, detail="Something new"))

    _run()
