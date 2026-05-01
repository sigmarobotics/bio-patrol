"""CORNER-004 — Telegram outbound failure must not crash the patrol.

Three layers cover this end-to-end:
  1. telegram_service.send_telegram_message catches httpx exceptions itself
     (defensive: any future direct caller still sees no crash).
  2. TelegramSink.send fans out via asyncio.gather(return_exceptions=True),
     so one chat failing does not poison the others.
  3. AnomalyDispatcher._safe_send wraps every sink.send in try/except, so
     even a TelegramSink that re-raises does not cancel the patrol task.

These are the three real production paths a Telegram outage can take.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from services import telegram_service
from services.notifications.dispatcher import AnomalyDispatcher
from services.notifications.events import AnomalyEvent, Severity, Source
from services.notifications.recipients import StaticResolver
from services.notifications.sinks.telegram import TelegramSink


def _patch_settings(**overrides):
    base = {
        "enable_telegram": True,
        "telegram_bot_token": "fake-token",
        "telegram_user_id": "111",
    }
    base.update(overrides)
    return patch("services.telegram_service.get_runtime_settings", return_value=base)


def _event():
    return AnomalyEvent(
        severity=Severity.WARN,
        source=Source.BIO_SCAN_FAILURE,
        title="t",
        body="b",
    )


def test_send_telegram_message_swallows_httpx_connect_error():
    """Network down — must log + return cleanly. No exception bubbles."""
    fake_client = MagicMock()
    fake_client.post = AsyncMock(side_effect=httpx.ConnectError("nxdomain"))
    with _patch_settings(), \
         patch("services.telegram_service._get_client", return_value=fake_client):
        # If this raised, patrol's finally block would mask the real exit reason.
        asyncio.run(telegram_service.send_telegram_message("hi"))


def test_send_telegram_message_swallows_500_response():
    """Telegram returns 500 — service logs warning, doesn't raise."""
    fake_resp = MagicMock()
    fake_resp.status_code = 500
    fake_resp.text = "internal server error"
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    with _patch_settings(), \
         patch("services.telegram_service._get_client", return_value=fake_client):
        asyncio.run(telegram_service.send_telegram_message("hi"))


def test_dispatcher_isolates_telegram_failure_from_other_sinks():
    """Even if the whole TelegramSink raises, MqttSink (or any other sink) keeps running."""
    other_sink_called = []

    class FailingTelegram:
        async def is_enabled(self):
            return True

        async def send(self, _e):
            raise httpx.ConnectError("network down")

    class HealthySink:
        async def is_enabled(self):
            return True

        async def send(self, _e):
            other_sink_called.append(True)

    async def _run():
        d = AnomalyDispatcher()
        d.register(FailingTelegram())
        d.register(HealthySink())
        await d.dispatch(_event())
        await d.drain(timeout=1.0)

    asyncio.run(_run())  # must complete without raising
    assert other_sink_called == [True]


def test_telegram_sink_partial_failure_does_not_block_remaining_chats():
    """gather(return_exceptions=True) — one chat fails, the others still get the message."""
    class TwoIds(StaticResolver):
        def __init__(self):
            pass
        async def resolve(self, _event, channel):
            return ["good", "bad"]

    sent_to: list[str] = []

    async def fake_send(message, chat_id):
        if chat_id == "bad":
            raise httpx.ConnectError("network down")
        sent_to.append(chat_id)

    sink = TelegramSink(TwoIds())
    with patch("services.notifications.sinks.telegram.send_telegram_message", side_effect=fake_send):
        asyncio.run(sink.send(_event()))
    assert sent_to == ["good"]
