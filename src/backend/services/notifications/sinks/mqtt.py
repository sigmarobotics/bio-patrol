"""MqttSink — publishes AnomalyEvent JSON to the internal mqtt-broker.

When a long-lived ZigbeeMQTT is injected (the production path), MqttSink
delegates publish to it so we don't pay TCP+CONNECT+SUBACK per event. With
no injected client (tests / zigbee disabled), a 3-second short-lived
aiomqtt.Client is used.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import TYPE_CHECKING

import aiomqtt

from services.notifications.events import AnomalyEvent
from settings.config import get_runtime_settings

if TYPE_CHECKING:
    from services.zigbee_mqtt import ZigbeeMQTT

logger = logging.getLogger("services.notifications.mqtt")

PUBLISH_TIMEOUT_S = 3.0


def _serialize_event(event: AnomalyEvent) -> dict:
    payload = dataclasses.asdict(event)
    payload["timestamp"] = event.timestamp.isoformat()
    # Severity / Source are str-Enums so they round-trip through JSON, but make the
    # wire format explicit values rather than relying on str-enum identity.
    payload["severity"] = event.severity.value
    payload["source"] = event.source.value
    return payload


class MqttSink:
    def __init__(self, zigbee_mqtt: "ZigbeeMQTT | None" = None):
        self._zigbee_mqtt = zigbee_mqtt

    async def is_enabled(self) -> bool:
        return bool(get_runtime_settings().get("enable_mqtt_egress", False))

    async def send(self, event: AnomalyEvent) -> None:
        cfg = get_runtime_settings()
        prefix = cfg.get("mqtt_egress_topic_prefix", "bio-patrol/anomaly")
        topic = f"{prefix}/{event.severity.value}/{event.source.value}"
        payload = _serialize_event(event)

        if self._zigbee_mqtt is not None:
            published = await asyncio.wait_for(
                self._zigbee_mqtt.publish(topic, payload, qos=1, retain=False),
                timeout=PUBLISH_TIMEOUT_S,
            )
            if not published:
                logger.warning("Anomaly %s skipped — shared MQTT client unavailable", event.event_id)
                return
            logger.debug("Published anomaly %s to %s via shared client", event.event_id, topic)
            return

        host = cfg.get("zigbee_mqtt_host", "mqtt-broker")
        port = cfg.get("zigbee_mqtt_port", 1883)
        wire = json.dumps(payload, ensure_ascii=False)

        async def _publish():
            async with aiomqtt.Client(hostname=host, port=port) as client:
                await client.publish(topic, wire, qos=1, retain=False)

        await asyncio.wait_for(_publish(), timeout=PUBLISH_TIMEOUT_S)
        logger.debug("Published anomaly %s to %s (short-lived)", event.event_id, topic)
