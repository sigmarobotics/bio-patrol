"""GET /api/robot/connection-state — real-time connection-state for the UI."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from dependencies import get_fleet
from kachaka_core.connection import ConnectionState
from settings.config import get_runtime_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["RobotHealth"])

ROBOT_ID = "kachaka"


@router.get("/robot/connection-state")
async def connection_state(fleet=Depends(get_fleet)) -> dict:
    cfg = get_runtime_settings()
    debounce_seconds = int(cfg.get("robot_offline_debounce_seconds", 300))

    slot = fleet.get_slot_or_none(ROBOT_ID) if hasattr(fleet, "get_slot_or_none") else None
    if slot is None:
        return {
            "robot_id": ROBOT_ID,
            "state": "unregistered",
            "ip": "",
            "serial": "",
            "last_seen": None,
            "disconnected_at": None,
            "last_reconnect_at": None,
            "in_patrol": False,
            "debounce_seconds": debounce_seconds,
            "offline_pending": False,
        }

    real_state = "connected" if slot.conn.state == ConnectionState.CONNECTED else "disconnected"

    from services.task_runtime import current_tasks
    in_patrol = current_tasks.get(slot.robot_id) is not None

    offline_pending = (
        slot.debouncer.is_offline_pending() if slot.debouncer is not None else False
    )

    return {
        "robot_id": slot.robot_id,
        "state": real_state,
        "ip": slot.ip,
        "serial": slot.serial,
        "last_seen": slot.last_seen,
        "disconnected_at": slot.disconnected_at,
        "last_reconnect_at": slot.last_reconnect_at,
        "in_patrol": in_patrol,
        "debounce_seconds": debounce_seconds,
        "offline_pending": offline_pending,
    }
