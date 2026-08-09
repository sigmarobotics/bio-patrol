"""Settings endpoints + the two MQTT test SSE streams."""
import asyncio
import json
import logging
import os
import ssl
from typing import AsyncIterator

import httpx
import paho.mqtt.client as mqtt

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse

import lifespan_state
from settings.config import SETTINGS_FILE, get_runtime_settings, update_settings
from utils.json_io import load_json, save_json
from services.bio_sensor_mqtt import is_valid_scan
from services.line_service import send_line_message
from services.notifications.recipients import StaticResolver
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

    Cancels any in-flight lifespan retry against the OLD IP first, so a
    racing retry that succeeds cannot reinstate a stale slot.
    """
    from dependencies import get_fleet
    fleet = get_fleet()

    await lifespan_state.cancel_register_retry(ROBOT_ID)
    await fleet.unregister_robot(ROBOT_ID)
    result = await fleet.register_robot(ROBOT_ID, new_ip, ROBOT_NAME)
    if result.get("ok") and ROBOT_ID not in engines:
        engines[ROBOT_ID] = TaskEngine(fleet, ROBOT_ID)
        task_queues[ROBOT_ID] = asyncio.Queue()
        worker = asyncio.create_task(task_worker(ROBOT_ID))
        lifespan_state._worker_tasks[ROBOT_ID] = worker
    return result


# ─── Secret masking ──────────────────────────────────────────────────────────
# /api/settings is unauthenticated on the ward LAN, so credentials never leave
# the backend in the clear. mqtt_tls_cert/key are file PATHS, not secrets, and
# stay readable. Internal consumers all call get_runtime_settings() directly —
# masking lives at the route layer only.

SECRET_KEYS = (
    "mqtt_password",
    "telegram_bot_token",
    "notify_hub_token",
    "line_channel_access_token",
    "line_webhook_api_key",
    "gemini_api_key",
)
MASK_PREFIX = "••••"


def _mask_secret(value: str) -> str:
    """Non-empty secret -> ••••<last 4>. Empty stays empty so the UI can still
    tell "not configured" from "configured but hidden"."""
    return f"{MASK_PREFIX}{value[-4:]}" if value else ""


def _mask_settings(cfg: dict) -> dict:
    masked = dict(cfg)
    for key in SECRET_KEYS:
        if key in masked:
            masked[key] = _mask_secret(str(masked[key] or ""))
    return masked


def _drop_unchanged_secrets(body: dict, cfg: dict) -> dict:
    """Strip secret fields whose incoming value is just the mask we handed out.

    The Settings page POSTs every field at once, so an untouched form would
    otherwise overwrite the real credential with its own mask. A genuinely new
    value differs from the mask and is saved; an empty value clears the secret.
    """
    cleaned = dict(body)
    for key in SECRET_KEYS:
        if key in cleaned and cleaned[key] == _mask_secret(str(cfg.get(key) or "")):
            del cleaned[key]
    return cleaned


# ─── Settings GET / POST + manual reconnect ──────────────────────────────────

@router.get("/settings")
async def get_settings():
    """Return DEFAULT_SETTINGS merged with settings.json, secrets masked."""
    return _mask_settings(get_runtime_settings())


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
    body = _drop_unchanged_secrets(body, get_runtime_settings())
    if "robot_ip" in body:
        body["robot_ip"] = _normalize_robot_ip(body["robot_ip"])

    current = load_json(SETTINGS_FILE, {})
    old_ip = current.get("robot_ip", "")
    current.update(body)
    save_json(SETTINGS_FILE, current)
    new_ip = current.get("robot_ip", "")

    response: dict = {"status": "ok", "data": _mask_settings(get_runtime_settings())}
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


# ─── TLS cert/key management ────────────────────────────────────────────────

def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


@router.get("/settings/tls-status")
async def tls_status():
    """Return existence + size of the configured TLS cert and key files."""
    cfg = get_runtime_settings()
    root = _project_root()

    def _info(rel_path: str) -> dict:
        if not rel_path:
            return {"path": "", "exists": False, "size": 0}
        abs_path = os.path.join(root, rel_path)
        exists = os.path.isfile(abs_path)
        return {
            "path": rel_path,
            "exists": exists,
            "size": os.path.getsize(abs_path) if exists else 0,
        }

    return {
        "cert": _info(cfg.get("mqtt_tls_cert", "")),
        "key": _info(cfg.get("mqtt_tls_key", "")),
    }


@router.post("/settings/upload-tls")
async def upload_tls(
    cert: UploadFile = File(None, description="PEM client certificate (.crt)"),
    key: UploadFile = File(None, description="PEM private key (.key)"),
):
    """Upload TLS client cert and/or key into the configured wisleep-key/ directory."""
    if cert is None and key is None:
        raise HTTPException(status_code=400, detail="Provide at least one of cert or key")

    cfg = get_runtime_settings()
    root = _project_root()
    saved = {}

    for field, upload, setting_key in [
        ("cert", cert, "mqtt_tls_cert"),
        ("key", key, "mqtt_tls_key"),
    ]:
        if upload is None:
            continue
        rel_path = cfg.get(setting_key, f"wisleep-key/sigmabot.{'crt' if field == 'cert' else 'key'}")
        abs_path = os.path.join(root, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        content = await upload.read()
        if not content:
            raise HTTPException(status_code=400, detail=f"{field} file is empty")
        with open(abs_path, "wb") as f:
            f.write(content)
        saved[field] = {"path": rel_path, "size": len(content)}
        logger.info(f"TLS {field} saved to {abs_path} ({len(content)} bytes)")

    return {"status": "ok", "saved": saved}


# ─── SSE-test helpers ────────────────────────────────────────────────────────

def _sse_event(msg: str, level: str = "info") -> str:
    """Format a Server-Sent Event line."""
    payload = json.dumps({"msg": msg, "level": level})
    return f"data: {payload}\n\n"


async def _mqtt_test_client(broker: str, port: int, topic: str, on_message,
                           username: str = None, password: str = None,
                           tls_cert: str = None, tls_key: str = None) -> AsyncIterator:
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
    if tls_cert and tls_key:
        client.tls_set(certfile=tls_cert, keyfile=tls_key, cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
    if username:
        client.username_pw_set(username, password)
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

def _get_tls_paths(cfg: dict) -> tuple[str | None, str | None]:
    """Resolve mqtt_tls_cert / mqtt_tls_key relative to the project root."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    cert = cfg.get("mqtt_tls_cert", "")
    key = cfg.get("mqtt_tls_key", "")
    return (
        os.path.join(project_root, cert) if cert else None,
        os.path.join(project_root, key) if key else None,
    )


@router.get("/settings/test-mqtt")
async def test_mqtt():
    """MQTT connectivity probe — connects, subscribes, waits up to 15s for any payload."""
    cfg = get_runtime_settings()
    broker = cfg.get("mqtt_broker", "localhost")
    port = int(cfg.get("mqtt_port", 8883))
    topic = cfg.get("mqtt_topic", "")
    username = cfg.get("mqtt_username") or None
    password = cfg.get("mqtt_password") or None
    tls_cert, tls_key = _get_tls_paths(cfg)

    async def generate() -> AsyncIterator[str]:
        received: list[str] = []

        def on_message(_client, _userdata, msg):
            try:
                received.append(msg.payload.decode())
            except Exception:
                received.append(str(msg.payload))

        client = None
        async for event in _mqtt_test_client(broker, port, topic, on_message,
                                             username=username, password=password,
                                             tls_cert=tls_cert, tls_key=tls_key):
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
    port = int(cfg.get("mqtt_port", 8883))
    topic = cfg.get("mqtt_topic", "")
    username = cfg.get("mqtt_username") or None
    password = cfg.get("mqtt_password") or None
    tls_cert, tls_key = _get_tls_paths(cfg)
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
        async for event in _mqtt_test_client(broker, port, topic, on_message,
                                             username=username, password=password,
                                             tls_cert=tls_cert, tls_key=tls_key):
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

# ─── LINE notification endpoints ─────────────────────────────────────────────

@router.get("/line/groups")
async def line_groups():
    """Proxy the LINE webhook service's recorded push targets (groups/users)."""
    cfg = get_runtime_settings()
    base_url = (cfg.get("line_webhook_url", "") or "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="line_webhook_url is not set in settings")
    headers = {"Authorization": f"Bearer {cfg.get('line_webhook_api_key', '')}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"{base_url}/groups", headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"LINE webhook service unreachable: {e}")
    if res.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"LINE webhook service returned {res.status_code}: {res.text[:200]}",
        )
    try:
        return res.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="LINE webhook service returned a non-JSON response")


@router.post("/settings/test-line")
async def test_line():
    """Push a test message to every selected LINE target using saved settings."""
    cfg = get_runtime_settings()
    if not cfg.get("enable_line", False):
        raise HTTPException(status_code=400, detail="enable_line is off")
    if not cfg.get("line_channel_access_token"):
        raise HTTPException(status_code=400, detail="line_channel_access_token is not set")
    # Resolve through the same path real anomaly dispatch uses.
    targets = await StaticResolver().resolve(None, channel="line")
    if not targets:
        raise HTTPException(status_code=400, detail="no LINE target selected (line_group_ids is empty)")
    results = await asyncio.gather(
        *(send_line_message("🔔 bio-patrol LINE 通報測試訊息", to=t) for t in targets)
    )
    ok = all(results)
    return {"status": "ok" if ok else "error", "results": dict(zip(targets, results))}
