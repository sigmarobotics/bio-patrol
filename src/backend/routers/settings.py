"""Settings endpoints + the two MQTT test SSE streams."""
import asyncio
import json
import logging
from typing import AsyncIterator

import paho.mqtt.client as mqtt

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from settings.config import SETTINGS_FILE, get_runtime_settings, update_settings
from utils.json_io import load_json, save_json
from services.bio_sensor_mqtt import is_valid_scan
from services.task_runtime import engines, task_queues, task_worker, TaskEngine

DEFAULT_ROBOT_PORT = 26400
ROBOT_ID = "kachaka"
ROBOT_NAME = "Kachaka Care"

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["Settings"])


# ─── Robot IP normalisation + re-registration ────────────────────────────────

def _normalize_robot_ip(ip: str) -> str:
    """Append default Kachaka gRPC port if caller omitted it."""
    if not ip:
        return ip
    return ip if ":" in ip else f"{ip}:{DEFAULT_ROBOT_PORT}"


async def _reregister_robot(new_ip: str) -> dict:
    """Swap FleetAPI's kachaka slot to a new IP without a container restart.

    Returns register_robot's result dict so the UI can surface ok/error inline.
    """
    from dependencies import get_fleet
    fleet = get_fleet()
    await fleet.unregister_robot(ROBOT_ID)
    result = await fleet.register_robot(ROBOT_ID, new_ip, ROBOT_NAME)
    if result.get("ok") and ROBOT_ID not in engines:
        engines[ROBOT_ID] = TaskEngine(fleet, ROBOT_ID)
        task_queues[ROBOT_ID] = asyncio.Queue()
        asyncio.create_task(task_worker(ROBOT_ID))
    return result


# ─── Settings GET / POST + manual reconnect ──────────────────────────────────

@router.get("/settings")
async def get_settings():
    """Return DEFAULT_SETTINGS merged with settings.json."""
    return get_runtime_settings()


@router.post("/settings/reconnect-robot")
async def reconnect_robot():
    """Force re-registration on the saved robot_ip — for robot reboot recovery."""
    cfg = get_runtime_settings()
    ip = cfg.get("robot_ip", "")
    if not ip:
        raise HTTPException(status_code=400, detail="robot_ip is not set in settings")
    result = await _reregister_robot(ip)
    if result.get("ok"):
        logger.info(f"Robot '{ROBOT_ID}' reconnected at {ip}")
    else:
        logger.warning(f"Robot '{ROBOT_ID}' reconnect failed at {ip}: {result.get('error', 'unknown')}")
    return {"status": "ok" if result.get("ok") else "error", "ip": ip, "result": result}


@router.post("/settings")
async def save_settings(body: dict):
    """Merge incoming JSON into settings.json. Re-register the robot on IP change."""
    if "robot_ip" in body:
        body["robot_ip"] = _normalize_robot_ip(body["robot_ip"])

    current = load_json(SETTINGS_FILE, {})
    old_ip = current.get("robot_ip", "")
    current.update(body)
    save_json(SETTINGS_FILE, current)
    new_ip = current.get("robot_ip", "")

    response: dict = {"status": "ok", "data": get_runtime_settings()}
    if new_ip and new_ip != old_ip:
        try:
            result = await _reregister_robot(new_ip)
            response["robot_re_register"] = result
            if result.get("ok"):
                logger.info(f"Robot '{ROBOT_ID}' re-registered at {new_ip}")
            else:
                logger.warning(f"Robot '{ROBOT_ID}' re-register failed at {new_ip}: {result.get('error', 'unknown')}")
        except Exception as e:
            logger.error(f"Re-register raised: {e}")
            response["robot_re_register"] = {"ok": False, "error": str(e)}
    return response


# ─── SSE-test helpers ────────────────────────────────────────────────────────

def _sse_event(msg: str, level: str = "info") -> str:
    """Format a Server-Sent Event line."""
    payload = json.dumps({"msg": msg, "level": level})
    return f"data: {payload}\n\n"


async def _mqtt_test_client(broker: str, port: int, topic: str, on_message) -> AsyncIterator:
    """Async-generator paho probe: yields SSE log strings as connect/subscribe
    progress, then yields the connected `mqtt.Client` (or `None` on failure)
    as the final event. Caller handles cleanup of the yielded client.

    Streaming the lines live (instead of buffering) keeps the UI's progress
    indicator responsive even when the broker is slow to ACK.
    """
    yield _sse_event(f"Connecting to MQTT {broker}:{port}...")

    connected = asyncio.Event()
    connect_error: dict = {}

    def _on_connect(_c, _u, _f, rc, _properties=None):
        if rc == 0:
            connected.set()
        else:
            connect_error["rc"] = rc
            connected.set()

    client = mqtt.Client(protocol=mqtt.MQTTv31)
    client.on_connect = _on_connect
    client.on_message = on_message

    try:
        await asyncio.to_thread(client.connect, broker, port, 60)
        client.loop_start()
    except Exception as e:
        yield _sse_event(f"MQTT connection failed: {e}", "error")
        yield None
        return

    try:
        await asyncio.wait_for(connected.wait(), timeout=5)
    except asyncio.TimeoutError:
        yield _sse_event("MQTT connection timed out (5s)", "error")
        client.loop_stop()
        client.disconnect()
        yield None
        return

    if connect_error:
        yield _sse_event(f"MQTT connection failed: rc={connect_error['rc']}", "error")
        client.loop_stop()
        client.disconnect()
        yield None
        return

    yield _sse_event("MQTT connected")
    await asyncio.to_thread(client.subscribe, topic)
    yield _sse_event(f"Subscribed to {topic}")
    yield client


# ─── /settings/test-mqtt SSE ────────────────────────────────────────────────

@router.get("/settings/test-mqtt")
async def test_mqtt():
    """MQTT connectivity probe — connects, subscribes, waits up to 15s for any payload."""
    cfg = get_runtime_settings()
    broker = cfg.get("mqtt_broker", "localhost")
    port = int(cfg.get("mqtt_port", 1883))
    topic = cfg.get("mqtt_topic", "")

    async def generate() -> AsyncIterator[str]:
        received: list[str] = []

        def on_message(_client, _userdata, msg):
            try:
                received.append(msg.payload.decode())
            except Exception:
                received.append(str(msg.payload))

        client = None
        async for event in _mqtt_test_client(broker, port, topic, on_message):
            if isinstance(event, str):
                yield event
            else:
                client = event

        if client is None:
            yield _sse_event("Test complete.", "done")
            return

        try:
            yield _sse_event("Waiting for data (15s timeout)...")
            for i in range(15):
                await asyncio.sleep(1)
                if received:
                    break
                if i % 5 == 4:
                    yield _sse_event(f"  Still waiting... ({i + 1}s)")

            if received:
                yield _sse_event(f"Received {len(received)} message(s):")
                for raw in received[:5]:
                    display = raw[:300] + ("..." if len(raw) > 300 else "")
                    yield _sse_event(f"  {display}")
            else:
                yield _sse_event("No data received within timeout", "warn")
        finally:
            client.loop_stop()
            client.disconnect()

        yield _sse_event("Test complete.", "done")

    return StreamingResponse(generate(), media_type="text/event-stream")


# ─── /settings/test-bio-scan SSE ────────────────────────────────────────────

@router.get("/settings/test-bio-scan")
async def test_bio_scan():
    """Simulate a full bio-scan cycle using saved settings — initial wait then retry loop."""
    cfg = get_runtime_settings()
    broker = cfg.get("mqtt_broker", "localhost")
    port = int(cfg.get("mqtt_port", 1883))
    topic = cfg.get("mqtt_topic", "")
    wait_time = int(cfg.get("bio_scan_wait_time", 10))
    retry_count = int(cfg.get("bio_scan_retry_count", 19))
    initial_wait = int(cfg.get("bio_scan_initial_wait", 120))
    valid_status = int(cfg.get("bio_scan_valid_status", 4))

    async def generate() -> AsyncIterator[str]:
        latest: dict = {}

        def on_message(_client, _userdata, msg):
            try:
                latest["value"] = json.loads(msg.payload.decode())
            except Exception:
                pass

        yield _sse_event(
            f"Config: initial_wait={initial_wait}s, retry_count={retry_count}, "
            f"wait_time={wait_time}s, valid_status={valid_status}"
        )

        client = None
        async for event in _mqtt_test_client(broker, port, topic, on_message):
            if isinstance(event, str):
                yield event
            else:
                client = event

        if client is None:
            yield _sse_event("Test complete.", "done")
            return

        try:
            yield _sse_event(f"Starting initial wait ({initial_wait}s)...")
            for elapsed in range(initial_wait):
                await asyncio.sleep(1)
                remaining = initial_wait - elapsed - 1
                if remaining > 0 and remaining % 10 == 0:
                    yield _sse_event(f"  Initial wait: {remaining}s remaining...")
            yield _sse_event("Initial wait complete. Starting scan retries...")

            valid_data = None
            for i in range(retry_count):
                yield _sse_event(f"Retry {i + 1}/{retry_count}: checking sensor data...")
                snapshot = latest.get("value")
                if snapshot and "records" in snapshot:
                    for record in snapshot["records"]:
                        is_valid = is_valid_scan(record, valid_status)
                        label = "VALID" if is_valid else "invalid"
                        yield _sse_event(
                            f"  Status={record.get('status')}, "
                            f"BPM={record.get('bpm', 0)}, RPM={record.get('rpm', 0)} -> {label}"
                        )
                        if is_valid and valid_data is None:
                            valid_data = record
                    if valid_data:
                        yield _sse_event("Valid measurement found!", "success")
                        break
                else:
                    yield _sse_event("  No MQTT data received")
                if i + 1 < retry_count:
                    yield _sse_event(f"  Waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)

            if valid_data:
                yield _sse_event(
                    f"Result: Valid data — BPM={valid_data.get('bpm')}, "
                    f"RPM={valid_data.get('rpm')}, Status={valid_data.get('status')}",
                    "success",
                )
            else:
                yield _sse_event("Result: No valid data after all retries", "warn")
        finally:
            client.loop_stop()
            client.disconnect()

        yield _sse_event("Test complete.", "done")

    return StreamingResponse(generate(), media_type="text/event-stream")
