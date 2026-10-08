"""IT-21 /demo panel endpoints (ARCH-035).

Read ONLY demo_data.db, the in-memory task store and the robot connection
state — never sensor_data.db, so nothing a visitor sees can be a resident's
real reading.

/live renders entirely from demo_data.db when no demo task is in memory (idle,
or after a restart): that is the panel's "last demo result" state. A demo task
in tasks_db overlays live step status (pending / measuring) on top.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends

from common_types import StepAction, StepStatus, Task, TaskStatus, get_now
from dependencies import get_fleet
from services import demo_data
from services.task_runtime import tasks_db
from settings.config import get_runtime_settings

router = APIRouter(prefix="/api/demo", tags=["Demo"])

ROBOT_ID = "kachaka"
_FINISHED = ("normal", "abnormal", "absent", "skipped")


def _latest_demo_task() -> Optional[Task]:
    # Keyed on the steps, not metadata.mode: a shelf drop rewrites metadata.
    demos = [
        t for t in list(tasks_db.values())
        if any(s.action == StepAction.DEMO_SCAN.value for s in t.steps)
    ]
    return max(demos, key=lambda t: t.task_id) if demos else None


def _row_fields(row: dict, cfg: dict) -> dict:
    return {
        "state": demo_data.seat_state(row, cfg),
        "bpm": row.get("bpm"),
        "rpm": row.get("rpm"),
        "status": row.get("status"),
        "timestamp": row.get("timestamp"),
    }


def _seat(bed_key, location_id, scenario, seconds) -> dict:
    return {
        "bed_key": bed_key, "location_id": location_id, "scenario": scenario,
        "seconds": seconds, "state": "pending", "started_at": None,
        "remaining_seconds": None, "bpm": None, "rpm": None, "status": None,
        "timestamp": None,
    }


def _task_seats(task: Task, cfg: dict) -> list[dict]:
    rows = {r["bed_name"]: r for r in demo_data.scan_rows(task.task_id)}
    now = get_now()
    seats = []
    for i, step in enumerate(task.steps):
        if step.action != StepAction.DEMO_SCAN.value:
            continue
        p = step.params
        seat = _seat(p.get("bed_key"), p.get("location_id"), p.get("scenario"),
                     p.get("seconds"))
        row = rows.get(p.get("bed_key"))
        if row is not None:
            seat.update(_row_fields(row, cfg))
        elif step.status == StepStatus.EXECUTING:
            # TaskStep carries no start time; the dwell starts when the step
            # before it (move_shelf / play_sound) finished.
            prev = task.steps[i - 1] if i > 0 else None
            started = prev.result.timestamp if prev and prev.result else None
            seconds = float(p.get("seconds") or 0)
            remaining = seconds
            if started:
                try:
                    elapsed = (now - datetime.fromisoformat(started)).total_seconds()
                    remaining = max(0.0, seconds - elapsed)
                except ValueError:
                    started = None
            seat.update(state="measuring", started_at=started or None,
                        remaining_seconds=round(remaining, 1))
        elif step.status != StepStatus.PENDING:
            seat["state"] = "skipped"   # move failed / run interrupted
        seats.append(seat)
    return seats


async def _robot(fleet, cfg: dict) -> dict:
    try:
        st = fleet.get_connection_state_dict(
            ROBOT_ID,
            debounce_seconds=int(cfg.get("robot_offline_debounce_seconds", 300)),
        )
    except Exception:
        st = {"state": "unknown"}
    connected = st.get("state") == "connected"
    pose = None
    if connected:
        try:
            cs = await fleet.get_controller_state(ROBOT_ID)
            if cs and cs.get("last_updated"):
                pose = {"x": cs.get("pose_x"), "y": cs.get("pose_y"),
                        "theta": cs.get("pose_theta")}
        except Exception:
            pose = None
    return {
        "state": st.get("state", "unknown"),
        "connected": connected,
        "offline_pending": bool(st.get("offline_pending", False)),
        "pose": pose,
    }


@router.get("/live")
async def demo_live(fleet=Depends(get_fleet)) -> dict:
    cfg = get_runtime_settings()
    task = _latest_demo_task()
    if task is not None:
        task_id = task.task_id
        seats = _task_seats(task, cfg)
        running = task.status in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS)
        task_status = task.status.value
    else:
        task_id = demo_data.latest_run_task_id()
        seats = []
        for row in demo_data.scan_rows(task_id) if task_id else []:
            seat = _seat(row["bed_name"], row["location_id"], row["scenario"], None)
            seat.update(_row_fields(row, cfg))
            seats.append(seat)
        running = False
        task_status = None
    return {
        "task_id": task_id,
        "task_status": task_status,
        "running": running,
        "seats": seats,
        "progress": {
            "done": sum(1 for s in seats if s["state"] in _FINISHED),
            "total": len(seats),
        },
        "robot": await _robot(fleet, cfg),
        "thresholds": {
            "hr_low": cfg.get("vitals_hr_low", 50),
            "hr_high": cfg.get("vitals_hr_high", 120),
            "rr_low": cfg.get("vitals_rr_low", 10),
            "rr_high": cfg.get("vitals_rr_high", 30),
        },
        "server_time": get_now().isoformat(),
    }


@router.get("/history/{bed_key}")
def demo_history(bed_key: str) -> dict:
    points = [
        {**r, "is_valid": bool(r["is_valid"])} for r in demo_data.history_rows(bed_key)
    ]
    valid = [p for p in points if p["is_valid"]]

    def _avg(key):
        return round(sum(p[key] for p in valid) / len(valid), 1) if valid else None

    return {"bed_key": bed_key, "points": points,
            "average": {"bpm": _avg("bpm"), "rpm": _avg("rpm")}}


@router.get("/notifications")
def demo_notifications(task_id: Optional[str] = None) -> dict:
    tid, items = demo_data.notification_rows(task_id)
    return {"task_id": tid, "items": items}
