"""FleetAPI — async bridge over kachaka_core for FastAPI.

Every public method is ``async`` and delegates to sync kachaka_core objects
via ``asyncio.to_thread()``, keeping the event loop unblocked.

Replaces the old FleetAPI that used ``kachaka_api.aio.KachakaApiClient``
directly.  All robot operations now flow through kachaka_core's
KachakaConnection (pooled), RobotController (command_id verified),
KachakaCommands (@with_retry), and KachakaQueries (@with_retry).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from kachaka_core import (
    KachakaCommands,
    KachakaConnection,
    KachakaQueries,
    RobotController,
)

logger = logging.getLogger(__name__)


class RobotNotRegistered(ValueError):
    """Raised by FleetAPI when a robot_id has no registered slot.

    Subclasses ValueError so existing ``except ValueError`` callers still match;
    main.py registers a targeted exception_handler for this subclass to map it
    to HTTP 404 without catching every other ValueError in the app.
    """


# ---------------------------------------------------------------------------
# Per-robot slot
# ---------------------------------------------------------------------------

@dataclass
class _RobotSlot:
    """Holds all kachaka_core objects and metadata for a single robot."""

    robot_id: str
    ip: str
    name: str
    conn: KachakaConnection
    ctrl: RobotController
    cmds: KachakaCommands
    queries: KachakaQueries
    status: str = "online"
    last_seen: float = field(default_factory=time.time)
    serial: str = ""


# ---------------------------------------------------------------------------
# FleetAPI
# ---------------------------------------------------------------------------

class FleetAPI:
    """Async bridge: FastAPI handlers -> sync kachaka_core objects."""

    def __init__(self) -> None:
        self._robots: Dict[str, _RobotSlot] = {}

    # ── helpers ───────────────────────────────────────────────────────

    def _get_slot(self, robot_id: str) -> _RobotSlot:
        slot = self._robots.get(robot_id)
        if slot is None:
            raise RobotNotRegistered(f"Robot {robot_id} not registered")
        return slot

    # ── registration ─────────────────────────────────────────────────

    async def register_robot(
        self, robot_id: str, ip: str, name: str = ""
    ) -> dict:
        """Create a pooled connection, ping, start the controller."""

        def _register() -> dict:
            conn = KachakaConnection.get(ip)
            ping = conn.ping()
            if not ping.get("ok"):
                return {"ok": False, "error": ping.get("error", "ping failed")}

            conn.ensure_resolver()

            ctrl = RobotController(conn)
            ctrl.start()

            cmds = KachakaCommands(conn)
            queries = KachakaQueries(conn)

            slot = _RobotSlot(
                robot_id=robot_id,
                ip=ip,
                name=name or robot_id,
                conn=conn,
                ctrl=ctrl,
                cmds=cmds,
                queries=queries,
                serial=ping.get("serial", ""),
            )
            self._robots[robot_id] = slot
            logger.info(
                "Registered robot %s (%s) serial=%s", robot_id, ip, slot.serial
            )
            return {"ok": True, "serial": slot.serial}

        return await asyncio.to_thread(_register)

    async def unregister_robot(self, robot_id: str) -> bool:
        """Stop the controller and remove the robot from the pool."""
        slot = self._robots.pop(robot_id, None)
        if slot is None:
            return False

        def _teardown() -> None:
            slot.ctrl.stop()
            KachakaConnection.remove(slot.ip)

        await asyncio.to_thread(_teardown)
        logger.info("Unregistered robot %s", robot_id)
        return True

    # ── status / metadata ────────────────────────────────────────────

    async def get_robot_status(self, robot_id: str) -> Optional[Dict]:
        """Return metadata dict for one robot, or None if not found."""
        slot = self._robots.get(robot_id)
        if slot is None:
            return None
        return {
            "id": slot.robot_id,
            "ip": slot.ip,
            "name": slot.name,
            "status": slot.status,
            "last_seen": slot.last_seen,
            "serial": slot.serial,
        }

    async def get_all_robots(self) -> Dict[str, Dict]:
        """Return metadata dicts for every registered robot."""
        return {
            rid: {
                "id": s.robot_id,
                "ip": s.ip,
                "name": s.name,
                "status": s.status,
                "last_seen": s.last_seen,
                "serial": s.serial,
            }
            for rid, s in self._robots.items()
        }

    async def update_robot_status(self, robot_id: str, status: str) -> bool:
        """Update the logical status string for a robot."""
        slot = self._robots.get(robot_id)
        if slot is None:
            return False
        slot.status = status
        slot.last_seen = time.time()
        return True

    # ── controller state / metrics ───────────────────────────────────

    async def get_controller_state(self, robot_id: str) -> dict:
        """Thread-safe snapshot from RobotController."""
        slot = self._get_slot(robot_id)

        def _read() -> dict:
            st = slot.ctrl.state
            return {
                "battery_pct": st.battery_pct,
                "pose_x": st.pose_x,
                "pose_y": st.pose_y,
                "pose_theta": st.pose_theta,
                "is_command_running": st.is_command_running,
                "last_updated": st.last_updated,
            }

        return await asyncio.to_thread(_read)

    async def get_metrics(self, robot_id: str) -> dict:
        """Return ControllerMetrics as a dict."""
        slot = self._get_slot(robot_id)
        m = slot.ctrl.metrics
        return {
            "poll_count": m.poll_count,
            "poll_success_count": m.poll_success_count,
            "poll_failure_count": m.poll_failure_count,
            "poll_rtt_list": list(m.poll_rtt_list),
        }

    async def reset_metrics(self, robot_id: str) -> None:
        """Clear metrics on the controller."""
        slot = self._get_slot(robot_id)
        slot.ctrl.reset_metrics()

    # ── movement commands (RobotController — command_id verified) ────

    async def move_to_location(
        self,
        robot_id: str,
        location_name: str,
        *,
        timeout: float = 120.0,
        cancel_all: bool = True,
        tts_on_success: str = "",
        title: str = "",
    ) -> dict:
        """Move robot to a named location (blocking, with command_id tracking)."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(
            slot.ctrl.move_to_location,
            location_name,
            timeout=timeout,
            cancel_all=cancel_all,
            tts_on_success=tts_on_success,
            title=title,
        )

    async def move_shelf(
        self,
        robot_id: str,
        shelf_name: str,
        location_name: str,
        *,
        timeout: float = 120.0,
        cancel_all: bool = True,
        tts_on_success: str = "",
        title: str = "",
    ) -> dict:
        """Pick up shelf and deliver to location (command_id verified)."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(
            slot.ctrl.move_shelf,
            shelf_name,
            location_name,
            timeout=timeout,
            cancel_all=cancel_all,
            tts_on_success=tts_on_success,
            title=title,
        )

    async def return_shelf(
        self,
        robot_id: str,
        shelf_name: str = "",
        *,
        timeout: float = 60.0,
        cancel_all: bool = True,
        tts_on_success: str = "",
        title: str = "",
    ) -> dict:
        """Return shelf to its home location (command_id verified)."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(
            slot.ctrl.return_shelf,
            shelf_name,
            timeout=timeout,
            cancel_all=cancel_all,
            tts_on_success=tts_on_success,
            title=title,
        )

    async def return_home(
        self,
        robot_id: str,
        *,
        timeout: float = 60.0,
        cancel_all: bool = True,
        tts_on_success: str = "",
        title: str = "",
    ) -> dict:
        """Return robot to charger (command_id verified)."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(
            slot.ctrl.return_home,
            timeout=timeout,
            cancel_all=cancel_all,
            tts_on_success=tts_on_success,
            title=title,
        )

    # ── simple commands (KachakaCommands — @with_retry) ──────────────

    async def speak(self, robot_id: str, text: str, **kwargs: Any) -> dict:
        """Text-to-speech on robot speaker."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.cmds.speak, text, **kwargs)

    async def dock_shelf(self, robot_id: str, **kwargs: Any) -> dict:
        """Dock the currently held shelf."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.cmds.dock_shelf, **kwargs)

    async def undock_shelf(self, robot_id: str, **kwargs: Any) -> dict:
        """Undock the currently held shelf."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.cmds.undock_shelf, **kwargs)

    async def move_to_pose(
        self, robot_id: str, x: float, y: float, yaw: float, **kwargs: Any
    ) -> dict:
        """Move to absolute map coordinate (x, y, yaw)."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(
            slot.cmds.move_to_pose, x, y, yaw, **kwargs
        )

    async def cancel_command(self, robot_id: str) -> dict:
        """Cancel the currently running command."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.cmds.cancel_command)

    # ── queries (KachakaQueries — @with_retry) ───────────────────────

    async def get_pose(self, robot_id: str) -> dict:
        """Current robot pose on the map."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.get_pose)

    async def get_battery_info(self, robot_id: str) -> dict:
        """Battery percentage and charging status."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.get_battery)

    async def get_locations(self, robot_id: str) -> dict:
        """All registered locations."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.list_locations)

    async def get_shelves(self, robot_id: str) -> dict:
        """All registered shelves."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.list_shelves)

    async def get_moving_shelf(self, robot_id: str) -> dict:
        """ID of the shelf the robot is currently carrying."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.get_moving_shelf)

    async def get_command_state(self, robot_id: str) -> dict:
        """Current command execution state."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.get_command_state)

    async def get_last_command_result(self, robot_id: str) -> dict:
        """Result of the most recently completed command."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.get_last_command_result)

    async def get_errors(self, robot_id: str) -> dict:
        """Current active errors on the robot."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.get_errors)

    async def get_status(self, robot_id: str) -> dict:
        """Full snapshot: pose, battery, command state, errors."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.get_status)

    async def get_serial_number(self, robot_id: str) -> dict:
        """Robot serial number."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.get_serial_number)

    async def get_map(self, robot_id: str) -> dict:
        """Current map as base64-encoded PNG."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.get_map)

    async def get_map_list(self, robot_id: str) -> dict:
        """All available maps."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.list_maps)

    async def get_speaker_volume(self, robot_id: str) -> dict:
        """Current speaker volume (0-10)."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.queries.get_speaker_volume)

    # ── raw SDK access ───────────────────────────────────────────────

    def get_raw_client(self, robot_id: str):
        """Return the underlying KachakaApiClient for ROS/advanced endpoints.

        This is synchronous — callers are responsible for wrapping in
        ``asyncio.to_thread()`` if needed.
        """
        slot = self._get_slot(robot_id)
        return slot.conn.client
