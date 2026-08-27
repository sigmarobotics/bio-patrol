"""FleetAPI — async bridge over kachaka_core for FastAPI.

Every public method is ``async`` and delegates to sync kachaka_core objects
via ``asyncio.to_thread()``, keeping the event loop unblocked.

IT-10 changes:
- ``_RobotSlot`` owns the ``on_state_change`` callback (registered before
  ``RobotController.start``, since ``start_monitoring`` is idempotent).
- ``_on_state_change`` mirrors state to ``slot.status``, delegates to the
  controller, and forwards to ``OfflineDebouncer`` via
  ``run_coroutine_threadsafe``.
- ``_on_shelf_dropped`` sets a slot-owned ``asyncio.Event`` consumed by
  TaskEngine.
- ``unregister_robot`` cancels any in-flight register-retry task and shuts
  the debouncer down.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Dict, Optional

from kachaka_core import (
    KachakaCommands,
    KachakaConnection,
    KachakaQueries,
    RobotController,
)
from kachaka_core.connection import ConnectionState

import lifespan_state
from services.notifications import dispatcher
from services.notifications.offline_debouncer import OfflineDebouncer

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
    loop: asyncio.AbstractEventLoop
    status: str = "online"
    last_seen: float = field(default_factory=time.time)
    serial: str = ""
    status_lock: threading.Lock = field(default_factory=threading.Lock)
    debouncer: Optional[OfflineDebouncer] = None
    shelf_drop_event: Optional[asyncio.Event] = None
    last_dropped_shelf_id: Optional[str] = None
    disconnected_at: Optional[float] = None
    last_reconnect_at: Optional[float] = None


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

    def get_slot_or_none(self, robot_id: str) -> Optional[_RobotSlot]:
        """Return the slot for ``robot_id`` or ``None`` if not registered.

        Used by callers that need to read slot state without raising
        (e.g. connection-state endpoint).
        """
        return self._robots.get(robot_id)

    # ── registration ─────────────────────────────────────────────────

    async def register_robot(
        self, robot_id: str, ip: str, name: str = ""
    ) -> dict:
        """Create a pooled connection, ping, build slot, then start monitoring.

        Two-phase to avoid a slot/monitor race: build the slot and insert it
        into ``self._robots`` BEFORE calling ``start_monitoring`` so the very
        first state-change callback can find the slot. If Phase 4 raises,
        the slot is popped and the debouncer is drained.
        """
        loop = asyncio.get_running_loop()

        # Phase 1 — sync ping. Returns (conn, ping) on success, None on fail.
        def _build_sync():
            conn = KachakaConnection.get(ip)
            ping = conn.ping()
            if not ping.get("ok"):
                # Tear down so the auto-started monitor thread doesn't keep
                # pinging a bad IP (remove() stops it since toolkit 0.6.1).
                KachakaConnection.remove(ip)
                return None, {"ok": False, "error": ping.get("error", "ping failed")}
            return (conn, ping), {"ok": True}

        built, build_result = await asyncio.to_thread(_build_sync)
        if built is None:
            return build_result
        conn, ping = built

        # Phase 2 — define callbacks. They close over a slot assigned in
        # Phase 3 before Phase 4 ever starts the monitor thread.
        slot: _RobotSlot

        def _on_state_change(state: ConnectionState) -> None:
            with slot.status_lock:
                slot.status = (
                    "online" if state == ConnectionState.CONNECTED else "offline"
                )
                slot.last_seen = time.time()
                if state == ConnectionState.DISCONNECTED:
                    slot.disconnected_at = time.time()
                else:
                    slot.last_reconnect_at = time.time()
            try:
                slot.ctrl._on_conn_state_change(state)
            except Exception:
                logger.exception("controller delegate failed")
            if slot.debouncer is not None:
                fn = (
                    slot.debouncer.note_disconnected
                    if state == ConnectionState.DISCONNECTED
                    else slot.debouncer.note_connected
                )
                slot.loop.call_soon_threadsafe(fn)
            if state == ConnectionState.CONNECTED:
                # Reconnect is the authoritative end of the "robot offline"
                # fact — sweep the disconnect-type alerts here, or an idle
                # reconnect leaves 機器人失聯 on the dashboard until the next
                # run's opening reset step (hours, for a robot that came back
                # overnight). Scheduled on the loop: tasks_db is loop-owned.
                def _sweep_offline_alerts() -> None:
                    from services.task_runtime import clear_shelf_dropped_tasks
                    clear_shelf_dropped_tasks(
                        only_disconnect=True, reason="robot reconnected"
                    )
                slot.loop.call_soon_threadsafe(_sweep_offline_alerts)

        def _on_shelf_dropped(shelf_id: str) -> None:
            slot.last_dropped_shelf_id = shelf_id
            if slot.shelf_drop_event is not None:
                slot.loop.call_soon_threadsafe(slot.shelf_drop_event.set)

        # Phase 3 — construct slot, build debouncer, INSERT into pool.
        ctrl = RobotController(conn, on_shelf_dropped=_on_shelf_dropped)
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
            loop=loop,
            serial=ping.get("serial", ""),
        )
        slot.shelf_drop_event = asyncio.Event()

        def _patrol_running() -> bool:
            from services.task_runtime import current_tasks
            return current_tasks.get(robot_id) is not None

        def _serial() -> str:
            return slot.serial

        def _debounce_seconds() -> float:
            from settings.config import get_runtime_settings
            return float(
                get_runtime_settings().get("robot_offline_debounce_seconds", 300)
            )

        slot.debouncer = OfflineDebouncer(
            robot_id=robot_id,
            debounce_seconds_provider=_debounce_seconds,
            is_patrol_running=_patrol_running,
            emit=dispatcher.dispatch,
            get_serial=_serial,
        )
        self._robots[robot_id] = slot

        # Phase 4 — start controller first, then register the fleet callback.
        # Since toolkit 0.6.0 start_monitoring() updates the single callback
        # slot in place, so whoever registers LAST wins it. ctrl.start()
        # installs the controller's own callback; the fleet callback must
        # come after (it already delegates to the controller). interval=1.0
        # matches the controller's _fast_interval so the loop isn't restarted.
        def _start_sync() -> None:
            ctrl.start()
            conn.start_monitoring(interval=1.0, on_state_change=_on_state_change)
            conn.ensure_resolver()

        try:
            await asyncio.to_thread(_start_sync)
        except Exception as exc:
            self._robots.pop(robot_id, None)
            try:
                await slot.debouncer.shutdown()
            except Exception:
                logger.exception("debouncer shutdown after Phase 4 failure")
            try:
                await asyncio.to_thread(conn.stop_monitoring)
            except Exception:
                pass
            logger.exception("register_robot Phase 4 raised: %s", exc)
            return {"ok": False, "error": f"start failed: {exc}"}

        logger.info(
            "Registered robot %s (%s) serial=%s", robot_id, ip, slot.serial
        )
        return {"ok": True, "serial": slot.serial}

    async def unregister_robot(self, robot_id: str) -> bool:
        """Stop the controller, drain the debouncer, remove from the pool."""
        await lifespan_state.cancel_register_retry(robot_id)

        slot = self._robots.pop(robot_id, None)
        if slot is None:
            return False

        if slot.debouncer is not None:
            await slot.debouncer.shutdown()

        def _teardown() -> None:
            try:
                slot.ctrl.stop()
            finally:
                try:
                    slot.conn.stop_monitoring()
                except Exception:
                    pass
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

    def get_connection_state_dict(
        self, robot_id: str, *, debounce_seconds: int
    ) -> Dict[str, Any]:
        """Snapshot for GET /api/robot/connection-state — slot-aware payload.

        Lives on FleetAPI so the router doesn't reach across the slot →
        conn → state / slot → debouncer → method abstraction layers.
        """
        from services.task_runtime import current_tasks

        slot = self._robots.get(robot_id)
        if slot is None:
            return {
                "robot_id": robot_id,
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
        return {
            "robot_id": slot.robot_id,
            "state": (
                "connected"
                if slot.conn.state == ConnectionState.CONNECTED
                else "disconnected"
            ),
            "ip": slot.ip,
            "serial": slot.serial,
            "last_seen": slot.last_seen,
            "disconnected_at": slot.disconnected_at,
            "last_reconnect_at": slot.last_reconnect_at,
            "in_patrol": current_tasks.get(slot.robot_id) is not None,
            "debounce_seconds": debounce_seconds,
            "offline_pending": (
                slot.debouncer.is_offline_pending()
                if slot.debouncer is not None
                else False
            ),
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
        with slot.status_lock:
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
        timeout: float = 240.0,
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
        timeout: float = 240.0,
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
        timeout: float = 240.0,
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
        timeout: float = 240.0,
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
        """Dock the currently held shelf (blocks until completion)."""
        slot = self._get_slot(robot_id)

        def _run() -> dict:
            # KachakaCommands is fire-and-accept since toolkit 0.6.0 —
            # poll so the result reflects completion, not acceptance.
            accepted = slot.cmds.dock_shelf(**kwargs)
            if not accepted.get("ok"):
                return accepted
            # Toolkit >=0.7: poll_until_complete verifies completion against
            # the command_id tracked by this cmds instance — the registration
            # window can no longer be misread as completion. The sleep stays
            # as belt-and-braces for 0.6.x (observed on hardware there:
            # elapsed=0.0 echoing the prior move_to_pose).
            time.sleep(0.5)
            return slot.cmds.poll_until_complete(timeout=60.0)

        return await asyncio.to_thread(_run)

    async def undock_shelf(self, robot_id: str, **kwargs: Any) -> dict:
        """Undock the currently held shelf (blocks until completion)."""
        slot = self._get_slot(robot_id)

        def _run() -> dict:
            accepted = slot.cmds.undock_shelf(**kwargs)
            if not accepted.get("ok"):
                return accepted
            time.sleep(0.5)  # let the command register before polling
            return slot.cmds.poll_until_complete(timeout=60.0)

        return await asyncio.to_thread(_run)

    async def reset_shelf_pose(self, robot_id: str, shelf_id: str) -> dict:
        """Reset the robot's recorded shelf pose to the shelf's home."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(slot.cmds.reset_shelf_pose, shelf_id)

    async def move_to_pose(
        self,
        robot_id: str,
        x: float,
        y: float,
        yaw: float,
        *,
        timeout: float = 120.0,
        cancel_all: bool = True,
        tts_on_success: str = "",
        title: str = "",
    ) -> dict:
        """Move to absolute map coordinate (blocking, command_id verified)."""
        slot = self._get_slot(robot_id)
        return await asyncio.to_thread(
            slot.ctrl.move_to_pose,
            x,
            y,
            yaw,
            timeout=timeout,
            cancel_all=cancel_all,
            tts_on_success=tts_on_success,
            title=title,
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
