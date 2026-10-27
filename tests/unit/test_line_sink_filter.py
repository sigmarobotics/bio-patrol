"""F-56 / BIZ-019: LINE gets an allow-listed subset of sources, and a 429
(push quota) is logged at ERROR with a once-a-month Telegram notice."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import line_service
from services.notifications.events import AnomalyEvent, Source
from services.notifications.sinks import line as line_sink
from services.notifications.sinks.line import LineSink


class _Resolver:
    async def resolve(self, event, channel):
        return ["Cgroup"]


def _send(event, settings):
    sent = AsyncMock(return_value=True)
    with patch.object(line_sink, "get_runtime_settings", return_value=settings), \
         patch.object(line_sink, "send_line_message", sent):
        asyncio.run(LineSink(_Resolver()).send(event))
    return sent


@pytest.mark.parametrize("source", [
    Source.TASK_SUMMARY, Source.ROBOT_OFFLINE, Source.ROBOT_RECOVERED,
    Source.SHELF_DROP, Source.SCHEDULE_NOT_RUN, Source.VITALS_OUT_OF_BAND,
])
def test_default_allow_list_sends(source):
    sent = _send(AnomalyEvent(source=source, title="t"), {})
    sent.assert_awaited_once()


@pytest.mark.parametrize("source", [
    Source.BIO_SCAN_FAILURE, Source.LOW_BATTERY_ABORT, Source.SENSOR_SILENT_ABORT,
])
def test_default_allow_list_filters(source):
    sent = _send(AnomalyEvent(source=source, title="t"), {})
    sent.assert_not_awaited()


def test_settings_line_sources_overrides_default():
    sent = _send(AnomalyEvent(source=Source.BIO_SCAN_FAILURE, title="t"),
                 {"line_sources": ["bio_scan_failure"]})
    sent.assert_awaited_once()
    sent = _send(AnomalyEvent(source=Source.TASK_SUMMARY, title="t"),
                 {"line_sources": ["bio_scan_failure"]})
    sent.assert_not_awaited()


@pytest.fixture
def _reset_quota_marker():
    line_service._quota_notice_month = None
    yield
    line_service._quota_notice_month = None


def _post_429(month_str, tg):
    resp = MagicMock(status_code=429, text="You have reached your monthly limit.")
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    now = MagicMock()
    now.strftime.return_value = month_str
    with patch("services.line_service.get_runtime_settings",
               return_value={"line_channel_access_token": "tok"}), \
         patch("services.line_service._get_client", return_value=client), \
         patch("common_types.get_now", return_value=now), \
         patch("services.telegram_service.send_telegram_message", tg):
        return asyncio.run(line_service.send_line_message("hi", to="Cgid"))


def test_429_logs_error_and_notifies_telegram_once_per_month(_reset_quota_marker, caplog):
    tg = AsyncMock()
    with caplog.at_level(logging.ERROR, logger="services.line_service"):
        assert _post_429("2026-10", tg) is False
    assert any(r.levelno == logging.ERROR and "429" in r.getMessage()
               for r in caplog.records)
    tg.assert_awaited_once_with(line_service._QUOTA_NOTICE)
    assert tg.await_args.args[0] == "LINE 推播額度用罄，院方 LINE 通報已停止，請改看 Telegram"

    assert _post_429("2026-10", tg) is False
    tg.assert_awaited_once()  # same month: no second notice

    assert _post_429("2026-11", tg) is False
    assert tg.await_count == 2  # new month: notice again


def test_429_telegram_failure_never_raises(_reset_quota_marker):
    tg = AsyncMock(side_effect=RuntimeError("tg down"))
    assert _post_429("2026-10", tg) is False


def test_quota_notice_retries_until_delivered(_reset_quota_marker):
    """A LINE-only site (Telegram off → None) or a failed send must not burn
    the month's one notice (code-review #56)."""
    tg_off = AsyncMock(return_value=None)
    _post_429("2026-11", tg_off)
    assert line_service._quota_notice_month is None

    tg_fail = AsyncMock(return_value={"ok": False, "status": 400, "description": "chat not found"})
    _post_429("2026-11", tg_fail)
    assert line_service._quota_notice_month is None

    tg_ok = AsyncMock(return_value={"ok": True, "status": 200, "description": "OK"})
    _post_429("2026-11", tg_ok)
    assert line_service._quota_notice_month == "2026-11"
    _post_429("2026-11", tg_ok)
    assert tg_ok.await_count == 1
