"""Schedule (cron) endpoints. Mutations re-load the live scheduler."""
import logging

from fastapi import APIRouter, HTTPException

from settings.config import SCHEDULE_FILE
from settings.defaults import DEFAULT_SCHEDULE
from utils.json_io import load_json, save_json

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Schedule"])


async def _reload_scheduler() -> None:
    """Re-read schedule.json and rebuild scheduler jobs. Logs but does not raise."""
    try:
        from services.scheduler import scheduler_service
        await scheduler_service.reload_from_json()
    except Exception as e:
        logger.warning(f"Scheduler reload failed: {e}")


@router.get("/schedule")
async def get_schedule():
    """Return schedule.json (or defaults if empty/missing)."""
    data = load_json(SCHEDULE_FILE, DEFAULT_SCHEDULE)
    return data or DEFAULT_SCHEDULE


@router.post("/schedule")
async def save_schedule(body: dict):
    """Save schedule.json and reload the scheduler."""
    save_json(SCHEDULE_FILE, body)
    await _reload_scheduler()
    return {"status": "ok", "data": body}


@router.delete("/schedule/{schedule_id}")
async def delete_schedule_entry(schedule_id: str):
    """Remove a single schedule entry by id and reload the scheduler."""
    data = load_json(SCHEDULE_FILE, DEFAULT_SCHEDULE)
    schedules = data.get("schedules", [])
    remaining = [s for s in schedules if s.get("id") != schedule_id]
    if len(remaining) == len(schedules):
        raise HTTPException(status_code=404, detail=f"Schedule '{schedule_id}' not found")
    data["schedules"] = remaining
    save_json(SCHEDULE_FILE, data)
    await _reload_scheduler()
    return {"status": "ok", "data": data}
