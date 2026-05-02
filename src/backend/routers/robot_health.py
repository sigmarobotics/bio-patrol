"""GET /api/robot/connection-state — real-time connection-state for the UI."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from dependencies import get_fleet
from settings.config import get_runtime_settings

router = APIRouter(prefix="/api", tags=["RobotHealth"])

ROBOT_ID = "kachaka"


@router.get("/robot/connection-state")
async def connection_state(fleet=Depends(get_fleet)) -> dict:
    cfg = get_runtime_settings()
    debounce_seconds = int(cfg.get("robot_offline_debounce_seconds", 300))
    return fleet.get_connection_state_dict(
        ROBOT_ID, debounce_seconds=debounce_seconds
    )
