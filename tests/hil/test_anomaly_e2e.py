"""HIL E2E tests for the IT-5 anomaly notification dispatcher.

Real Telegram bot, real internal mqtt-broker. Marked @pytest.mark.hil.

Credentials are read from ~/.claude/.env.test:
  TEST_TELEGRAM_BOT_TOKEN, TEST_TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import asyncio
import json

import pytest

import aiomqtt
import httpx

from services.notifications.dispatcher import AnomalyDispatcher
from services.notifications.recipients import StaticResolver
from services.notifications.sinks.mqtt import MqttSink
from services.notifications.sinks.telegram import TelegramSink


@pytest.fixture
def telegram_creds(env_test):
    token = env_test.get("TEST_TELEGRAM_BOT_TOKEN")
    chat_id = env_test.get("TEST_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        pytest.skip("TEST_TELEGRAM_BOT_TOKEN / TEST_TELEGRAM_CHAT_ID not set in ~/.claude/.env.test")
    return token, chat_id


@pytest.fixture
def telegram_settings(telegram_creds, monkeypatch):
    """Patch every get_runtime_settings consumer in the notification path."""
    token, chat_id = telegram_creds
    fake = {
        "enable_telegram": True,
        "telegram_bot_token": token,
        "telegram_user_id": chat_id,
        "enable_mqtt_egress": True,
        "zigbee_mqtt_host": "localhost",
        "zigbee_mqtt_port": 1883,
        "mqtt_egress_topic_prefix": "bio-patrol/anomaly-test",
    }
    monkeypatch.setattr("services.notifications.sinks.telegram.get_runtime_settings", lambda: fake)
    monkeypatch.setattr("services.notifications.sinks.mqtt.get_runtime_settings", lambda: fake)
    monkeypatch.setattr("services.notifications.recipients.get_runtime_settings", lambda: fake)
    monkeypatch.setattr("services.telegram_service.get_runtime_settings", lambda: fake)
    return fake


@pytest.mark.hil
@pytest.mark.slow
def test_dispatcher_publishes_to_telegram(telegram_settings, make_failure_event):
    """Real bot, real chat. Verifies the bot is reachable and dispatch completes without error.

    Visual confirmation in the chat is the human-side verification.
    """
    async def _run():
        d = AnomalyDispatcher()
        d.register(TelegramSink(StaticResolver()))
        await d.dispatch(make_failure_event())
        await d.drain(timeout=15.0)

    asyncio.run(_run())

    token = telegram_settings["telegram_bot_token"]
    resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10.0)
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("ok") is True


@pytest.mark.hil
def test_dispatcher_publishes_to_internal_mqtt(telegram_settings, make_failure_event):
    """Real internal mosquitto. Subscribe + dispatch + assert receipt within 5s.

    Requires a local broker on localhost:1883 — skips cleanly if unavailable.
    """
    received: list[tuple[str, dict]] = []

    async def _subscribe_and_run():
        async def _consume():
            async with aiomqtt.Client("localhost", 1883) as client:
                await client.subscribe("bio-patrol/anomaly-test/#")
                async for msg in client.messages:
                    received.append((str(msg.topic), json.loads(msg.payload.decode())))
                    return  # one message is enough

        consume_task = asyncio.create_task(_consume())
        await asyncio.sleep(0.5)  # let the subscriber settle

        d = AnomalyDispatcher()
        d.register(MqttSink())
        await d.dispatch(make_failure_event())
        await d.drain(timeout=10.0)

        await asyncio.wait_for(consume_task, timeout=5.0)

    try:
        asyncio.run(_subscribe_and_run())
    except (aiomqtt.MqttError, ConnectionRefusedError, OSError) as e:
        pytest.skip(f"Internal mqtt-broker not reachable on localhost:1883: {e}")
    except asyncio.TimeoutError:
        pytest.fail("Did not receive MQTT message on bio-patrol/anomaly-test/# within 5s")

    assert len(received) == 1
    topic, payload = received[0]
    assert topic == "bio-patrol/anomaly-test/warn/bio_scan_failure"
    assert payload["severity"] == "warn"
    assert payload["source"] == "bio_scan_failure"
    assert payload["bed_key"] == "HIL-101-1"
    assert "event_id" in payload


@pytest.mark.hil
def test_one_sink_failure_does_not_block_other_sinks(telegram_settings, monkeypatch, make_failure_event):
    """Force MqttSink to fail by pointing it at an unreachable port; dispatch completes cleanly.

    The TelegramSink should still ship its message (visual confirm). The dispatch+drain call
    must not raise, even though MqttSink fails.
    """
    fake = dict(telegram_settings)
    fake["zigbee_mqtt_port"] = 9   # discard / unreachable
    monkeypatch.setattr("services.notifications.sinks.mqtt.get_runtime_settings", lambda: fake)

    async def _run():
        d = AnomalyDispatcher()
        d.register(TelegramSink(StaticResolver()))
        d.register(MqttSink())
        await d.dispatch(make_failure_event())
        await d.drain(timeout=15.0)

    asyncio.run(_run())  # if this raises, the isolation is broken
