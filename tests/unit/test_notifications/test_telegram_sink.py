"""Unit tests for TelegramSink."""
from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock

from services.notifications.events import AnomalyEvent, Severity, Source
from services.notifications.recipients import StaticResolver
from services.notifications.sinks.telegram import TelegramSink


def _event():
    return AnomalyEvent(
        severity=Severity.WARN,
        source=Source.BIO_SCAN_FAILURE,
        title="⚠️ 101-1 量測失敗",
        body="床位：101-1\n原因：無有效量測數值\n重試次數：19\n最後一筆：status=2, bpm=0, rpm=0",
        bed_key="101-1",
        task_id="task-1",
    )


def test_format_wraps_html_and_appends_event_id_footer():
    sink = TelegramSink(StaticResolver())
    e = _event()
    rendered = sink._format(e)
    assert rendered.startswith("<b>⚠️ 101-1 量測失敗</b>\n\n")
    assert e.body in rendered
    assert f"<code>{e.event_id[-8:]}</code>" in rendered


def test_is_enabled_reads_settings():
    sink = TelegramSink(StaticResolver())
    with patch("services.notifications.sinks.telegram.get_runtime_settings",
               return_value={"enable_telegram": True}):
        assert asyncio.run(sink.is_enabled()) is True
    with patch("services.notifications.sinks.telegram.get_runtime_settings",
               return_value={"enable_telegram": False}):
        assert asyncio.run(sink.is_enabled()) is False


def test_send_no_recipients_makes_no_api_call():
    sink = TelegramSink(StaticResolver())
    with patch("services.notifications.recipients.get_runtime_settings",
               return_value={"telegram_user_id": ""}), \
         patch("services.notifications.sinks.telegram.send_telegram_message",
               new_callable=AsyncMock) as mock_send:
        asyncio.run(sink.send(_event()))
        mock_send.assert_not_awaited()


def test_send_one_recipient_one_call():
    sink = TelegramSink(StaticResolver())
    with patch("services.notifications.recipients.get_runtime_settings",
               return_value={"telegram_user_id": "111"}), \
         patch("services.notifications.sinks.telegram.send_telegram_message",
               new_callable=AsyncMock) as mock_send:
        asyncio.run(sink.send(_event()))
        assert mock_send.await_count == 1
        args, kwargs = mock_send.await_args
        assert kwargs["chat_id"] == "111"
        assert "<b>⚠️ 101-1 量測失敗</b>" in args[0]


def test_send_multiple_recipients_one_call_each():
    """Future-proof: when ShiftBasedResolver returns N chat_ids, sink fires N posts."""
    class TwoIds:
        async def resolve(self, event, channel):
            return ["111", "222"]
    sink = TelegramSink(TwoIds())
    with patch("services.notifications.sinks.telegram.send_telegram_message",
               new_callable=AsyncMock) as mock_send:
        asyncio.run(sink.send(_event()))
        chat_ids = [call.kwargs["chat_id"] for call in mock_send.await_args_list]
        assert chat_ids == ["111", "222"]


def test_format_skips_body_separator_when_body_empty():
    sink = TelegramSink(StaticResolver())
    e = AnomalyEvent(
        severity=Severity.CRITICAL,
        source=Source.SHELF_DROP,
        title="⚠️ 貨架掉落，請協助歸位",
        body="",
    )
    rendered = sink._format(e)
    # No double-blank line between title and footer when body is empty
    assert "\n\n\n" not in rendered
    assert rendered.startswith("<b>⚠️ 貨架掉落，請協助歸位</b>\n\n")
    assert rendered.endswith(f"<code>{e.event_id[-8:]}</code>")
