"""
Async MQTT client for the zigbee2mqtt bridge.

Subscribes to `zigbee2mqtt/#`, parses bridge events (device_joined/announce) and
device messages (button_action), and dispatches them to a handler. Also exposes
permit_join / forget_device for pairing UX.

Adapted from sigma-button-controller (services/mqtt_service.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import aiomqtt

logger = logging.getLogger("services.zigbee_mqtt")

Handler = Callable[[dict], Awaitable[None]]


def parse_zigbee_message(topic: str, payload: str) -> dict | None:
    """Decode a zigbee2mqtt MQTT message into a structured event.

    Returns one of:
      - {"type": "device_joined" | "device_announce", "ieee_addr", "friendly_name"}
      - {"type": "button_action", "ieee_addr", "action", "battery", "linkquality"}
      - None for irrelevant or malformed messages
    """
    parts = topic.split("/")
    if len(parts) < 2 or parts[0] != "zigbee2mqtt":
        return None

    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    if len(parts) >= 3 and parts[1] == "bridge" and parts[2] == "event":
        event_type = data.get("type", "")
        event_data = data.get("data") or {}
        if event_type in ("device_joined", "device_announce"):
            ieee = event_data.get("ieee_address", "")
            if ieee:
                return {
                    "type": event_type,
                    "ieee_addr": ieee,
                    "friendly_name": event_data.get("friendly_name", ieee),
                }
        return None

    if len(parts) >= 2 and parts[1] == "bridge":
        return None

    ieee_addr = parts[1]
    action = data.get("action")
    if action:
        return {
            "type": "button_action",
            "ieee_addr": ieee_addr,
            "action": str(action),
            "battery": data.get("battery"),
            "linkquality": data.get("linkquality"),
        }
    return None


class ZigbeeMQTT:
    """Long-running aiomqtt subscriber, runs as a background task."""

    SUB_TOPIC = "zigbee2mqtt/#"
    PERMIT_JOIN_TOPIC = "zigbee2mqtt/bridge/request/permit_join"
    REMOVE_DEVICE_TOPIC = "zigbee2mqtt/bridge/request/device/remove"

    def __init__(self, host: str = "mqtt-broker", port: int = 1883):
        self._host = host
        self._port = port
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task | None = None
        self._handler: Handler | None = None
        self._connected = False
        self._stop = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected

    def set_handler(self, handler: Handler) -> None:
        self._handler = handler

    async def start(self) -> None:
        if self._task and not self._task.done():
            logger.info("Zigbee MQTT already running")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="zigbee_mqtt_loop")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._connected = False

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(hostname=self._host, port=self._port) as client:
                    self._client = client
                    self._connected = True
                    backoff = 1.0
                    logger.info("Connected to MQTT %s:%s", self._host, self._port)
                    await client.subscribe(self.SUB_TOPIC)
                    async for message in client.messages:
                        payload = message.payload
                        if isinstance(payload, (bytes, bytearray)):
                            payload = payload.decode(errors="replace")
                        event = parse_zigbee_message(str(message.topic), str(payload))
                        if event and self._handler:
                            try:
                                await self._handler(event)
                            except Exception:
                                logger.exception("Handler raised on event %s", event.get("type"))
            except aiomqtt.MqttError as e:
                self._connected = False
                self._client = None
                logger.warning("MQTT error: %s; reconnecting in %.1fs", e, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                break
            except Exception:
                self._connected = False
                logger.exception("Unexpected error in zigbee_mqtt loop")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)

    async def publish(self, topic: str, payload: dict) -> bool:
        client = self._client
        if client is None or not self._connected:
            logger.warning("Cannot publish to %s — MQTT not connected", topic)
            return False
        await client.publish(topic, json.dumps(payload))
        return True

    async def permit_join(self, allow: bool, time_s: int = 120) -> bool:
        return await self.publish(
            self.PERMIT_JOIN_TOPIC,
            {"value": bool(allow), "time": int(time_s) if allow else 0},
        )

    async def remove_device(self, ieee_addr: str) -> bool:
        return await self.publish(self.REMOVE_DEVICE_TOPIC, {"id": ieee_addr})
