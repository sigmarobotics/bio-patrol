"""TODO-020: a scheduled patrol that never starts must page someone.

The 23:00 run is unattended — an empty bed list, a flat battery or a still-live
task used to leave nothing but a log line nobody reads until morning. All three
non-start paths now push a one-line Chinese notice through the notify hub.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

import routers.patrol as patrol
import services.telegram_service as telegram_service
from services.scheduler import scheduler_service


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """Capture notifications; point the schedule lookup at a 23:00 entry."""
    messages = []

    async def _fake_send(message, chat_id=None):
        messages.append(message)

    monkeypatch.setattr(telegram_service, "send_telegram_message", _fake_send)

    schedule_file = tmp_path / "schedule.json"
    schedule_file.write_text(json.dumps(
        {"schedules": [{"id": "night", "enabled": True, "time": "23:00", "type": "daily"}]}
    ))
    monkeypatch.setattr("settings.config.SCHEDULE_FILE", str(schedule_file))
    return messages


def _run():
    asyncio.run(scheduler_service._run_patrol("night"))


def _stub_start(monkeypatch, result=None, exc=None):
    async def _fake_start(req):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(patrol, "start_patrol", _fake_start)


def test_battery_refusal_is_notified(sent, monkeypatch):
    _stub_start(monkeypatch, exc=HTTPException(
        status_code=400, detail="Battery too low to start patrol: 25% < 30%"))

    _run()

    assert sent == ["⚠️ 23:00 排程巡房未啟動：電量 25% 低於門檻 30%"]


def test_no_enabled_beds_is_notified(sent, monkeypatch):
    _stub_start(monkeypatch, exc=HTTPException(
        status_code=400, detail="No enabled beds in patrol config"))

    _run()

    assert sent == ["⚠️ 23:00 排程巡房未啟動：巡邏設定中沒有啟用的床位"]


def test_unknown_refusal_falls_back_to_raw_detail(sent, monkeypatch):
    _stub_start(monkeypatch, exc=HTTPException(status_code=400, detail="Something new"))

    _run()

    assert sent == ["⚠️ 23:00 排程巡房未啟動：Something new"]


def test_already_running_is_notified(sent, monkeypatch):
    _stub_start(monkeypatch, result={"status": "already_running", "task_id": "T_1",
                                     "mode": "patrol", "beds_count": 3})

    _run()

    assert len(sent) == 1
    assert sent[0].startswith("⚠️ 23:00 排程巡房未啟動：已有巡房任務執行中")
    assert "T_1" in sent[0]


def test_unexpected_error_is_notified(sent, monkeypatch):
    """gRPC errors stringify as <AioRpcError ...>; the message goes out with
    parse_mode=HTML, so the angle brackets must be escaped or Telegram drops
    the whole notice."""
    _stub_start(monkeypatch, exc=RuntimeError("<AioRpcError UNAVAILABLE>"))

    _run()

    assert len(sent) == 1
    assert "未預期錯誤" in sent[0]
    assert "&lt;AioRpcError UNAVAILABLE&gt;" in sent[0]


def test_successful_start_is_not_notified(sent, monkeypatch):
    _stub_start(monkeypatch, result={"status": "ok", "task_id": "T_2",
                                     "mode": "patrol", "beds_count": 3})

    _run()

    assert sent == []


def test_missing_schedule_entry_still_carries_a_time(sent, monkeypatch):
    """A schedule deleted between trigger and run must not drop the notice."""
    _stub_start(monkeypatch, exc=HTTPException(status_code=400, detail="Something new"))

    asyncio.run(scheduler_service._run_patrol("unknown_id"))

    assert len(sent) == 1
    assert sent[0].startswith("⚠️ ") and "排程巡房未啟動：Something new" in sent[0]
