from fastapi import APIRouter, Response, HTTPException, Depends, File, UploadFile
from pathlib import Path
from typing import Optional
from pydantic import BaseModel
from google.protobuf.json_format import MessageToJson
from services.fleet_api import FleetAPI
from dependencies import get_fleet

import asyncio
import json
import os
import re
import tempfile

router = APIRouter(prefix='/kachaka', tags=['KACHAKA Robot Fleet'])

# ---------------------------------------------------------------------------
# Request models for command endpoints
# ---------------------------------------------------------------------------

class SpeakRequest(BaseModel):
    text: str

class MoveToLocationRequest(BaseModel):
    location_id: str

class MoveToPoseRequest(BaseModel):
    x: float
    y: float
    yaw: float

class MoveShelfRequest(BaseModel):
    shelf_id: str
    location_id: str

class ReturnShelfRequest(BaseModel):
    shelf_id: str

class ResetShelfPoseRequest(BaseModel):
    shelf_id: str

# Unknown robot_id -> RobotNotRegistered -> global handler in main.py returns 404.

# ====== Fleet Management APIs ======

@router.get("/robots")
async def get_all_robots(fleet: FleetAPI = Depends(get_fleet)):
    """Get all registered robots"""
    return await fleet.get_all_robots()

@router.post("/robots/register")
async def register_robot(robot_id: str, url: str, name: Optional[str] = None, fleet: FleetAPI = Depends(get_fleet)):
    """Register a new robot instance"""
    result = await fleet.register_robot(robot_id, url, name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=f"Failed to register robot: {result.get('error', 'unknown')}")
    return {"message": "Robot registered successfully"}

@router.get("/robots/{robot_id}")
async def get_robot_status(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot status"""
    status = await fleet.get_robot_status(robot_id)
    if not status:
        raise HTTPException(status_code=404, detail="Robot not found")
    return status

@router.put("/robots/{robot_id}/status")
async def update_robot_status(robot_id: str, status: str, fleet: FleetAPI = Depends(get_fleet)):
    """Update robot status"""
    result = await fleet.update_robot_status(robot_id, status)
    if not result:
        raise HTTPException(status_code=404, detail="Robot not found")
    return {"message": "Robot status updated successfully"}

@router.delete("/robots/{robot_id}")
async def unregister_robot(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Unregister an existing robot instance"""
    result = await fleet.unregister_robot(robot_id)
    if not result:
        raise HTTPException(status_code=404, detail="Robot not found")
    return {"message": "Robot unregistered successfully"}

# ====== Robot Info APIs ======

@router.get("/{robot_id}/serial_number")
async def serial_number(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot serial number"""
    return await fleet.get_serial_number(robot_id)

@router.get("/{robot_id}/version")
async def version(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot version"""
    client = fleet.get_raw_client(robot_id)
    return await asyncio.to_thread(client.get_robot_version)

@router.get("/{robot_id}/pose")
async def robot_pose(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot pose"""
    return await fleet.get_pose(robot_id)

@router.get("/{robot_id}/battery")
async def battery(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot battery info"""
    return await fleet.get_battery_info(robot_id)

@router.get("/{robot_id}/error/json")
async def error_code_in_json(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot error code in JSON format"""
    client = fleet.get_raw_client(robot_id)
    res = await asyncio.to_thread(client.get_robot_error_code)
    return Response(content=json.dumps(res), media_type='application/json')

@router.get("/{robot_id}/error")
async def error(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot error info"""
    return await fleet.get_errors(robot_id)

@router.get("/{robot_id}/map")
async def png_map(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot map"""
    return await fleet.get_map(robot_id)

@router.get("/{robot_id}/map_list")
async def map_list(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot map list"""
    return await fleet.get_map_list(robot_id)

@router.get("/{robot_id}/export_map")
async def export_map(robot_id: str, map_id: Optional[str] = None, fleet: FleetAPI = Depends(get_fleet)):
    """Export a robot map archive (defaults to the current map).

    The SDK signature is ``export_map(map_id, output_file_path) -> pb2.Result``:
    it streams the archive into a file and returns only a status. Calling it
    bare (the pre-fix code) raised TypeError -> HTTP 500 (新營 2026-07-30).
    """
    client = fleet.get_raw_client(robot_id)

    def _export():
        target = map_id or client.get_current_map_id()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "map.bin")
            result = client.export_map(target, path)
            # The SDK writes the file only on success.
            data = Path(path).read_bytes() if result.success else b""
        return result, target, data

    result, target, data = await asyncio.to_thread(_export)
    if not result.success:
        raise HTTPException(status_code=502, detail=f"export_map failed: error {result.error_code}")
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", target or "map") + ".kmap"
    return Response(
        content=data,
        media_type='application/octet-stream',
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@router.post("/{robot_id}/import_map")
async def import_map(robot_id: str, file: UploadFile = File(...), fleet: FleetAPI = Depends(get_fleet)):
    """Import a map archive uploaded as multipart form data.

    The SDK signature is ``import_map(target_file_path, chunk_size)`` and it
    returns a ``(pb2.Result, map_id)`` tuple — not a protobuf message, so the
    pre-fix ``MessageToJson(res)`` could only raise.
    """
    client = fleet.get_raw_client(robot_id)
    payload = await file.read()

    def _import():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "map.bin")
            with open(path, "wb") as fh:
                fh.write(payload)
            return client.import_map(path)

    result, new_map_id = await asyncio.to_thread(_import)
    if not result.success:
        raise HTTPException(status_code=502, detail=f"import_map failed: error {result.error_code}")
    return {"ok": True, "map_id": new_map_id}

# ====== ROS-level endpoints (raw SDK client) ======

@router.get("/{robot_id}/imu")
async def ros_imu_info(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot IMU info"""
    client = fleet.get_raw_client(robot_id)
    res = await asyncio.to_thread(client.get_ros_imu)
    return Response(content=MessageToJson(res), media_type='application/json')

@router.get("/{robot_id}/odometry")
async def ros_odometry(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot odometry info"""
    client = fleet.get_raw_client(robot_id)
    res = await asyncio.to_thread(client.get_ros_odometry)
    return Response(content=MessageToJson(res), media_type='application/json')

@router.get("/{robot_id}/wheel/odometry")
async def ros_wheel_odometry(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot wheel odometry info"""
    client = fleet.get_raw_client(robot_id)
    res = await asyncio.to_thread(client.get_ros_wheel_odometry)
    return Response(content=MessageToJson(res), media_type='application/json')

@router.get("/{robot_id}/laser/scan")
async def ros_laser_scan(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot laser scan info"""
    client = fleet.get_raw_client(robot_id)
    res = await asyncio.to_thread(client.get_ros_laser_scan)
    return Response(content=MessageToJson(res), media_type='application/json')

# ====== Query APIs (kachaka_core — returns dicts) ======

@router.get("/{robot_id}/locations")
async def locations(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot locations"""
    return await fleet.get_locations(robot_id)

@router.get("/{robot_id}/shelves")
async def shelves(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get robot shelves"""
    return await fleet.get_shelves(robot_id)

@router.get("/{robot_id}/shelves/moving")
async def moving_shelf(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get moving shelf ID"""
    return await fleet.get_moving_shelf(robot_id)

# ====== Robot Command APIs ======

@router.post("/{robot_id}/command/speak")
async def speak(robot_id: str, req: SpeakRequest, fleet: FleetAPI = Depends(get_fleet)):
    """Send speak command to robot"""
    return await fleet.speak(robot_id, req.text)

@router.post("/{robot_id}/command/move_to_location")
async def move_to_location(robot_id: str, req: MoveToLocationRequest, fleet: FleetAPI = Depends(get_fleet)):
    """Move robot to specified location"""
    return await fleet.move_to_location(robot_id, req.location_id)

@router.post("/{robot_id}/command/move_to_pose")
async def move_to_pose(robot_id: str, req: MoveToPoseRequest, fleet: FleetAPI = Depends(get_fleet)):
    """Move robot to specified pose"""
    return await fleet.move_to_pose(robot_id, req.x, req.y, req.yaw)

@router.post("/{robot_id}/command/dock_shelf")
async def dock_shelf(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Dock robot to shelf"""
    return await fleet.dock_shelf(robot_id)

@router.post("/{robot_id}/command/undock_shelf")
async def undock_shelf(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Undock robot from shelf"""
    return await fleet.undock_shelf(robot_id)

@router.post("/{robot_id}/command/move_shelf")
async def move_shelf(robot_id: str, req: MoveShelfRequest, fleet: FleetAPI = Depends(get_fleet)):
    """Move shelf to specified location"""
    return await fleet.move_shelf(robot_id, req.shelf_id, req.location_id)

@router.post("/{robot_id}/command/return_shelf")
async def return_shelf(robot_id: str, req: ReturnShelfRequest, fleet: FleetAPI = Depends(get_fleet)):
    """Return shelf to its original position"""
    return await fleet.return_shelf(robot_id, req.shelf_id)

@router.post("/{robot_id}/command/reset_shelf_pose")
async def reset_shelf_pose(robot_id: str, req: ResetShelfPoseRequest, fleet: FleetAPI = Depends(get_fleet)):
    """Reset shelf pose"""
    client = fleet.get_raw_client(robot_id)
    return await asyncio.to_thread(client.reset_shelf_pose, req.shelf_id)

@router.post("/{robot_id}/command/return_home")
async def return_home(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Return robot to home position"""
    return await fleet.return_home(robot_id)

# ====== Command State APIs ======

@router.get("/{robot_id}/command/state")
async def command_state(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get command state"""
    return await fleet.get_command_state(robot_id)

@router.get("/{robot_id}/command/last")
async def last_command_result(robot_id: str, fleet: FleetAPI = Depends(get_fleet)):
    """Get last command result"""
    return await fleet.get_last_command_result(robot_id)
