"""IT-25 — per-bed results for the 板橋榮家 1F electronic whiteboard.

The whiteboard vendor's server subscribes (read-only ACL, listener 1884) to
``bio-patrol/board/<site>/bed/<bed>``. Each time a bed's result is final the
patrol publishes one retained QoS1 message there, so a vendor that connects
late still sees every bed's latest state. ``bio-patrol/board/<site>/online``
is "1" while the app's shared MQTT connection is up and "0" (LWT) when it
drops. Contract: docs/whiteboard-board-topic.md.

Best effort by design: nothing here may block or break a patrol. A failed
publish is one WARN line — no retry, no queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import aiomqtt

from common_types import get_now
from settings.config import get_runtime_settings

if TYPE_CHECKING:
    from services.zigbee_mqtt import ZigbeeMQTT

logger = logging.getLogger("services.integration.board_publisher")

PUBLISH_TIMEOUT_S = 5.0
TOPIC_ROOT = "bio-patrol/board"

# Statuses that carry a reading; every other status sends bpm/rpm null.
_WITH_VITALS = ("normal", "restless")

_client: "ZigbeeMQTT | None" = None
_warned_no_site = False


def classify_scan(status, is_valid) -> str:
    """One scan row → normal / restless / absent / unreachable / no_reading.

    The same rule the Telegram run summary buckets by (absent is folded into
    no_reading there). ``status`` is the Wisleep status (int or str) or "N/A"
    for a skipped scan.
    """
    if is_valid:
        return "normal"
    s = str(status)
    if s == "2":
        return "restless"
    if s == "0":
        return "absent"
    if s == "N/A":
        return "unreachable"
    return "no_reading"


def configure(client: "ZigbeeMQTT | None") -> None:
    """Hand over the shared long-lived MQTT client (None = zigbee disabled →
    a short-lived connection per publish)."""
    global _client
    _client = client


def _site(cfg: dict) -> str | None:
    """The site id when the board is on and configured, else None."""
    global _warned_no_site
    if not cfg.get("whiteboard_enabled", False):
        return None
    site = str(cfg.get("whiteboard_site_id") or "").strip()
    if not site:
        if not _warned_no_site:
            logger.warning("whiteboard_enabled is on but whiteboard_site_id is empty — board publish skipped")
            _warned_no_site = True
        return None
    return site


def _online_topic(site: str) -> str:
    return f"{TOPIC_ROOT}/{site}/online"


def _as_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


async def publish_bed_status(bed_key: str, status: str, bpm, rpm, *,
                             demo: bool = False, ts: str | None = None) -> bool:
    """Publish one bed's final result. Returns True when it went out; never raises."""
    if demo:
        return False  # demo runs never reach the whiteboard
    topic = ""
    try:
        cfg = get_runtime_settings()
        site = _site(cfg)
        if site is None or not bed_key:
            return False
        bed_map = cfg.get("whiteboard_bed_id_map") or {}
        bed = str(bed_map.get(bed_key, bed_key)) if isinstance(bed_map, dict) else str(bed_key)
        topic = f"{TOPIC_ROOT}/{site}/bed/{bed}"
        has_vitals = status in _WITH_VITALS
        payload = {
            "bed": bed,
            "time": ts or get_now().isoformat(),
            "bpm": _as_int(bpm) if has_vitals else None,
            "rpm": _as_int(rpm) if has_vitals else None,
            "status": status,
        }

        if _client is not None:
            ok = await asyncio.wait_for(
                _client.publish(topic, payload, qos=1, retain=True),
                timeout=PUBLISH_TIMEOUT_S,
            )
            if not ok:
                logger.warning("Board publish to %s skipped — shared MQTT client not connected", topic)
                return False
            return True

        host = cfg.get("zigbee_mqtt_host", "mqtt-broker")
        port = int(cfg.get("zigbee_mqtt_port", 1883))
        wire = json.dumps(payload, ensure_ascii=False)

        async def _short_lived():
            async with aiomqtt.Client(hostname=host, port=port) as client:
                await client.publish(topic, wire, qos=1, retain=True)

        await asyncio.wait_for(_short_lived(), timeout=PUBLISH_TIMEOUT_S)
        return True
    except asyncio.TimeoutError:
        logger.warning("Board publish to %s timed out after %.0fs", topic or bed_key, PUBLISH_TIMEOUT_S)
    except Exception as e:
        logger.warning("Board publish to %s failed: %s", topic or bed_key, e)
    return False


def will() -> "aiomqtt.Will | None":
    """LWT for the shared connection: retained "0" on the online topic, or
    None when the board is off / has no site id."""
    try:
        site = _site(get_runtime_settings())
    except Exception as e:
        logger.warning("Board LWT not set: %s", e)
        return None
    if site is None:
        return None
    return aiomqtt.Will(_online_topic(site), "0", qos=1, retain=True)


async def announce_online(client) -> None:
    """ZigbeeMQTT on_connect hook: retained "1" on the online topic.

    ``client`` is the raw aiomqtt.Client — the payload is the bare string "1",
    not JSON, so ZigbeeMQTT.publish (which json-encodes) is not used.
    """
    try:
        site = _site(get_runtime_settings())
        if site is None:
            return
        await asyncio.wait_for(
            client.publish(_online_topic(site), "1", qos=1, retain=True),
            timeout=PUBLISH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("Board online announce timed out after %.0fs", PUBLISH_TIMEOUT_S)
    except Exception as e:
        logger.warning("Board online announce failed: %s", e)
