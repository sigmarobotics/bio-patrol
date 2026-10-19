"""Patrol endpoints — start / resume / shelf-drop recovery / patrol presets."""
import asyncio
import logging
import os
from typing import List, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from settings.config import (
    BEDS_FILE, PATROL_FILE, PATROL_PRESETS_DIR,
    get_runtime_settings, update_settings,
)
from settings.defaults import DEFAULT_BEDS, DEFAULT_PATROL
from utils.grpc_errors import is_connection_error
from utils.json_io import load_json, save_json
from common_types import (
    StepAction, StepStatus, Task, TaskStatus, TaskStep, generate_task_id,
)
from dependencies import get_bio_sensor_client
from services import announcer, demo_data
from services.bio_sensor_mqtt import (
    SENSOR_FRESH_WITHIN_S, SENSOR_OFFLINE_PREFIX, SENSOR_SILENT_PREFIX,
)
from services.fleet_api import RobotNotRegistered
from services.task_runtime import clear_shelf_dropped_tasks, submit_task, tasks_db

logger = logging.getLogger(__name__)

PatrolMode = Literal["demo", "patrol"]
_PRESET_MISSING = object()
ARRIVAL_SOUND = "arrival_zh"

router = APIRouter(prefix="/api", tags=["Patrol"])

# IT-23: the sensor gate can wait up to 30 s, so two starts inside that window
# would both pass a dedup run before it. The whole start body is serialized.
_start_lock = asyncio.Lock()


# ── Patrol config ────────────────────────────────────────────────────────────

def _route_file(request: Request, *, for_write: bool = False) -> str:
    """The route file a request edits. The /demo view (``X-Bio-Data: demo``)
    works on the demo route — the `demo_preset` preset — never patrol.json.
    With no demo preset yet it reads the real route, and its first write
    creates one named "demo"."""
    if request.headers.get("x-bio-data") != "demo":
        return PATROL_FILE
    name = get_runtime_settings().get("demo_preset", "")
    if not name:
        if not for_write:
            return PATROL_FILE
        name = "demo"
        os.makedirs(PATROL_PRESETS_DIR, exist_ok=True)
        update_settings(demo_preset=name)
    return os.path.join(PATROL_PRESETS_DIR, f"{name}.json")


@router.get("/patrol")
async def get_patrol(request: Request):
    """Return patrol.json (or defaults if empty/missing)."""
    data = load_json(_route_file(request), DEFAULT_PATROL)
    return data or DEFAULT_PATROL


@router.post("/patrol")
async def save_patrol(body: dict, request: Request):
    """Save patrol.json. beds_order is persisted in caller-supplied order —
    it IS the patrol route (e.g. odd-side rooms first, then even side)."""
    save_json(_route_file(request, for_write=True), body)
    return {"status": "ok", "data": body}


# ── Patrol presets (save-as / load) ──────────────────────────────────────────

@router.get("/patrol/presets")
async def list_patrol_presets():
    """List saved patrol presets and the currently designated demo preset."""
    os.makedirs(PATROL_PRESETS_DIR, exist_ok=True)
    cfg = get_runtime_settings()
    demo_preset = cfg.get("demo_preset", "")
    presets = []
    for fname in sorted(os.listdir(PATROL_PRESETS_DIR)):
        if not fname.endswith(".json"):
            continue
        data = load_json(os.path.join(PATROL_PRESETS_DIR, fname), {})
        enabled = [b for b in data.get("beds_order", []) if b.get("enabled")]
        presets.append({"name": fname[:-5], "beds_count": len(enabled)})
    return {"presets": presets, "demo_preset": demo_preset}


@router.post("/patrol/presets/{name}")
async def save_patrol_preset(name: str, request: Request):
    """Save current patrol.json as a named preset."""
    os.makedirs(PATROL_PRESETS_DIR, exist_ok=True)
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid preset name")
    current = load_json(_route_file(request), DEFAULT_PATROL)
    save_json(os.path.join(PATROL_PRESETS_DIR, f"{safe_name}.json"), current)
    return {"status": "ok", "name": safe_name}


@router.post("/patrol/presets/{name}/load")
async def load_patrol_preset(name: str, request: Request):
    """Load a named preset into patrol.json."""
    fpath = os.path.join(PATROL_PRESETS_DIR, f"{name}.json")
    data = load_json(fpath, _PRESET_MISSING)
    if data is _PRESET_MISSING:
        raise HTTPException(status_code=404, detail="Preset not found")
    save_json(_route_file(request, for_write=True), data)
    return {"status": "ok", "data": data}


@router.delete("/patrol/presets/{name}")
async def delete_patrol_preset(name: str):
    """Delete a named patrol preset (and clear demo_preset if it was this one)."""
    fpath = os.path.join(PATROL_PRESETS_DIR, f"{name}.json")
    try:
        os.remove(fpath)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Preset not found")
    if get_runtime_settings().get("demo_preset") == name:
        update_settings(demo_preset="")
    return {"status": "ok"}


@router.post("/patrol/presets/{name}/set-demo")
async def set_demo_preset(name: str):
    """Mark a named preset as the demo route."""
    fpath = os.path.join(PATROL_PRESETS_DIR, f"{name}.json")
    if load_json(fpath, _PRESET_MISSING) is _PRESET_MISSING:
        raise HTTPException(status_code=404, detail="Preset not found")
    update_settings(demo_preset=name)
    return {"status": "ok", "demo_preset": name}


# ── Patrol step builder ──────────────────────────────────────────────────────

def build_patrol_steps(beds: List[dict], shelf_id: str, *, mode: PatrolMode,
                       final_wait_seconds: int = 5,
                       arrival_voice: bool = False,
                       demo_dwell_seconds: float = 15,
                       demo_synthetic: bool = True) -> List[TaskStep]:
    """Build a reset_shelf_pose + (move_shelf -> action -> ...)+ + return_shelf list.

    `beds`: ordered list of {bed_key, location_id} for the run. Empty/invalid
            entries are skipped silently (resume_patrol may carry partial dicts).
    `mode`: "demo" -> action is demo_scan (IT-21 synthetic scan, scenario by
            route order); "patrol" -> bio_scan.
    `final_wait_seconds`: demo only — extends the dwell at the LAST bed (site
            demos park the robot at the endpoint for the audience before
            auto-returning); it never shortens it below the regular dwell.
    `arrival_voice`: insert a play_sound step on arrival at each bed. In patrol
            mode play_sound is fire-and-forget, so the bio_scan starts while
            the clip plays; a demo step waits the clip out (IT-24), so the
            robot never drives off mid-sentence.
    `demo_dwell_seconds`: demo only — how long each seat shows 量測中.
    `demo_synthetic`: demo only — False makes a walk-only demo (IT-22, 板榮):
            a 5s wait per bed instead of demo_scan, no data and no alerts.

    The shelf sits at its home whenever a run starts, so the run opens by
    resetting the robot's shelf-pose estimate — a drifted estimate is what
    turns into move_shelf 11005 and phantom drop alerts mid-patrol.
    """
    steps: List[TaskStep] = []
    counter = 0
    for bed in beds:
        bed_key = bed.get("bed_key", "")
        location_id = bed.get("location_id", "")
        if not bed_key or not location_id:
            continue

        move_id = f"move_{counter}"
        action_id = f"action_{counter}"
        voice_id = f"voice_{counter}"
        steps.append(TaskStep(
            step_id=move_id,
            action=StepAction.MOVE_SHELF.value,
            params={"shelf_id": shelf_id, "location_id": location_id},
            status=StepStatus.PENDING,
            skip_on_failure=[voice_id, action_id] if arrival_voice else [action_id],
        ))
        if arrival_voice:
            steps.append(TaskStep(
                step_id=voice_id,
                action=StepAction.PLAY_SOUND.value,
                params={"sound_name": ARRIVAL_SOUND,
                        **({"wait": True} if mode == "demo" else {})},
                status=StepStatus.PENDING,
            ))
        if mode == "demo" and not demo_synthetic:
            steps.append(TaskStep(
                step_id=action_id,
                action=StepAction.WAIT.value,
                params={"bed_key": bed_key, "seconds": 5},
                status=StepStatus.PENDING,
            ))
        elif mode == "demo":
            steps.append(TaskStep(
                step_id=action_id,
                action=StepAction.DEMO_SCAN.value,
                params={
                    "bed_key": bed_key,
                    "location_id": location_id,
                    "scenario": demo_data.scenario_for(counter),
                    "seconds": demo_dwell_seconds,
                },
                status=StepStatus.PENDING,
            ))
        else:
            steps.append(TaskStep(
                step_id=action_id,
                action=StepAction.BIO_SCAN.value,
                params={"bed_key": bed_key},
                status=StepStatus.PENDING,
            ))
        counter += 1

    if steps:
        if mode == "demo" and not demo_synthetic:
            steps[-1].params["seconds"] = final_wait_seconds
        elif mode == "demo":
            # Only the dwell changes — the last seat keeps its scan params.
            steps[-1].params["seconds"] = max(demo_dwell_seconds, final_wait_seconds)
        steps.insert(0, TaskStep(
            step_id="reset_shelf",
            action=StepAction.RESET_SHELF_POSE.value,
            params={"shelf_id": shelf_id},
            status=StepStatus.PENDING,
        ))
        steps.append(TaskStep(
            step_id=f"return_{counter}",
            action=StepAction.RETURN_SHELF.value,
            params={"shelf_id": shelf_id},
            status=StepStatus.PENDING,
        ))
    return steps


# ── Patrol start (patrol vs demo) ────────────────────────────────────────────

class PatrolStartRequest(BaseModel):
    mode: PatrolMode = "patrol"


def _robot_offline(cfg: dict) -> bool:
    """Robot confirmed offline? A blip still inside the debounce grace window
    counts as online — the 300s window exists exactly so mesh-WiFi hiccups
    don't cancel a whole night's run. Fail-open: a state query that itself
    blows up must not block the patrol (same philosophy as the battery gate).
    ``cfg`` comes from the caller's already-loaded runtime settings — this
    runs on every start, and get_runtime_settings() is a disk read.
    """
    try:
        from dependencies import get_fleet
        state = get_fleet().get_connection_state_dict(
            "kachaka",
            debounce_seconds=int(cfg.get("robot_offline_debounce_seconds", 300)),
        )
        if state.get("state") == "connected":
            return False
        return not state.get("offline_pending", False)
    except Exception as e:
        logger.warning(f"Connection-state check failed, starting patrol anyway: {e}")
        return False


def _live_run(req: PatrolStartRequest, beds_count: int):
    """Dedup: a patrol-family run already queued/executing means this start is a
    duplicate — an impatient operator, a schedule firing onto a manual run, a
    demo button pressed mid-patrol, a resume still going. There is one robot
    and one shelf, so the mode is not what conflicts; any live run is. Return
    that run instead of queueing a second one on the same shelf behind it.
    Tasks without a mode are not patrols (generic /api/tasks submissions)."""
    for existing in tasks_db.values():
        if existing.status not in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS):
            continue
        running_mode = (existing.metadata or {}).get("mode")
        if not running_mode:
            continue
        logger.warning(
            f"Patrol start ignored (mode={req.mode}): task {existing.task_id} "
            f"(mode={running_mode}) is already {existing.status.value}"
        )
        return {
            "status": "already_running",
            "task_id": existing.task_id,
            "mode": running_mode,
            "beds_count": beds_count,
        }
    return None


async def _refuse(status: int, detail: str, sound: str) -> None:
    """IT-24: say the refusal out loud on the robot, then refuse. The ward
    staff who pressed the button never see the web page or Telegram. ``detail``
    must stay word-for-word — scheduler._refusal_reason parses its prefix. A
    broken announcement must never turn a refusal into a start."""
    try:
        await announcer.announce(sound)
    except Exception as e:
        logger.warning(f"Refusal announcement '{sound}' failed: {e}")
    raise HTTPException(status_code=status, detail=detail)


async def _sensor_gate(cfg: dict) -> None:
    """IT-23: refuse a patrol whose Wisleep pad is silent. The pad publishes
    every ~2.5 s even with nobody on it, so message arrival — not the broker
    connection — is the liveness signal. A wait that itself blows up fails
    open (same philosophy as the battery gate)."""
    wait_s = cfg.get("sensor_precheck_wait_seconds", 30) or 0
    if not cfg.get("mqtt_enabled") or wait_s <= 0:
        return
    client = get_bio_sensor_client()
    if client is None:
        await _refuse(
            503, f"{SENSOR_OFFLINE_PREFIX}：請檢查網路或憑證設定",
            announcer.SOUND_SENSOR_OFFLINE,
        )
    # Wait even when the broker link is down right now: paho reconnects in the
    # background within seconds (sensor.log rc=16 blips), and a pad message
    # inside the wait proves the pad alive. Only a silent wait is refused, and
    # the link state then decides which problem the notice names.
    try:
        alive = await client.wait_for_message(SENSOR_FRESH_WITHIN_S, wait_s)
    except Exception as e:
        logger.warning(f"Sensor liveness check failed, starting patrol anyway: {e}")
        return
    if not alive and not client.connected:
        await _refuse(
            503, f"{SENSOR_OFFLINE_PREFIX}：請檢查網路或憑證設定",
            announcer.SOUND_SENSOR_OFFLINE,
        )
    if not alive:
        await _refuse(
            503,
            f"{SENSOR_SILENT_PREFIX}：{wait_s} 秒內沒有收到感測墊訊息，"
            "請檢查感測棚車上的行動電源是否沒電或沒接",
            announcer.SOUND_SENSOR_SILENT,
        )


def _remove_pending_retries() -> None:
    """A patrol is running now, so a scheduled sensor retry must not fire later
    (e.g. after the operator cancels this run)."""
    try:
        from services.scheduler import scheduler_service
        for job in scheduler_service.scheduler.get_jobs():
            if job.id.endswith("_retry"):
                scheduler_service.scheduler.remove_job(job.id)
                logger.info(f"Removed pending scheduler retry: {job.id}")
    except Exception as e:
        logger.warning(f"Could not remove pending scheduler retries: {e}")


@router.post("/patrol/start")
async def start_patrol(req: PatrolStartRequest):
    """Start a patrol run. Demo mode loads the demo_preset (if any) and runs a
    synthetic demo_scan per bed instead of bio_scan (IT-21)."""
    async with _start_lock:
        return await _start_patrol_locked(req)


async def _start_patrol_locked(req: PatrolStartRequest):
    cfg = get_runtime_settings()
    shelf_id = cfg.get("shelf_id", "S_04")

    if req.mode == "demo":
        demo_name = cfg.get("demo_preset", "")
        if demo_name:
            patrol_cfg = load_json(os.path.join(PATROL_PRESETS_DIR, f"{demo_name}.json"), DEFAULT_PATROL)
        else:
            patrol_cfg = load_json(PATROL_FILE, DEFAULT_PATROL)
    else:
        patrol_cfg = load_json(PATROL_FILE, DEFAULT_PATROL)

    beds_cfg = load_json(BEDS_FILE, DEFAULT_BEDS)
    beds_map = beds_cfg.get("beds", {})
    enabled = [b for b in patrol_cfg.get("beds_order", []) if b.get("enabled", False)]
    if not enabled:
        raise HTTPException(status_code=400, detail="No enabled beds in patrol config")

    beds = [
        {"bed_key": b["bed_key"], "location_id": beds_map.get(b["bed_key"], {}).get("location_id", b["bed_key"])}
        for b in enabled
    ]
    live = _live_run(req, len(enabled))
    if live:
        return live

    # Offline gate — a run started against a confirmed-offline robot cannot
    # move a single step; all it produces is one more shelf_dropped(disconnect)
    # alert minutes later (新營/板榮 backlog). Sits in start_patrol so manual,
    # demo and scheduled starts are all refused up front with the same words.
    if _robot_offline(cfg):
        raise HTTPException(
            status_code=503,
            detail="機器人失聯，無法啟動巡房，請確認機器人電源與網路",
        )

    # Battery gate — fail-open: a robot we cannot query is a robot whose patrol
    # will fail on its own; blocking start adds nothing. Only the read is
    # fail-open: the refusal sits outside the try, so nothing on the announce
    # path can let a low-battery patrol start.
    min_battery = cfg.get("patrol_min_battery_pct", 30)
    low = False
    try:
        from dependencies import get_fleet
        battery = await get_fleet().get_battery_info("kachaka")
        pct = battery.get("percentage")
        low = pct is not None and pct < min_battery
    except Exception as e:
        logger.warning(f"Battery check failed, starting patrol anyway: {e}")
    if low:
        await _refuse(
            400, f"Battery too low to start patrol: {pct:.0f}% < {min_battery}%",
            announcer.SOUND_BATTERY_LOW,
        )

    # Sensor gate — demo runs never read the pad, so only patrol is checked.
    if req.mode == "patrol":
        await _sensor_gate(cfg)

    # The gates awaited; resume_patrol (not under the lock) may have queued a
    # run meanwhile, so check again with no await before the task is created.
    live = _live_run(req, len(enabled))
    if live:
        return live

    demo_synthetic = bool(cfg.get("demo_synthetic", False))
    steps = build_patrol_steps(
        beds, shelf_id, mode=req.mode,
        final_wait_seconds=cfg.get("demo_final_wait_seconds", 5),
        arrival_voice=cfg.get("arrival_voice_enabled", False),
        demo_dwell_seconds=cfg.get("demo_dwell_seconds", 15),
        demo_synthetic=demo_synthetic,
    )
    if req.mode == "demo" and demo_synthetic:
        # /demo's history tab and bed drawer show a 30-day synthetic trend.
        try:
            demo_data.ensure_history(beds, cfg)
        except Exception:
            logger.exception("Demo history back-fill failed — demo run continues")

    task = Task(
        task_id=generate_task_id(),
        robot_id="kachaka",
        steps=steps,
        status=TaskStatus.QUEUED,
        metadata={"mode": req.mode, **({"demo_synthetic": demo_synthetic} if req.mode == "demo" else {})},
    )
    tasks_db[task.task_id] = task
    await submit_task(task)
    if req.mode == "patrol":
        _remove_pending_retries()
    logger.info(f"Patrol started (mode={req.mode}): task {task.task_id} with {len(enabled)} beds")
    return {"status": "ok", "task_id": task.task_id, "mode": req.mode, "beds_count": len(enabled)}


# ── Shelf-drop recovery / resume ─────────────────────────────────────────────

class RecoverShelfRequest(BaseModel):
    shelf_id: str


@router.post("/patrol/recover-shelf")
async def recover_shelf(req: RecoverShelfRequest):
    """Reset shelf pose so the robot can re-dock it. Marks every
    shelf_dropped task DONE so the dashboard clears the alert — an offline
    weekend piles up one dropped task per fired schedule, and clearing one
    per press made the backlog look unclearable (新營/板榮 2026-08)."""
    try:
        from dependencies import get_fleet
        fleet = get_fleet()
        client = fleet.get_raw_client("kachaka")
        result = await asyncio.to_thread(client.reset_shelf_pose, req.shelf_id)
        if not result.success:
            return {"status": "error", "message": f"Recovery failed: error {result.error_code}"}
        clear_shelf_dropped_tasks(reason="manual recover-shelf")
        return {"status": "ok", "message": "Shelf pose reset successfully"}
    except Exception as e:
        logger.error(f"Shelf recovery failed: {e}")
        if is_connection_error(e) or isinstance(e, RobotNotRegistered):
            # An unreachable robot is not a server fault — and a raw 500 with a
            # gRPC traceback tells the ward nurse nothing actionable. Covers
            # RobotNotRegistered too: on this single-robot appliance the only
            # way registration is missing is that the robot was offline at boot.
            raise HTTPException(
                status_code=503, detail="機器人失聯，請先確認機器人電源與網路"
            )
        raise HTTPException(status_code=500, detail=str(e))


class ResumePatrolRequest(BaseModel):
    task_id: str


@router.post("/patrol/resume")
async def resume_patrol(req: ResumePatrolRequest):
    """Resume patrol after a shelf drop: reset shelf pose, mark old task DONE,
    queue a new task with the remaining beds."""
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
        if is_connection_error(e) or isinstance(e, RobotNotRegistered):
            # Same failure as recover-shelf, so the same answer: 503 plus text
            # the ward can act on, not a raw gRPC string.
            raise HTTPException(
                status_code=503, detail="機器人失聯，請先確認機器人電源與網路"
            )
        raise HTTPException(status_code=500, detail=f"Shelf reset failed: {e}")

    old_task.status = TaskStatus.DONE

    steps = build_patrol_steps(
        remaining_beds, shelf_id, mode="patrol",
        arrival_voice=get_runtime_settings().get("arrival_voice_enabled", False),
    )
    if not steps:
        raise HTTPException(status_code=400, detail="No valid beds to resume")

    new_task = Task(
        task_id=generate_task_id(),
        robot_id="kachaka",
        steps=steps,
        status=TaskStatus.QUEUED,
        # Same mode tag as a fresh start, so a resumed run is visible to the
        # start_patrol dedup and nobody starts a second run on the same shelf.
        metadata={"mode": "patrol"},
    )
    tasks_db[new_task.task_id] = new_task
    await submit_task(new_task)
    bio_scan_count = sum(1 for s in steps if s.action == StepAction.BIO_SCAN.value)
    logger.info(f"Resume patrol: new task {new_task.task_id} with {bio_scan_count} beds (from {req.task_id})")
    return {
        "status": "ok",
        "new_task_id": new_task.task_id,
        "beds_count": bio_scan_count,
        "remaining_beds": remaining_beds,
    }


@router.post("/patrol/resume-latest")
async def resume_latest_shelf_drop() -> dict:
    """Resume the newest shelf-dropped task. Used by the Zigbee shelf_resume
    button — caller never has a task_id."""
    candidates = [
        t for t in tasks_db.values()
        if (t.metadata or {}).get("shelf_drop") and t.status != TaskStatus.DONE
    ]
    if not candidates:
        raise HTTPException(status_code=400, detail="No shelf-dropped task to resume")
    latest = max(
        candidates,
        key=lambda t: (t.metadata or {}).get("dropped_at", "") or t.task_id,
    )
    return await resume_patrol(ResumePatrolRequest(task_id=latest.task_id))
