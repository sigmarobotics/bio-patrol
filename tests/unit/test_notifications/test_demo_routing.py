"""IT-21 FEAT-026 / CORNER-064: demo events reach Telegram (prefixed, optional
demo bot/chat) — never MQTT egress or LINE."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from services import telegram_service
from services.notifications.dispatcher import AnomalyDispatcher
from services.notifications.events import AnomalyEvent, Severity, Source
from services.notifications.sinks.line import LineSink
from services.notifications.sinks.mqtt import MqttSink
from services.notifications.sinks.telegram import TelegramSink

PREFIX = "🧪 DEMO・合成資料"


class _Static:
    def __init__(self, ids):
        self.ids = ids

    async def resolve(self, event, channel):
        return list(self.ids)


def _event(demo=True):
    return AnomalyEvent(
        severity=Severity.WARN, source=Source.VITALS_OUT_OF_BAND,
        title="⚠️ S2 心跳呼吸異常", body="床位：S2\n心跳：132 次／分",
        bed_key="S2", task_id="20261008100000-demo01", demo=demo,
    )


def _tg_settings(**overrides):
    base = {"enable_telegram": True, "demo_telegram_bot_token": "",
            "demo_telegram_chat_id": ""}
    base.update(overrides)
    return patch("services.notifications.sinks.telegram.get_runtime_settings",
                 return_value=base)


# ── Telegram ────────────────────────────────────────────────────────────────

def test_demo_title_carries_the_prefix():
    sink = TelegramSink(_Static(["111"]))
    assert sink._format(_event()).startswith(f"<b>{PREFIX} ⚠️ S2 心跳呼吸異常</b>")
    assert PREFIX not in sink._format(_event(demo=False))


def test_demo_event_uses_the_demo_bot_and_chat():
    sink = TelegramSink(_Static(["111"]))
    with _tg_settings(demo_telegram_bot_token="999:DEMOTOKEN", demo_telegram_chat_id="-100demo"), \
         patch("services.notifications.sinks.telegram.send_telegram_message",
               new_callable=AsyncMock) as send:
        asyncio.run(sink.send(_event()))
    assert send.await_count == 1
    args, kwargs = send.await_args
    assert kwargs == {"chat_id": "-100demo", "bot_token": "999:DEMOTOKEN"}
    assert PREFIX in args[0]


def test_demo_event_without_demo_settings_falls_back_to_the_real_bot_and_chat():
    sink = TelegramSink(_Static(["111"]))
    with _tg_settings(), \
         patch("services.notifications.sinks.telegram.send_telegram_message",
               new_callable=AsyncMock) as send:
        asyncio.run(sink.send(_event()))
    assert send.await_args.kwargs == {"chat_id": "111", "bot_token": None}


def test_real_event_ignores_the_demo_bot_and_chat():
    sink = TelegramSink(_Static(["111"]))
    with _tg_settings(demo_telegram_bot_token="999:DEMOTOKEN", demo_telegram_chat_id="-100demo"), \
         patch("services.notifications.sinks.telegram.send_telegram_message",
               new_callable=AsyncMock) as send:
        asyncio.run(sink.send(_event(demo=False)))
    assert send.await_args.kwargs == {"chat_id": "111"}


def test_bot_token_override_goes_straight_to_telegram_not_the_hub():
    resp = MagicMock(status_code=200, text="")
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    cfg = {"enable_telegram": True, "telegram_bot_token": "REAL:TOKEN",
           "telegram_user_id": "111", "notify_hub_url": "https://hub.example",
           "notify_hub_token": "hubtok"}
    with patch("services.telegram_service.get_runtime_settings", return_value=cfg), \
         patch("services.telegram_service._get_client", return_value=client):
        asyncio.run(telegram_service.send_telegram_message(
            "hi", chat_id="-100demo", bot_token="999:DEMOTOKEN"))
    args, kwargs = client.post.await_args
    assert args[0] == "https://api.telegram.org/bot999:DEMOTOKEN/sendMessage"
    assert kwargs["json"]["chat_id"] == "-100demo"


# ── MQTT / LINE skip ────────────────────────────────────────────────────────

def test_mqtt_sink_skips_demo_events():
    shared = MagicMock()
    shared.publish = AsyncMock(return_value=True)
    sink = MqttSink(zigbee_mqtt=shared)
    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value={"enable_mqtt_egress": True}):
        asyncio.run(sink.send(_event()))
        shared.publish.assert_not_awaited()
        asyncio.run(sink.send(_event(demo=False)))
        shared.publish.assert_awaited_once()


def test_line_sink_skips_demo_events():
    sink = LineSink(_Static(["Cgroup"]))
    with patch("services.notifications.sinks.line.send_line_message",
               new_callable=AsyncMock) as send:
        asyncio.run(sink.send(_event()))
        send.assert_not_awaited()
        asyncio.run(sink.send(_event(demo=False)))
        send.assert_awaited_once()


# ── Dispatcher fan-out ──────────────────────────────────────────────────────

def test_dispatcher_fans_a_demo_event_to_telegram_only():
    shared = MagicMock()
    shared.publish = AsyncMock(return_value=True)
    d = AnomalyDispatcher()
    d.register(TelegramSink(_Static(["111"])))
    d.register(LineSink(_Static(["Cgroup"])))
    d.register(MqttSink(zigbee_mqtt=shared))

    async def _run():
        await d.dispatch(_event())
        await d.drain(timeout=2.0)

    with _tg_settings(), \
         patch("services.notifications.sinks.line.get_runtime_settings",
               return_value={"enable_line": True}), \
         patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value={"enable_mqtt_egress": True}), \
         patch("services.notifications.sinks.telegram.send_telegram_message",
               new_callable=AsyncMock) as tg, \
         patch("services.notifications.sinks.line.send_line_message",
               new_callable=AsyncMock) as line:
        asyncio.run(_run())

    assert tg.await_count == 1
    line.assert_not_awaited()
    shared.publish.assert_not_awaited()
