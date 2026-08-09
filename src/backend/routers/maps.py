"""Map management endpoints. PNG + JSON metadata persisted under data/maps/."""
import asyncio
import base64
import logging
import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from settings.config import MAPS_DIR, get_runtime_settings, update_settings
from utils.json_io import load_json, save_json

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Maps"])


def _safe_map_id(robot_map_id: str) -> str:
    """Local map id for a robot map id — it is the on-disk filename stem."""
    return robot_map_id.replace("/", "_").replace("\\", "_")


def _save_map_png_and_meta(map_pb, robot_map_id: str, locations: list, entry_name: str = "") -> dict | None:
    """Save a protobuf Map to data/maps/ as PNG + JSON. Returns metadata dict."""
    from google.protobuf.json_format import MessageToDict

    map_dict = MessageToDict(map_pb, preserving_proto_field_name=True)
    png_b64 = map_dict.get("data", "")
    png_bytes = base64.b64decode(png_b64) if png_b64 else b""
    if not png_bytes:
        return None

    safe_id = _safe_map_id(robot_map_id)
    os.makedirs(MAPS_DIR, exist_ok=True)
    with open(os.path.join(MAPS_DIR, f"{safe_id}.png"), "wb") as f:
        f.write(png_bytes)

    origin = map_dict.get("origin", {})
    meta = {
        "robot_map_id": robot_map_id,
        "name": entry_name or map_dict.get("name", "robot_map"),
        "resolution": map_dict.get("resolution", 0.05),
        "width": map_dict.get("width", 0),
        "height": map_dict.get("height", 0),
        "origin": {"x": origin.get("x", 0), "y": origin.get("y", 0)},
        "locations": locations,
        "timestamp": datetime.now().isoformat(),
    }
    save_json(os.path.join(MAPS_DIR, f"{safe_id}.json"), meta)
    return {**meta, "id": safe_id}


@router.get("/maps")
async def list_maps():
    """List all locally saved maps + the dashboard's active map."""
    cfg = get_runtime_settings()
    active_map = cfg.get("active_map", "")

    maps = []
    os.makedirs(MAPS_DIR, exist_ok=True)
    for fname in sorted(os.listdir(MAPS_DIR)):
        if not fname.endswith(".json"):
            continue
        meta = load_json(os.path.join(MAPS_DIR, fname), {})
        map_id = fname[:-5]  # strip .json
        maps.append({
            "id": map_id,
            "name": meta.get("name", map_id),
            "robot_map_id": meta.get("robot_map_id", ""),
            "timestamp": meta.get("timestamp", ""),
            "resolution": meta.get("resolution"),
            "width": meta.get("width"),
            "height": meta.get("height"),
        })
    return {"active_map": active_map, "maps": maps}


@router.post("/maps/fetch")
async def fetch_maps_from_robot():
    """Re-fetch all maps from the robot via get_map_list + load_map_preview."""
    from dependencies import get_fleet
    fleet = get_fleet()

    # Local map ids are derived from robot_map_id and survive a refetch, so
    # remember the active one and restore it below if the robot still has it.
    previous_active = get_runtime_settings().get("active_map", "")

    # 1. Map list + locations in parallel. Nothing local is touched until the
    #    robot has answered — an offline or rebooting robot used to leave the
    #    dashboard with zero maps and no active map.
    map_res, loc_res = await asyncio.gather(
        fleet.get_map_list("kachaka"),
        fleet.get_locations("kachaka"),
        return_exceptions=True,
    )
    if isinstance(map_res, Exception):
        raise HTTPException(status_code=502, detail=f"Failed to get map list: {map_res}")

    maps = map_res.get("maps", [])
    current_map_id = map_res.get("current_map_id", "")

    # 2. Clear stale local data so we don't surface maps deleted on the robot
    if os.path.isdir(MAPS_DIR):
        for fname in os.listdir(MAPS_DIR):
            fpath = os.path.join(MAPS_DIR, fname)
            if os.path.isfile(fpath):
                os.remove(fpath)

    if previous_active:
        update_settings(active_map="")

    if not maps:
        return {"status": "ok", "maps": [], "active_map": "", "message": "No maps on robot"}

    locations = []
    if not isinstance(loc_res, Exception) and loc_res.get("ok"):
        for loc in loc_res.get("locations", []):
            locations.append({
                "id": loc.get("id", ""),
                "name": loc.get("name", ""),
                "pose": loc.get("pose", {}),
            })

    # 3. Load all map previews in parallel and persist
    client = fleet.get_raw_client("kachaka")
    targets = [(e.get("id", ""), e.get("name", "")) for e in maps if e.get("id")]
    previews = await asyncio.gather(
        *(asyncio.to_thread(client.load_map_preview, rid) for rid, _ in targets),
        return_exceptions=True,
    )

    saved = []
    for (robot_map_id, entry_name), preview in zip(targets, previews):
        if isinstance(preview, Exception):
            logger.warning("load_map_preview error for %s", robot_map_id, exc_info=preview)
            continue
        # Locations apply only to the current map (others render without overlays)
        locs = locations if robot_map_id == current_map_id else []
        meta = _save_map_png_and_meta(preview, robot_map_id, locs, entry_name)
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

    # 4. Restore the previously active map if it came back in this fetch
    active_map = ""
    if previous_active and any(m["id"] == previous_active for m in saved):
        update_settings(active_map=previous_active)
        active_map = previous_active
    elif previous_active:
        if any(_safe_map_id(rid) == previous_active for rid, _ in targets):
            logger.warning(
                "Active map %s is still on the robot but its preview failed — cleared",
                previous_active,
            )
        else:
            logger.info("Previously active map %s no longer on robot — cleared", previous_active)

    return {
        "status": "ok",
        "current_robot_map": current_map_id,
        "maps": saved,
        "active_map": active_map,
    }


class SwitchMapRequest(BaseModel):
    map_id: str  # local map id (filename without extension)


@router.post("/maps/switch")
async def switch_map(req: SwitchMapRequest):
    """Switch the robot to a different map and set it as the dashboard's active map."""
    from dependencies import get_fleet

    meta = load_json(os.path.join(MAPS_DIR, f"{req.map_id}.json"), None)
    if not meta:
        raise HTTPException(status_code=404, detail=f"Map '{req.map_id}' not found")

    robot_map_id = meta.get("robot_map_id", "")
    if not robot_map_id:
        raise HTTPException(status_code=400, detail="Map has no robot_map_id — cannot switch")

    fleet = get_fleet()
    try:
        client = fleet.get_raw_client("kachaka")
        result = await asyncio.to_thread(client.switch_map, robot_map_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"switch_map failed: {e}")

    if not result.success:
        raise HTTPException(status_code=502, detail=f"switch_map error: code {result.error_code}")

    update_settings(active_map=req.map_id)
    return {"status": "ok", "active_map": req.map_id, "robot_map_id": robot_map_id}


class SetActiveMapRequest(BaseModel):
    map_id: str


@router.post("/maps/active")
async def set_active_map(req: SetActiveMapRequest):
    """Set active map for dashboard display only (no robot switch)."""
    if load_json(os.path.join(MAPS_DIR, f"{req.map_id}.json"), None) is None:
        raise HTTPException(status_code=404, detail=f"Map '{req.map_id}' not found")
    update_settings(active_map=req.map_id)
    return {"status": "ok", "active_map": req.map_id}


@router.get("/maps/active-info")
async def get_active_map_info():
    """Return the active map's metadata (or 404 if none set)."""
    cfg = get_runtime_settings()
    map_id = cfg.get("active_map", "")
    if not map_id:
        raise HTTPException(status_code=404, detail="No active map set")

    meta = load_json(os.path.join(MAPS_DIR, f"{map_id}.json"), None)
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
    """Serve a saved map PNG."""
    png_path = os.path.join(MAPS_DIR, f"{map_id}.png")
    try:
        with open(png_path, "rb") as f:
            return Response(content=f.read(), media_type="image/png")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Map image not found: {map_id}")
