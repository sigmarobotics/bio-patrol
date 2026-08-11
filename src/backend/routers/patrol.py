"""Patrol endpoints — start / resume / shelf-drop recovery / patrol presets."""
import asyncio
import logging
import os
from typing import List, Literal

from fastapi import APIRouter, HTTPException
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
from services.fleet_api import RobotNotRegistered
from services.task_runtime import submit_task, tasks_db

logger = logging.getLogger(__name__)

PatrolMode = Literal["demo", "patrol"]
_PRESET_MISSING = object()

router = APIRouter(prefix="/api", tags=["Patrol"])


# ── Patrol config ────────────────────────────────────────────────────────────

@router.get("/patrol")
async def get_patrol():
    """Return patrol.json (or defaults if empty/missing)."""
    data = load_json(PATROL_FILE, DEFAULT_PATROL)
    return data or DEFAULT_PATROL


@router.post("/patrol")
async def save_patrol(body: dict):
    """Save patrol.json. beds_order is persisted in caller-supplied order —
    it IS the patrol route (e.g. odd-side rooms first, then even side)."""
    save_json(PATROL_FILE, body)
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
async def save_patrol_preset(name: str):
    """Save current patrol.json as a named preset."""
    os.makedirs(PATROL_PRESETS_DIR, exist_ok=True)
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid preset name")
    current = load_json(PATROL_FILE, DEFAULT_PATROL)
    save_json(os.path.join(PATROL_PRESETS_DIR, f"{safe_name}.json"), current)
    return {"status": "ok", "name": safe_name}


@router.post("/patrol/presets/{name}/load")
async def load_patrol_preset(name: str):
    """Load a named preset into patrol.json."""
    fpath = os.path.join(PATROL_PRESETS_DIR, f"{name}.json")
    data = load_json(fpath, _PRESET_MISSING)
    if data is _PRESET_MISSING:
        raise HTTPException(status_code=404, detail="Preset not found")
    save_json(PATROL_FILE, data)
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

def build_patrol_steps(beds: List[dict], shelf_id: str, *, mode: PatrolMode) -> List[TaskStep]:
    """Build a reset_shelf_pose + (move_shelf -> action -> ...)+ + return_shelf list.

    `beds`: ordered list of {bed_key, location_id} for the run. Empty/invalid
            entries are skipped silently (resume_patrol may carry partial dicts).
    `mode`: "demo" -> action is wait(5s); "patrol" -> bio_scan.

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
        steps.append(TaskStep(
            step_id=move_id,
            action=StepAction.MOVE_SHELF.value,
            params={"shelf_id": shelf_id, "location_id": location_id},
            status=StepStatus.PENDING,
            skip_on_failure=[action_id],
        ))
        if mode == "demo":
            steps.append(TaskStep(
                step_id=action_id,
                action=StepAction.WAIT.value,
                params={"seconds": 5},
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


@router.post("/patrol/start")
async def start_patrol(req: PatrolStartRequest):
    """Start a patrol run. Demo mode loads the demo_preset (if any) and uses
    wait(5s) per bed instead of bio_scan."""
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
    # Dedup: a patrol-family run already queued/executing means this start is a
    # duplicate — an impatient operator, a schedule firing onto a manual run, a
    # demo button pressed mid-patrol, a resume still going. There is one robot
    # and one shelf, so the mode is not what conflicts; any live run is. Return
    # that run instead of queueing a second one on the same shelf behind it.
    # Tasks without a mode are not patrols (generic /api/tasks submissions).
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
            "beds_count": len(enabled),
        }

    # Battery gate — fail-open: a robot we cannot query is a robot whose patrol
    # will fail on its own; blocking start adds nothing.
    min_battery = cfg.get("patrol_min_battery_pct", 30)
    try:
        from dependencies import get_fleet
        battery = await get_fleet().get_battery_info("kachaka")
        pct = battery.get("percentage")
        if pct is not None and pct < min_battery:
            raise HTTPException(
                status_code=400,
                detail=f"Battery too low to start patrol: {pct:.0f}% < {min_battery}%",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Battery check failed, starting patrol anyway: {e}")

    steps = build_patrol_steps(beds, shelf_id, mode=req.mode)

    task = Task(
        task_id=generate_task_id(),
        robot_id="kachaka",
        steps=steps,
        status=TaskStatus.QUEUED,
        metadata={"mode": req.mode},
    )
    tasks_db[task.task_id] = task
    await submit_task(task)
    logger.info(f"Patrol started (mode={req.mode}): task {task.task_id} with {len(enabled)} beds")
    return {"status": "ok", "task_id": task.task_id, "mode": req.mode, "beds_count": len(enabled)}


# ── Shelf-drop recovery / resume ─────────────────────────────────────────────

class RecoverShelfRequest(BaseModel):
    shelf_id: str


@router.post("/patrol/recover-shelf")
async def recover_shelf(req: RecoverShelfRequest):
    """Reset shelf pose so the robot can re-dock it. Marks any active
    shelf_dropped task as DONE so the dashboard clears the alert."""
    try:
        from dependencies import get_fleet
        fleet = get_fleet()
        client = fleet.get_raw_client("kachaka")
        result = await asyncio.to_thread(client.reset_shelf_pose, req.shelf_id)
        if not result.success:
            return {"status": "error", "message": f"Recovery failed: error {result.error_code}"}
        for task in tasks_db.values():
            if task.status == TaskStatus.SHELF_DROPPED:
                task.status = TaskStatus.DONE
                break
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

    steps = build_patrol_steps(remaining_beds, shelf_id, mode="patrol")
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
