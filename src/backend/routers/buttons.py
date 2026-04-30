"""REST API for Zigbee button bindings.

Action-centric UX — the Settings UI shows one row per registered action; per
row the user can Pair / Cancel / Unpair / Test. There is no separate "buttons"
list because each action holds at most one IEEE.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services import action_registry, button_db

logger = logging.getLogger("routers.buttons")

router = APIRouter(prefix="/api", tags=["Button Bindings"])


_button_manager = None


def set_manager(manager) -> None:
    global _button_manager
    _button_manager = manager


def _ensure_manager():
    if _button_manager is None:
        raise HTTPException(
            status_code=503,
            detail="Button manager not initialized (zigbee MQTT disabled?)",
        )
    return _button_manager


class TestParams(BaseModel):
    params: dict | None = None


def _online_state(row: dict) -> str:
    """unpaired | online | offline.

    A bound device with last_left_at set (and not cleared by a subsequent
    update_status) is considered offline — the SNZB-01P firmware sleeps after
    each press session and announces a leave; the next press wakes it and
    flips it back to online.
    """
    if not row.get("ieee_addr"):
        return "unpaired"
    return "offline" if row.get("last_left_at") else "online"


@router.get("/button-bindings")
async def list_bindings():
    rows = {b["action_key"]: b for b in button_db.list_bindings()}
    actions = []
    for action in action_registry.list_actions():
        row = rows.get(action["key"], {})
        actions.append({
            **action,
            "ieee_addr": row.get("ieee_addr"),
            "friendly_name": row.get("friendly_name"),
            "paired_at": row.get("paired_at"),
            "battery": row.get("battery"),
            "last_seen": row.get("last_seen"),
            "last_left_at": row.get("last_left_at"),
            "last_fired_at": row.get("last_fired_at"),
            "fire_count": row.get("fire_count", 0),
            "online_state": _online_state(row),
        })
    mgr = _button_manager
    pair_status = {
        "armed_action": mgr.pairing_target if mgr else None,
        "armed_remaining_s": mgr.pair_remaining_seconds if mgr else None,
        "mqtt_connected": bool(mgr and mgr.zigbee.connected),
    }
    return {"actions": actions, "pair_status": pair_status}


@router.post("/button-bindings/{action_key}/pair")
async def start_pair(action_key: str):
    mgr = _ensure_manager()
    if not action_registry.is_registered(action_key):
        raise HTTPException(status_code=404, detail=f"Unknown action: {action_key}")
    result = await mgr.arm_pair(action_key)
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("error", "pair failed"))
    return result


@router.post("/button-bindings/{action_key}/pair/cancel")
async def cancel_pair(action_key: str):
    mgr = _ensure_manager()
    return await mgr.cancel_pair()


@router.delete("/button-bindings/{action_key}")
async def unpair(action_key: str, forget_device: bool = False):
    if not action_registry.is_registered(action_key):
        raise HTTPException(status_code=404, detail=f"Unknown action: {action_key}")
    prev_ieee = button_db.unbind_action(action_key)
    if forget_device and prev_ieee and _button_manager is not None:
        await _button_manager.forget_device(prev_ieee)
    return {"ok": True, "previous_ieee": prev_ieee, "forget_device": forget_device}


@router.post("/button-bindings/{action_key}/test")
async def test_action(action_key: str, body: TestParams | None = None):
    if not action_registry.is_registered(action_key):
        raise HTTPException(status_code=404, detail=f"Unknown action: {action_key}")
    params = body.params if body else None
    result = await action_registry.fire(action_key, params)
    return result
