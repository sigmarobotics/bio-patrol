"""
Settings API Router
Handles system configuration, beds, patrol, and schedule management.
"""
import uuid
import asyncio
import json
import os
import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from typing import Optional

from settings.config import (
    SETTINGS_FILE, BEDS_FILE, PATROL_FILE, SCHEDULE_FILE,
    get_runtime_settings, get_settings_dir,
)
from settings.defaults import DEFAULT_SETTINGS, DEFAULT_BEDS, DEFAULT_PATROL, DEFAULT_SCHEDULE
from utils.json_io import load_json, save_json
from common_types import (
    Task, TaskStep, TaskStatus, StepStatus, generate_task_id,
)
from services.task_runtime import tasks_db, submit_task, engines, task_queues, task_worker, TaskEngine
from services.bio_sensor_mqtt import is_valid_scan

DEFAULT_ROBOT_PORT = 26400
ROBOT_ID = "kachaka"
ROBOT_NAME = "Kachaka Care"


def _normalize_robot_ip(ip: str) -> str:
    """Append default Kachaka gRPC port if caller omitted it."""
    if not ip:
        return ip
    return ip if ":" in ip else f"{ip}:{DEFAULT_ROBOT_PORT}"


async def _reregister_robot(new_ip: str) -> dict:
    """Swap the FleetAPI's kachaka slot to a new IP without a container restart.

    Returns the register_robot result dict so callers can surface ok/error to the UI.
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

logger = logging.getLogger(__name__)

# Maps directory (sibling to data/config)
MAPS_DIR = os.path.join(os.path.dirname(get_settings_dir()), "maps")

router = APIRouter(prefix="/api", tags=["Settings & Config"])


# ═══════════════════════════════════════════════════════════════════════════
# SETTINGS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/settings")
async def get_settings():
    """Return merged DEFAULT_SETTINGS + settings.json"""
    return get_runtime_settings()


@router.post("/settings/reconnect-robot")
async def reconnect_robot():
    """Force re-registration using the currently saved robot_ip.

    Useful when the robot was rebooted or the network hiccupped — IP unchanged
    but the FleetAPI slot needs a fresh gRPC connection.
    """
    cfg = get_runtime_settings()
    ip = cfg.get("robot_ip", "")
    if not ip:
        raise HTTPException(status_code=400, detail="robot_ip is not set in settings")
    result = await _reregister_robot(ip)
    if result.get("ok"):
        logger.info(f"Robot '{ROBOT_ID}' reconnected at {ip}")
    else:
        logger.warning(
            f"Robot '{ROBOT_ID}' reconnect failed at {ip}: "
            f"{result.get('error', 'unknown')}"
        )
    return {
        "status": "ok" if result.get("ok") else "error",
        "ip": ip,
        "result": result,
    }


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
                logger.warning(
                    f"Robot '{ROBOT_ID}' re-register failed at {new_ip}: "
                    f"{result.get('error', 'unknown')}"
                )
        except Exception as e:
            logger.error(f"Re-register raised: {e}")
            response["robot_re_register"] = {"ok": False, "error": str(e)}
    return response


# ═══════════════════════════════════════════════════════════════════════════
# BEDS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/beds")
async def get_beds():
    """Return beds.json (or generate default)."""
    data = load_json(BEDS_FILE, DEFAULT_BEDS)
    if not data or data == {}:
        data = DEFAULT_BEDS
    return data


@router.post("/beds")
async def save_beds(body: dict):
    """Save beds.json."""
    save_json(BEDS_FILE, body)
    return {"status": "ok", "data": body}


# ═══════════════════════════════════════════════════════════════════════════
# PATROL
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/patrol")
async def get_patrol():
    """Return patrol.json."""
    data = load_json(PATROL_FILE, DEFAULT_PATROL)
    if not data or data == {}:
        data = DEFAULT_PATROL
    return data


@router.post("/patrol")
async def save_patrol(body: dict):
    """Save patrol.json. beds_order is sorted by bed_key."""
    if "beds_order" in body:
        body["beds_order"] = sorted(body["beds_order"], key=lambda b: b.get("bed_key", ""))
    save_json(PATROL_FILE, body)
    return {"status": "ok", "data": body}


# --- Patrol presets (save-as / load) ---
PATROL_PRESETS_DIR = os.path.join(get_settings_dir(), "patrol_presets")


@router.get("/patrol/presets")
async def list_patrol_presets():
    """List saved patrol presets."""
    os.makedirs(PATROL_PRESETS_DIR, exist_ok=True)
    cfg = get_runtime_settings()
    demo_preset = cfg.get("demo_preset", "")
    presets = []
    for fname in sorted(os.listdir(PATROL_PRESETS_DIR)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(PATROL_PRESETS_DIR, fname)
        data = load_json(fpath, {})
        name = fname[:-5]  # strip .json
        enabled = [b for b in data.get("beds_order", []) if b.get("enabled")]
        presets.append({"name": name, "beds_count": len(enabled)})
    return {"presets": presets, "demo_preset": demo_preset}


@router.post("/patrol/presets/{name}")
async def save_patrol_preset(name: str):
    """Save current patrol.json as a named preset."""
    os.makedirs(PATROL_PRESETS_DIR, exist_ok=True)
    current = load_json(PATROL_FILE, DEFAULT_PATROL)
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid preset name")
    fpath = os.path.join(PATROL_PRESETS_DIR, f"{safe_name}.json")
    save_json(fpath, current)
    return {"status": "ok", "name": safe_name}


@router.post("/patrol/presets/{name}/load")
async def load_patrol_preset(name: str):
    """Load a named preset into patrol.json."""
    fpath = os.path.join(PATROL_PRESETS_DIR, f"{name}.json")
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Preset not found")
    data = load_json(fpath, {})
    save_json(PATROL_FILE, data)
    return {"status": "ok", "data": data}


@router.delete("/patrol/presets/{name}")
async def delete_patrol_preset(name: str):
    """Delete a named patrol preset."""
    fpath = os.path.join(PATROL_PRESETS_DIR, f"{name}.json")
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Preset not found")
    os.remove(fpath)
    # Clear demo_preset if it was the deleted one
    cfg = get_runtime_settings()
    if cfg.get("demo_preset") == name:
        current = load_json(SETTINGS_FILE, {})
        current["demo_preset"] = ""
        save_json(SETTINGS_FILE, current)
    return {"status": "ok"}


@router.post("/patrol/presets/{name}/set-demo")
async def set_demo_preset(name: str):
    """Set a named preset as the demo route."""
    fpath = os.path.join(PATROL_PRESETS_DIR, f"{name}.json")
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="Preset not found")
    current = load_json(SETTINGS_FILE, {})
    current["demo_preset"] = name
    save_json(SETTINGS_FILE, current)
    return {"status": "ok", "demo_preset": name}


# ═══════════════════════════════════════════════════════════════════════════
# SCHEDULE
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/schedule")
async def get_schedule():
    """Return schedule.json."""
    data = load_json(SCHEDULE_FILE, DEFAULT_SCHEDULE)
    if not data or data == {}:
        data = DEFAULT_SCHEDULE
    return data


@router.post("/schedule")
async def save_schedule(body: dict):
    """Save schedule.json."""
    save_json(SCHEDULE_FILE, body)
    # Reload schedules in the scheduler service
    try:
        from services.scheduler import scheduler_service
        await scheduler_service.reload_from_json()
    except Exception as e:
        logger.warning(f"Failed to reload scheduler after save: {e}")
    return {"status": "ok", "data": body}


@router.delete("/schedule/{schedule_id}")
async def delete_schedule_entry(schedule_id: str):
    """Remove a single schedule entry by its id."""
    data = load_json(SCHEDULE_FILE, DEFAULT_SCHEDULE)
    schedules = data.get("schedules", [])
    original_len = len(schedules)
    schedules = [s for s in schedules if s.get("id") != schedule_id]
    if len(schedules) == original_len:
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    data["schedules"] = schedules
    save_json(SCHEDULE_FILE, data)
    # Reload schedules
    try:
        from services.scheduler import scheduler_service
        await scheduler_service.reload_from_json()
    except Exception as e:
        logger.warning(f"Failed to reload scheduler after delete: {e}")
    return {"status": "ok", "data": data}


# ═══════════════════════════════════════════════════════════════════════════
# PATROL START  (patrol mode vs demo mode)
# ═══════════════════════════════════════════════════════════════════════════

class PatrolStartRequest(BaseModel):
    mode: str = "patrol"  # "patrol" or "demo"


@router.post("/patrol/start")
async def start_patrol(req: PatrolStartRequest):
    """
    Start a patrol run.
    - patrol: move_shelf + bio_scan per enabled bed
    - demo: move_shelf + wait(5s) per enabled bed (no bio_scan)
    """
    cfg = get_runtime_settings()
    shelf_id = cfg.get("shelf_id", "S_04")

    # Demo mode: load from demo preset if configured
    if req.mode == "demo":
        demo_name = cfg.get("demo_preset", "")
        if demo_name:
            demo_path = os.path.join(PATROL_PRESETS_DIR, f"{demo_name}.json")
            patrol_cfg = load_json(demo_path, DEFAULT_PATROL)
        else:
            patrol_cfg = load_json(PATROL_FILE, DEFAULT_PATROL)
    else:
        patrol_cfg = load_json(PATROL_FILE, DEFAULT_PATROL)

    beds_cfg = load_json(BEDS_FILE, DEFAULT_BEDS)
    beds_order = patrol_cfg.get("beds_order", [])
    beds_map = beds_cfg.get("beds", {})

    enabled_beds = [b for b in beds_order if b.get("enabled", False)]
    if not enabled_beds:
        raise HTTPException(status_code=400, detail="No enabled beds in patrol config")

    steps = []
    step_counter = 0

    for bed_entry in enabled_beds:
        bed_key = bed_entry["bed_key"]
        bed_info = beds_map.get(bed_key, {})
        location_id = bed_info.get("location_id", bed_key)

        move_step_id = f"move_{step_counter}"
        action_step_id = f"action_{step_counter}"

        # move_shelf step — skip_on_failure points to the action step
        move_step = TaskStep(
            step_id=move_step_id,
            action="move_shelf",
            params={"shelf_id": shelf_id, "location_id": location_id},
            status=StepStatus.PENDING,
            skip_on_failure=[action_step_id],
        )
        steps.append(move_step)

        if req.mode == "demo":
            action_step = TaskStep(
                step_id=action_step_id,
                action="wait",
                params={"seconds": 5},
                status=StepStatus.PENDING,
            )
        else:
            action_step = TaskStep(
                step_id=action_step_id,
                action="bio_scan",
                params={"bed_key": bed_key},
                status=StepStatus.PENDING,
            )
        steps.append(action_step)
        step_counter += 1

    # Final return_shelf step
    steps.append(TaskStep(
        step_id=f"return_{step_counter}",
        action="return_shelf",
        params={"shelf_id": shelf_id},
        status=StepStatus.PENDING,
    ))

    task = Task(
        task_id=generate_task_id(),
        robot_id="kachaka",
        steps=steps,
        status=TaskStatus.QUEUED,
    )
    tasks_db[task.task_id] = task
    await submit_task(task)
    logger.info(f"Patrol started (mode={req.mode}): task {task.task_id} with {len(enabled_beds)} beds")
    return {"status": "ok", "task_id": task.task_id, "mode": req.mode, "beds_count": len(enabled_beds)}


# ═══════════════════════════════════════════════════════════════════════════
# SHELF DROP RECOVERY
# ═══════════════════════════════════════════════════════════════════════════

class RecoverShelfRequest(BaseModel):
    shelf_id: str


@router.post("/patrol/recover-shelf")
async def recover_shelf(req: RecoverShelfRequest):
    """
    Recovery endpoint: reset shelf pose so the robot can re-dock it.
    Uses reset_shelf_pose (not move_shelf) — only needs shelf_id.
    """
    try:
        from dependencies import get_fleet
        fleet = get_fleet()
        client = fleet.get_raw_client("kachaka")
        result = await asyncio.to_thread(client.reset_shelf_pose, req.shelf_id)

        if result.success:
            # Clear shelf_drop status from any active task
            for task in tasks_db.values():
                if task.status and task.status.value == "shelf_dropped":
                    task.status = TaskStatus.DONE
                    break
            return {"status": "ok", "message": "Shelf pose reset successfully"}
        else:
            return {"status": "error", "message": f"Recovery failed: error {result.error_code}"}
    except Exception as e:
        logger.error(f"Shelf recovery failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════
# SHELF DROP RESUME (recover + continue remaining beds)
# ═══════════════════════════════════════════════════════════════════════════

class ResumePatrolRequest(BaseModel):
    task_id: str


@router.post("/patrol/resume")
async def resume_patrol(req: ResumePatrolRequest):
    """
    Resume patrol after shelf drop:
    1. Reset shelf pose
    2. Mark old task as DONE
    3. Create new task with remaining beds
    4. Submit via submit_task()
    """
    old_task = tasks_db.get(req.task_id)
    if not old_task:
        raise HTTPException(status_code=404, detail=f"Task '{req.task_id}' not found")

    meta = old_task.metadata or {}
    if not meta.get("shelf_drop"):
        raise HTTPException(status_code=400, detail="Task is not in shelf_dropped state")

    shelf_id = meta.get("shelf_id", "")
    remaining_beds = meta.get("remaining_beds", [])
    if not remaining_beds:
        raise HTTPException(status_code=400, detail="No remaining beds to resume")

    # Step 1: Reset shelf pose
    try:
        from dependencies import get_fleet
        fleet = get_fleet()
        client = fleet.get_raw_client("kachaka")
        result = await asyncio.to_thread(client.reset_shelf_pose, shelf_id)
        if not result.success:
            raise HTTPException(status_code=502, detail=f"Shelf reset failed: error {result.error_code}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Resume patrol - shelf reset failed: {e}")
        raise HTTPException(status_code=500, detail=f"Shelf reset failed: {e}")

    # Step 2: Mark old task as DONE
    old_task.status = TaskStatus.DONE

    # Step 3: Build new task with remaining beds
    steps = []
    step_counter = 0
    for bed in remaining_beds:
        bed_key = bed.get("bed_key", "")
        location_id = bed.get("location_id", "")
        if not bed_key or not location_id:
            continue

        move_step_id = f"move_{step_counter}"
        action_step_id = f"action_{step_counter}"

        steps.append(TaskStep(
            step_id=move_step_id,
            action="move_shelf",
            params={"shelf_id": shelf_id, "location_id": location_id},
            status=StepStatus.PENDING,
            skip_on_failure=[action_step_id],
        ))
        steps.append(TaskStep(
            step_id=action_step_id,
            action="bio_scan",
            params={"bed_key": bed_key},
            status=StepStatus.PENDING,
        ))
        step_counter += 1

    if not steps:
        raise HTTPException(status_code=400, detail="No valid beds to resume")

    # Final return_shelf
    steps.append(TaskStep(
        step_id=f"return_{step_counter}",
        action="return_shelf",
        params={"shelf_id": shelf_id},
        status=StepStatus.PENDING,
    ))

    new_task = Task(
        task_id=generate_task_id(),
        robot_id="kachaka",
        steps=steps,
        status=TaskStatus.QUEUED,
    )
    tasks_db[new_task.task_id] = new_task

    # Step 4: Queue
    await submit_task(new_task)
    logger.info(f"Resume patrol: new task {new_task.task_id} with {len(remaining_beds)} beds (from {req.task_id})")

    return {
        "status": "ok",
        "new_task_id": new_task.task_id,
        "beds_count": step_counter,
        "remaining_beds": remaining_beds,
    }


async def resume_latest_shelf_drop() -> dict:
    """Resume the newest shelf-dropped task (used by the Zigbee shelf_resume
    button — caller never has a task_id)."""
    candidates = [
        t for t in tasks_db.values()
        if (t.metadata or {}).get("shelf_drop") and t.status != TaskStatus.DONE
    ]
    if not candidates:
        raise HTTPException(
            status_code=400, detail="No shelf-dropped task to resume"
        )
    latest = max(
        candidates,
        key=lambda t: (t.metadata or {}).get("dropped_at", "") or t.task_id,
    )
    return await resume_patrol(ResumePatrolRequest(task_id=latest.task_id))


@router.post("/patrol/resume-latest")
async def resume_latest_endpoint():
    return await resume_latest_shelf_drop()


# ═══════════════════════════════════════════════════════════════════════════
# MQTT TEST (SSE)
# ═══════════════════════════════════════════════════════════════════════════

def _sse_event(msg: str, level: str = "info") -> str:
    """Format a Server-Sent Event line."""
    payload = json.dumps({"msg": msg, "level": level})
    return f"data: {payload}\n\n"


@router.get("/settings/test-mqtt")
async def test_mqtt():
    """Test MQTT connection — streams log lines via SSE."""
    cfg = get_runtime_settings()
    broker = cfg.get("mqtt_broker", "localhost")
    port = int(cfg.get("mqtt_port", 1883))
    topic = cfg.get("mqtt_topic", "")

    async def generate():
        import paho.mqtt.client as mqtt

        received = []
        connected_event = asyncio.Event()
        connect_error = {}

        def on_connect(client, userdata, flags, rc, properties=None):
            if rc == 0:
                connected_event.set()
            else:
                connect_error["rc"] = rc
                connected_event.set()

        def on_message(client, userdata, msg):
            try:
                payload = msg.payload.decode()
                received.append(payload)
            except Exception:
                received.append(str(msg.payload))

        yield _sse_event(f"Connecting to {broker}:{port}...")
        await asyncio.sleep(0)

        client = mqtt.Client(protocol=mqtt.MQTTv31)
        client.on_connect = on_connect
        client.on_message = on_message

        try:
            await asyncio.to_thread(client.connect, broker, port, 60)
            client.loop_start()
        except Exception as e:
            yield _sse_event(f"Connection failed: {e}", "error")
            yield _sse_event("Test complete.", "done")
            return

        # Wait for connect callback (up to 5s)
        try:
            await asyncio.wait_for(connected_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            yield _sse_event("Connection timed out (5s)", "error")
            client.loop_stop()
            client.disconnect()
            yield _sse_event("Test complete.", "done")
            return

        if connect_error:
            yield _sse_event(f"Connection failed: rc={connect_error['rc']}", "error")
            client.loop_stop()
            client.disconnect()
            yield _sse_event("Test complete.", "done")
            return

        yield _sse_event("Connected successfully")

        yield _sse_event(f"Subscribing to {topic}...")
        await asyncio.to_thread(client.subscribe, topic)
        yield _sse_event("Subscribed. Waiting for data (15s timeout)...")

        # Wait up to 15s for data, checking every second
        for i in range(15):
            await asyncio.sleep(1)
            if received:
                break
            if i % 5 == 4:
                yield _sse_event(f"  Still waiting... ({i + 1}s)")

        if received:
            yield _sse_event(f"Received {len(received)} message(s):")
            for msg_str in received[:5]:
                # Truncate long messages
                display = msg_str[:300] + ("..." if len(msg_str) > 300 else "")
                yield _sse_event(f"  {display}")
        else:
            yield _sse_event("No data received within timeout", "warn")

        yield _sse_event("Disconnecting...")
        client.loop_stop()
        client.disconnect()
        yield _sse_event("Test complete.", "done")

    return StreamingResponse(generate(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════════════════════
# BIO-SCAN TEST (SSE)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/settings/test-bio-scan")
async def test_bio_scan():
    """Simulate a bio-scan cycle using saved settings — streams log lines via SSE."""
    cfg = get_runtime_settings()
    broker = cfg.get("mqtt_broker", "localhost")
    port = int(cfg.get("mqtt_port", 1883))
    topic = cfg.get("mqtt_topic", "")
    wait_time = int(cfg.get("bio_scan_wait_time", 10))
    retry_count = int(cfg.get("bio_scan_retry_count", 19))
    initial_wait = int(cfg.get("bio_scan_initial_wait", 120))
    valid_status = int(cfg.get("bio_scan_valid_status", 4))

    async def generate():
        import paho.mqtt.client as mqtt

        yield _sse_event(
            f"Config: initial_wait={initial_wait}s, retry_count={retry_count}, "
            f"wait_time={wait_time}s, valid_status={valid_status}"
        )

        latest_data = {}
        connected_event = asyncio.Event()
        connect_error = {}

        def on_connect(client, userdata, flags, rc, properties=None):
            if rc == 0:
                connected_event.set()
            else:
                connect_error["rc"] = rc
                connected_event.set()

        def on_message(client, userdata, msg):
            try:
                latest_data["value"] = json.loads(msg.payload.decode())
            except Exception:
                pass

        yield _sse_event(f"Connecting to MQTT {broker}:{port}...")
        await asyncio.sleep(0)

        client = mqtt.Client(protocol=mqtt.MQTTv31)
        client.on_connect = on_connect
        client.on_message = on_message

        try:
            await asyncio.to_thread(client.connect, broker, port, 60)
            client.loop_start()
        except Exception as e:
            yield _sse_event(f"MQTT connection failed: {e}", "error")
            yield _sse_event("Test complete.", "done")
            return

        try:
            await asyncio.wait_for(connected_event.wait(), timeout=5)
        except asyncio.TimeoutError:
            yield _sse_event("MQTT connection timed out", "error")
            client.loop_stop()
            client.disconnect()
            yield _sse_event("Test complete.", "done")
            return

        if connect_error:
            yield _sse_event(f"MQTT connection failed: rc={connect_error['rc']}", "error")
            client.loop_stop()
            client.disconnect()
            yield _sse_event("Test complete.", "done")
            return

        yield _sse_event("MQTT connected")
        await asyncio.to_thread(client.subscribe, topic)
        yield _sse_event(f"Subscribed to {topic}")

        # Initial wait with countdown
        yield _sse_event(f"Starting initial wait ({initial_wait}s)...")
        for elapsed in range(initial_wait):
            await asyncio.sleep(1)
            remaining = initial_wait - elapsed - 1
            if remaining > 0 and remaining % 10 == 0:
                yield _sse_event(f"  Initial wait: {remaining}s remaining...")

        yield _sse_event("Initial wait complete. Starting scan retries...")

        # Retry loop
        valid_data = None
        for i in range(retry_count):
            yield _sse_event(f"Retry {i + 1}/{retry_count}: checking sensor data...")

            data_snapshot = latest_data.get("value")
            if data_snapshot and "records" in data_snapshot:
                for record in data_snapshot["records"]:
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

        client.loop_stop()
        client.disconnect()
        yield _sse_event("Test complete.", "done")

    return StreamingResponse(generate(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════════════════════
# MAP MANAGEMENT
# Uses kachaka-api: get_map_list, load_map_preview, switch_map
# ═══════════════════════════════════════════════════════════════════════════

def _save_map_png_and_meta(map_pb, robot_map_id: str, locations: list, entry_name: str = "") -> dict:
    """Save a protobuf Map to data/maps/ as PNG + JSON. Returns metadata dict."""
    import base64
    from google.protobuf.json_format import MessageToDict

    map_dict = MessageToDict(map_pb, preserving_proto_field_name=True)
    resolution = map_dict.get("resolution", 0.05)
    width = map_dict.get("width", 0)
    height = map_dict.get("height", 0)
    map_name = entry_name or map_dict.get("name", "robot_map")
    origin_dict = map_dict.get("origin", {})

    png_b64 = map_dict.get("data", "")
    png_bytes = base64.b64decode(png_b64) if png_b64 else b""
    if not png_bytes:
        return None

    # Use robot map id as local file id (sanitise for filesystem)
    safe_id = robot_map_id.replace("/", "_").replace("\\", "_")
    os.makedirs(MAPS_DIR, exist_ok=True)

    with open(os.path.join(MAPS_DIR, f"{safe_id}.png"), "wb") as f:
        f.write(png_bytes)

    meta = {
        "robot_map_id": robot_map_id,
        "name": map_name,
        "resolution": resolution,
        "width": width,
        "height": height,
        "origin": {"x": origin_dict.get("x", 0), "y": origin_dict.get("y", 0)},
        "locations": locations,
        "timestamp": datetime.now().isoformat(),
    }
    save_json(os.path.join(MAPS_DIR, f"{safe_id}.json"), meta)
    return {**meta, "id": safe_id}


@router.get("/maps")
async def list_maps():
    """List all locally saved maps + active map."""
    cfg = get_runtime_settings()
    active_map = cfg.get("active_map", "")

    maps = []
    os.makedirs(MAPS_DIR, exist_ok=True)
    for fname in sorted(os.listdir(MAPS_DIR)):
        if fname.endswith(".json"):
            meta_path = os.path.join(MAPS_DIR, fname)
            try:
                meta = load_json(meta_path, {})
                map_id = fname.replace(".json", "")
                maps.append({
                    "id": map_id,
                    "name": meta.get("name", map_id),
                    "robot_map_id": meta.get("robot_map_id", ""),
                    "timestamp": meta.get("timestamp", ""),
                    "resolution": meta.get("resolution"),
                    "width": meta.get("width"),
                    "height": meta.get("height"),
                })
            except Exception:
                pass

    return {"active_map": active_map, "maps": maps}


@router.post("/maps/fetch")
async def fetch_maps_from_robot():
    """Fetch all maps from robot via get_map_list + load_map_preview and save locally."""
    from dependencies import get_fleet

    fleet = get_fleet()

    # 0. Clear old map data
    if os.path.isdir(MAPS_DIR):
        for fname in os.listdir(MAPS_DIR):
            fpath = os.path.join(MAPS_DIR, fname)
            if os.path.isfile(fpath):
                os.remove(fpath)

    # Clear active_map since old files are gone
    current_settings = load_json(SETTINGS_FILE, {})
    if current_settings.get("active_map"):
        current_settings["active_map"] = ""
        save_json(SETTINGS_FILE, current_settings)

    # 1. Get list of maps on robot (kachaka_core returns dict)
    try:
        map_res = await fleet.get_map_list("kachaka")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to get map list: {e}")

    maps = map_res.get("maps", [])
    if not maps:
        return {"status": "ok", "maps": [], "message": "No maps on robot"}

    # 2. Get current map id (included in map_list response)
    current_map_id = map_res.get("current_map_id", "")

    # 3. Get locations (for current map) — kachaka_core returns dict
    locations = []
    try:
        loc_res = await fleet.get_locations("kachaka")
        if loc_res.get("ok"):
            for loc in loc_res.get("locations", []):
                locations.append({
                    "id": loc.get("id", ""),
                    "name": loc.get("name", ""),
                    "pose": loc.get("pose", {}),
                })
    except Exception:
        pass

    # 4. For each map, load preview via raw SDK and save PNG + metadata
    saved = []
    client = fleet.get_raw_client("kachaka")
    for entry in maps:
        robot_map_id = entry.get("id", "")
        entry_name = entry.get("name", "")
        if not robot_map_id:
            continue

        try:
            map_pb = await asyncio.to_thread(client.load_map_preview, robot_map_id)
        except Exception as e:
            logger.warning(f"load_map_preview error for {robot_map_id}: {e}")
            continue

        # Attach locations only for the current map
        locs = locations if robot_map_id == current_map_id else []
        meta = _save_map_png_and_meta(map_pb, robot_map_id, locs, entry_name)
        if meta:
            saved.append({
                "id": meta["id"],
                "name": meta["name"],
                "robot_map_id": robot_map_id,
                "timestamp": meta["timestamp"],
                "resolution": meta["resolution"],
                "width": meta["width"],
                "height": meta["height"],
            })

    return {"status": "ok", "current_robot_map": current_map_id, "maps": saved}


class SwitchMapRequest(BaseModel):
    map_id: str  # local map id (filename without extension)


@router.post("/maps/switch")
async def switch_map(req: SwitchMapRequest):
    """Switch the robot to a different map and set it as Dashboard active map."""
    from dependencies import get_fleet

    meta_path = os.path.join(MAPS_DIR, f"{req.map_id}.json")
    meta = load_json(meta_path, None)
    if not meta:
        raise HTTPException(status_code=404, detail=f"Map '{req.map_id}' not found")

    robot_map_id = meta.get("robot_map_id", "")
    if not robot_map_id:
        raise HTTPException(status_code=400, detail="Map has no robot_map_id — cannot switch")

    fleet = get_fleet()

    # Switch map on robot via raw SDK
    try:
        client = fleet.get_raw_client("kachaka")
        result = await asyncio.to_thread(client.switch_map, robot_map_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"switch_map failed: {e}")

    if not result.success:
        raise HTTPException(
            status_code=502,
            detail=f"switch_map error: code {result.error_code}",
        )

    # Set as active map in settings
    current = load_json(SETTINGS_FILE, {})
    current["active_map"] = req.map_id
    save_json(SETTINGS_FILE, current)

    return {"status": "ok", "active_map": req.map_id, "robot_map_id": robot_map_id}


class SetActiveMapRequest(BaseModel):
    map_id: str


@router.post("/maps/active")
async def set_active_map(req: SetActiveMapRequest):
    """Set active map for Dashboard display only (no robot switch)."""
    meta_path = os.path.join(MAPS_DIR, f"{req.map_id}.json")
    if not os.path.exists(meta_path):
        raise HTTPException(status_code=404, detail=f"Map '{req.map_id}' not found")

    current = load_json(SETTINGS_FILE, {})
    current["active_map"] = req.map_id
    save_json(SETTINGS_FILE, current)
    return {"status": "ok", "active_map": req.map_id}


@router.get("/maps/active-info")
async def get_active_map_info():
    """Return the active map's metadata (or 404 if none set)."""
    cfg = get_runtime_settings()
    map_id = cfg.get("active_map", "")
    if not map_id:
        raise HTTPException(status_code=404, detail="No active map set")

    meta_path = os.path.join(MAPS_DIR, f"{map_id}.json")
    meta = load_json(meta_path, None)
    if not meta:
        raise HTTPException(status_code=404, detail=f"Map metadata not found: {map_id}")

    return {
        "status": "ok",
        "map_id": map_id,
        "name": meta.get("name", ""),
        "resolution": meta.get("resolution", 0.05),
        "width": meta.get("width", 0),
        "height": meta.get("height", 0),
        "origin": meta.get("origin", {"x": 0, "y": 0}),
        "locations": meta.get("locations", []),
    }


@router.get("/maps/{map_id}/image")
async def get_map_image(map_id: str):
    """Serve a saved map PNG file."""
    png_path = os.path.join(MAPS_DIR, f"{map_id}.png")
    if not os.path.exists(png_path):
        raise HTTPException(status_code=404, detail=f"Map image not found: {map_id}")

    with open(png_path, "rb") as f:
        png_bytes = f.read()
    return Response(content=png_bytes, media_type="image/png")
