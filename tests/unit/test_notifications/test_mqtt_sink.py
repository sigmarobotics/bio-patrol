"""Unit tests for MqttSink — covers topic assembly, payload schema, is_enabled gating,
and the long-lived ZigbeeMQTT client reuse path."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch, AsyncMock, MagicMock

from services.notifications.events import AnomalyEvent, Severity, Source
from services.notifications.sinks.mqtt import MqttSink


def _settings(**overrides):
    base = {
        "enable_mqtt_egress": True,
        "zigbee_mqtt_host": "mqtt-broker",
        "zigbee_mqtt_port": 1883,
        "mqtt_egress_topic_prefix": "bio-patrol/anomaly",
    }
    base.update(overrides)
    return base


def _event():
    return AnomalyEvent(
        severity=Severity.WARN,
        source=Source.BIO_SCAN_FAILURE,
        title="⚠️ 101-1 量測失敗",
        body="床位：101-1\n原因：無有效量測數值",
        bed_key="101-1",
        task_id="task-1",
        raw={"status": 2, "bpm": 0, "rpm": 0},
    )


def test_is_enabled_reads_setting():
    sink = MqttSink()
    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings(enable_mqtt_egress=False)):
        assert asyncio.run(sink.is_enabled()) is False
    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings(enable_mqtt_egress=True)):
        assert asyncio.run(sink.is_enabled()) is True


def test_send_publishes_to_hierarchical_topic_with_json_payload():
    sink = MqttSink()
    fake_client = AsyncMock()
    fake_client.publish = AsyncMock()
    aenter = MagicMock()
    aenter.__aenter__ = AsyncMock(return_value=fake_client)
    aenter.__aexit__ = AsyncMock(return_value=None)

    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings()), \
         patch("services.notifications.sinks.mqtt.aiomqtt.Client",
               return_value=aenter) as mock_client_cls:
        e = _event()
        asyncio.run(sink.send(e))

    mock_client_cls.assert_called_once_with(hostname="mqtt-broker", port=1883)
    fake_client.publish.assert_awaited_once()
    args, kwargs = fake_client.publish.await_args
    topic, payload = args
    assert topic == "bio-patrol/anomaly/warn/bio_scan_failure"
    assert kwargs.get("qos") == 1
    assert kwargs.get("retain") is False
    decoded = json.loads(payload)
    assert decoded["event_id"] == e.event_id
    assert decoded["severity"] == "warn"
    assert decoded["source"] == "bio_scan_failure"
    assert decoded["bed_key"] == "101-1"
    assert decoded["task_id"] == "task-1"
    assert decoded["raw"] == {"status": 2, "bpm": 0, "rpm": 0}
    assert decoded["title"] == "⚠️ 101-1 量測失敗"
    assert "timestamp" in decoded


def test_send_respects_custom_prefix():
    sink = MqttSink()
    fake_client = AsyncMock()
    fake_client.publish = AsyncMock()
    aenter = MagicMock()
    aenter.__aenter__ = AsyncMock(return_value=fake_client)
    aenter.__aexit__ = AsyncMock(return_value=None)
    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings(mqtt_egress_topic_prefix="custom/prefix")), \
         patch("services.notifications.sinks.mqtt.aiomqtt.Client", return_value=aenter):
        asyncio.run(sink.send(_event()))
    topic = fake_client.publish.await_args.args[0]
    assert topic == "custom/prefix/warn/bio_scan_failure"


def test_send_timeout_propagates_for_safe_send_to_handle():
    """A hung broker should raise asyncio.TimeoutError that the dispatcher logs."""
    sink = MqttSink()

    async def hang_forever(*a, **kw):
        await asyncio.sleep(60)

    aenter = MagicMock()
    aenter.__aenter__ = AsyncMock(side_effect=hang_forever)
    aenter.__aexit__ = AsyncMock(return_value=None)

    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings()), \
         patch("services.notifications.sinks.mqtt.aiomqtt.Client", return_value=aenter), \
         patch("services.notifications.sinks.mqtt.PUBLISH_TIMEOUT_S", 0.05):
        try:
            asyncio.run(sink.send(_event()))
            raised = False
        except asyncio.TimeoutError:
            raised = True
    assert raised is True


def test_send_uses_shared_zigbee_client_when_injected():
    """With ZigbeeMQTT injected, MqttSink delegates to its long-lived client and
    does not open a fresh aiomqtt connection per event."""
    zigbee = MagicMock()
    zigbee.publish = AsyncMock(return_value=True)
    sink = MqttSink(zigbee_mqtt=zigbee)
    e = _event()

    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings()), \
         patch("services.notifications.sinks.mqtt.aiomqtt.Client") as mock_client_cls:
        asyncio.run(sink.send(e))

    mock_client_cls.assert_not_called()  # short-lived path must not run
    zigbee.publish.assert_awaited_once()
    topic, payload = zigbee.publish.await_args.args
    kwargs = zigbee.publish.await_args.kwargs
    assert topic == "bio-patrol/anomaly/warn/bio_scan_failure"
    assert kwargs == {"qos": 1, "retain": False}
    assert payload["event_id"] == e.event_id
    assert payload["severity"] == "warn"
    assert payload["source"] == "bio_scan_failure"


def test_send_skips_when_shared_client_publish_returns_false():
    """Shared client returns False (broker unreachable / mid-reconnect).
    MqttSink logs + returns; it does NOT silently open a parallel short-lived
    connection while the long-lived client is reconnecting."""
    zigbee = MagicMock()
    zigbee.publish = AsyncMock(return_value=False)
    sink = MqttSink(zigbee_mqtt=zigbee)

    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings()), \
         patch("services.notifications.sinks.mqtt.aiomqtt.Client") as mock_client_cls:
        asyncio.run(sink.send(_event()))

    mock_client_cls.assert_not_called()
    zigbee.publish.assert_awaited_once()


def test_send_shared_client_publish_is_timeout_bounded():
    """A hung shared client must surface asyncio.TimeoutError so the dispatcher
    can isolate this sink and keep the others moving."""
    async def hang_forever(*a, **kw):
        await asyncio.sleep(60)

    zigbee = MagicMock()
    zigbee.publish = AsyncMock(side_effect=hang_forever)
    sink = MqttSink(zigbee_mqtt=zigbee)

    with patch("services.notifications.sinks.mqtt.get_runtime_settings",
               return_value=_settings()), \
         patch("services.notifications.sinks.mqtt.PUBLISH_TIMEOUT_S", 0.05):
        try:
            asyncio.run(sink.send(_event()))
            raised = False
        except asyncio.TimeoutError:
            raised = True
    assert raised is True
