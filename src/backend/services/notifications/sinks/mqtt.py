"""MqttSink — publishes AnomalyEvent as JSON to internal mqtt-broker.

Short-lived connection per publish: event volume is low (handful per patrol),
not worth keeping a long-running client.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging

import aiomqtt

from services.notifications.events import AnomalyEvent
from settings.config import get_runtime_settings

logger = logging.getLogger("services.notifications.mqtt")

PUBLISH_TIMEOUT_S = 10.0


def _serialize_event(event: AnomalyEvent) -> str:
    payload = dataclasses.asdict(event)
    payload["timestamp"] = event.timestamp.isoformat()
    # Severity / Source are str-Enums so they round-trip through JSON, but make the
    # wire format explicit values rather than relying on str-enum identity.
    payload["severity"] = event.severity.value
    payload["source"] = event.source.value
    return json.dumps(payload, ensure_ascii=False)


class MqttSink:
    async def is_enabled(self) -> bool:
        return bool(get_runtime_settings().get("enable_mqtt_egress", False))

    async def send(self, event: AnomalyEvent) -> None:
        cfg = get_runtime_settings()
        host = cfg.get("zigbee_mqtt_host", "mqtt-broker")
        port = cfg.get("zigbee_mqtt_port", 1883)
        prefix = cfg.get("mqtt_egress_topic_prefix", "bio-patrol/anomaly")
        topic = f"{prefix}/{event.severity.value}/{event.source.value}"
        payload = _serialize_event(event)

        async def _publish():
            async with aiomqtt.Client(hostname=host, port=port) as client:
                await client.publish(topic, payload, qos=1, retain=False)

        await asyncio.wait_for(_publish(), timeout=PUBLISH_TIMEOUT_S)
        logger.debug("Published anomaly %s to %s", event.event_id, topic)
