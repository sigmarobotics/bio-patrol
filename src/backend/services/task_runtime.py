import asyncio
import logging
import sqlite3
from typing import Any, Dict, List, Optional
from datetime import datetime
from services.fleet_api import FleetAPI
from services.notifications import AnomalyEvent, Severity, Source, dispatcher
from services.notifications.evaluator import BioScanFailureEvaluator
from common_types import (
    Task, TaskStep, TaskStatus, StepStatus, StepResult, StepAction,
    NON_CRITICAL_ACTIONS, get_now,
)
from dependencies import get_bio_sensor_client
from utils.grpc_errors import is_connection_error
from utils.sqlite_wal import connect_db

logger = logging.getLogger("kachaka.task_runtime")

_bio_scan_evaluator = BioScanFailureEvaluator()

# Kachaka puts its shelf down and drives to the charger on its own when the
# battery runs out — the moving-shelf id then disappears exactly like a drop
# and the run gets a CRITICAL "shelf dropped" alarm nobody can act on. Below
# this level a release backed by a charging / return-home signal is read as
# that auto-recharge instead.
LOW_BATTERY_RELEASE_PCT = 20.0

# kachaka_core.get_battery returns power_status as ``str(pb2.PowerSupplyStatus)``
# — a numeric string on firmware 3.16 ("1" CHARGING, "4" FULL). The names are
# accepted too so a toolkit that starts returning them keeps working.
_CHARGING_POWER_STATUSES = {
    "1", "4", "CHARGING", "FULL",
    "POWER_SUPPLY_STATUS_CHARGING", "POWER_SUPPLY_STATUS_FULL",
}

# A robot that stopped answering is not a robot that dropped its shelf
# (2026-08-10 新營: the robot left the network mid-patrol and the failed reads
# became a CRITICAL drop alarm). The state watcher must see this many
# consecutive failed polls before calling the robot gone — a single transient
# gRPC hiccup is normal and stays silent.
DISCONNECT_POLL_THRESHOLD = 3


def classify_shelf_release(battery: dict, command_state: dict,
                           *, disconnected: bool = False) -> str:
    """Why the moving-shelf id vanished: ``disconnected`` /
    ``low_battery_return`` / ``drop``.

    ``battery`` is a kachaka_core ``get_battery`` dict
    ({ok, percentage, power_status}); ``command_state`` a ``get_command_state``
    dict ({ok, state, command, is_running}). ``disconnected`` says the robot
    itself is unreachable — then nothing was actually observed, and claiming
    either "dropped" or "did not drop" would be a guess, so the release is
    reported as unknown. A robot that answers keeps the old rules: anything
    unreadable stays a drop, because a missed drop leaves a shelf loose in a
    hospital corridor.
    """
    if disconnected:
        return "disconnected"
    if not battery.get("ok"):
        return "drop"
    pct = battery.get("percentage")
    if not isinstance(pct, (int, float)) or pct > LOW_BATTERY_RELEASE_PCT:
        return "drop"
    if str(battery.get("power_status", "")).upper() in _CHARGING_POWER_STATUSES:
        return "low_battery_return"
    if "return_home_command" in str((command_state or {}).get("command") or ""):
        return "low_battery_return"
    return "drop"

# --- global states ---
tasks_db: Dict[str, Task] = {}
engines: Dict[str, "TaskEngine"] = {}
task_queues: Dict[str, asyncio.Queue] = {}
current_tasks: Dict[str, str] = {}  # robot_id -> task_id


def clear_shelf_dropped_tasks(*, only_disconnect: bool = False,
                              reason: str = "") -> int:
    """Mark SHELF_DROPPED tasks DONE; returns how many were cleared.

    One robot, one shelf: the manual recover-shelf action means a human has
    just re-established the physical situation, so every standing drop alert
    is stale — clear them all. With ``only_disconnect=True`` only
    offline-type alerts (metadata.disconnect) are swept: the same "robot is
    offline" fact never needs N alerts, while a real CRITICAL drop is only
    cleared by an explicit recovery action — never by an automatic sweep.
    """
    cleared = 0
    for task in tasks_db.values():
        if task.status != TaskStatus.SHELF_DROPPED:
            continue
        if only_disconnect and (task.metadata or {}).get("disconnect") is not True:
            continue
        task.status = TaskStatus.DONE
        cleared += 1
    if cleared:
        logger.info(f"Cleared {cleared} shelf_dropped task(s) ({reason})")
    return cleared


async def submit_task(task: Task):
    """Submit a task for execution. Routes directly to the robot's queue."""
    robot_id = task.robot_id or "kachaka"
    task.robot_id = robot_id
    if robot_id not in task_queues:
        logger.error(f"Robot '{robot_id}' not registered. Failing task {task.task_id}.")
        task.status = TaskStatus.FAILED
        tasks_db[task.task_id] = task
        return
    task.status = TaskStatus.QUEUED
    tasks_db[task.task_id] = task
    await task_queues[robot_id].put(task)
    logger.info(f"Task {task.task_id} submitted to robot {robot_id}")


async def submit_cleanup_task(robot_id: str) -> Optional[str]:
    """取消載棚車任務後的收工:身上還有棚車就先歸位,再回充。

    操作者按「停止」的語意是「收工」,不是「原地棄置」。持棚車與否問機器人
    本人(get_moving_shelf)——引擎的 _current_shelf_id 只在 move_shelf 完整
    跑完後才設,第一段移動途中取消會漏抓(2026-08-18 板橋現場)。
    """
    from dependencies import get_fleet
    from common_types import generate_task_id
    shelf_id = None
    try:
        res = await get_fleet().get_moving_shelf(robot_id)
        shelf_id = res.get("shelf_id")
    except Exception as e:
        logger.warning(f"Cleanup: moving-shelf query failed, assuming none held: {e}")
    steps = []
    if shelf_id:
        steps.append(TaskStep(
            step_id="cleanup_return_shelf", action=StepAction.RETURN_SHELF.value,
            params={"shelf_id": shelf_id}, status=StepStatus.PENDING))
    steps.append(TaskStep(
        step_id="cleanup_return_home", action=StepAction.RETURN_HOME.value,
        params={}, status=StepStatus.PENDING))
    task = Task(task_id=generate_task_id(), robot_id=robot_id, steps=steps,
                status=TaskStatus.QUEUED, metadata={"mode": "cleanup"})
    await submit_task(task)
    logger.info(f"Cleanup task {task.task_id} queued (shelf={shelf_id or 'none'})")
    return task.task_id


class TaskEngine:
    def __init__(self, fleet_api: FleetAPI, robot_id: str):
        self.fleet = fleet_api
        self.robot_id = robot_id
        self._shelf_names: Dict[str, str] = {}
        self._location_names: Dict[str, str] = {}
        self.shelf_drop_event: Optional[asyncio.Event] = None
        self._state_watcher_task: Optional[asyncio.Task] = None
        self._state_watcher_stop = False
        # True while return_shelf is placing the shelf — the moving-shelf id
        # legitimately disappears then, and the watcher must not call it a drop.
        self._shelf_release_expected = False
        # True once the robot stopped answering: the run still pauses, but the
        # shelf state is reported as unknown instead of dropped.
        self._disconnect_suspected = False
        self._action_handlers: Dict[str, Any] = {
            StepAction.SPEAK.value: self._do_speak,
            StepAction.MOVE_TO_POSE.value: self._do_move_to_pose,
            StepAction.MOVE_TO_LOCATION.value: self._do_move_to_location,
            StepAction.DOCK_SHELF.value: self._do_dock_shelf,
            StepAction.UNDOCK_SHELF.value: self._do_undock_shelf,
            StepAction.MOVE_SHELF.value: self._do_move_shelf,
            StepAction.RETURN_SHELF.value: self._do_return_shelf,
            StepAction.RESET_SHELF_POSE.value: self._do_reset_shelf_pose,
            StepAction.RETURN_HOME.value: self._do_return_home,
            StepAction.BIO_SCAN.value: self._do_bio_scan,
            StepAction.WAIT.value: self._do_wait,
        }

    async def _refresh_name_cache(self):
        """Fetch shelf/location names from robot for readable logs"""
        try:
            shelves_res, locations_res = await asyncio.gather(
                self.fleet.get_shelves(self.robot_id),
                self.fleet.get_locations(self.robot_id),
            )
            if shelves_res.get("ok"):
                self._shelf_names = {s["id"]: s["name"] for s in shelves_res.get("shelves", [])}
            if locations_res.get("ok"):
                self._location_names = {loc["id"]: loc["name"] for loc in locations_res.get("locations", [])}
        except Exception as e:
            logger.warning(f"Failed to refresh name cache: {e}")

    def _format_params(self, params: Dict[str, Any]) -> str:
        """Format step params with resolved names for shelf_id/location_id"""
        if not params:
            return ""
        parts = []
        for k, v in params.items():
            if k == "shelf_id" and v in self._shelf_names:
                parts.append(f"{k}={v}({self._shelf_names[v]})")
            elif k == "location_id" and v in self._location_names:
                parts.append(f"{k}={v}({self._location_names[v]})")
            else:
                parts.append(f"{k}={v}")
        return ", ".join(parts)

    # ── Shelf state watcher ───────────────────────────────────────────

    async def _watch_shelf_state(self):
        """1 Hz safety-net reading get_moving_shelf_id() via the SDK.

        The controller's on_shelf_dropped callback fires only inside
        _execute_command — i.e. during an active move_shelf. This watcher
        covers drops that happen between commands (between move_shelf and
        bio_scan, or after return_shelf), reading via the pooled connection.
        Cached slot.ctrl.state.moving_shelf_id is NOT used: the controller's
        _state_loop stops refreshing it while _monitoring_shelf=True, which
        is exactly the window we need to cover.
        """
        slot = self.fleet._robots.get(self.robot_id)
        if slot is None:
            return
        last_seen_id: Optional[str] = None
        confirmed_docked = False
        fail_streak = 0
        while not self._state_watcher_stop:
            try:
                mid = await asyncio.to_thread(slot.conn.client.get_moving_shelf_id)
                fail_streak = 0
                if self._disconnect_suspected:
                    # The robot answers again, so the suspicion is over. Left
                    # sticky it would downgrade a later real drop to "offline".
                    logger.info(
                        f"[STATE WATCHER] Robot {self.robot_id} reachable again — "
                        f"clearing disconnect suspicion"
                    )
                    self._disconnect_suspected = False
                mid = mid or None
                if mid:
                    last_seen_id = mid
                    confirmed_docked = True
                elif confirmed_docked and last_seen_id:
                    if self._shelf_release_expected:
                        # return_shelf is placing the shelf — a legal release,
                        # not a drop (2026-07-23/24 false alarms: both patrol
                        # runs ended with a normal placement flagged as a drop).
                        logger.info(
                            f"[STATE WATCHER] Shelf {last_seen_id} released by "
                            f"return_shelf — normal placement, not a drop"
                        )
                    elif self.shelf_drop_event is not None:
                        self.shelf_drop_event.set()
                    confirmed_docked = False
                    last_seen_id = None
            except Exception as e:
                # A failed read says nothing about the shelf — only a
                # successful read reporting no shelf can be a drop. Repeated
                # failures mean the robot itself is gone (2026-08-10 新營).
                fail_streak += 1
                logger.debug(
                    f"[STATE WATCHER] Read failed ({fail_streak}x): {e}", exc_info=True
                )
                if (fail_streak >= DISCONNECT_POLL_THRESHOLD
                        and is_connection_error(e)
                        and not self._disconnect_suspected):
                    logger.error(
                        f"[STATE WATCHER] Robot {self.robot_id} unreachable for "
                        f"{fail_streak} polls — treating as disconnect, shelf state unknown"
                    )
                    self._disconnect_suspected = True
                    if self.shelf_drop_event is not None:
                        self.shelf_drop_event.set()
            try:
                # 3s matches the legacy _monitor_shelf cadence; in-command
                # drops are caught faster by the controller's on_shelf_dropped.
                await asyncio.sleep(3.0)
            except asyncio.CancelledError:
                break

    # ── Shelf drop helpers ────────────────────────────────────────────

    async def _query_shelf_pose(self, shelf_id: str) -> Optional[dict]:
        """Query current shelf position from the robot, or None if unknown.

        Read through the raw SDK: kachaka_core's ``list_shelves()`` projects
        only id/name/home_location_id, so the old dict-based read always fell
        back to (0, 0, 0) — which every caller then took for "the shelf is
        lying at the map origin". ``pb2.Shelf`` carries the real pose, and
        ``HasField`` separates "not reported" from "actually at the origin".
        """
        try:
            client = self.fleet.get_raw_client(self.robot_id)
            shelves = await asyncio.to_thread(client.get_shelves)
            for s in shelves:
                if s.id == shelf_id or s.name == shelf_id:
                    if not s.HasField("pose"):
                        logger.warning(
                            f"[SHELF DROP] Shelf {shelf_id} reports no pose — unknown"
                        )
                        return None
                    shelf_pose = {"x": s.pose.x, "y": s.pose.y, "theta": s.pose.theta}
                    logger.info(f"[SHELF DROP] Shelf {shelf_id} pose: {shelf_pose}")
                    return shelf_pose
            logger.warning(f"[SHELF DROP] Shelf {shelf_id} not in shelf list — pose unknown")
        except Exception as e:
            logger.warning(f"[SHELF DROP] Failed to get shelf pose: {e}")
        return None

    def _collect_remaining_beds(self, task: Task, step_index: int,
                                trigger_step: Optional[TaskStep] = None,
                                location_id: str = "") -> List[dict]:
        """Collect remaining unprocessed beds from the task steps."""
        remaining = []
        collected = set()

        # Current bed: from trigger step's skip_on_failure
        if trigger_step and trigger_step.skip_on_failure:
            for skip_id in trigger_step.skip_on_failure:
                step = next((s for s in task.steps if s.step_id == skip_id), None)
                if step and step.action == StepAction.BIO_SCAN:
                    remaining.append({"bed_key": step.params.get("bed_key", ""), "location_id": location_id})
                    collected.add(skip_id)

        # Future unprocessed bio_scan steps
        for future in task.steps[step_index + 1:]:
            if (future.action == StepAction.BIO_SCAN
                    and future.status in (StepStatus.PENDING, StepStatus.SKIPPED)
                    and future.step_id not in collected):
                future_loc = ""
                for ms in task.steps:
                    if ms.action == StepAction.MOVE_SHELF and ms.skip_on_failure and future.step_id in ms.skip_on_failure:
                        future_loc = ms.params.get("location_id", "")
                        break
                remaining.append({"bed_key": future.params.get("bed_key", ""), "location_id": future_loc})

        # If no trigger step (polling detection), include current executing bio_scan
        if not trigger_step:
            current = task.steps[step_index] if step_index < len(task.steps) else None
            if current and current.action == StepAction.BIO_SCAN and current.status == StepStatus.EXECUTING:
                remaining.insert(0, {
                    "bed_key": current.params.get("bed_key", ""),
                    "location_id": getattr(self, "target_bed", ""),
                })

        return remaining

    def _record_skipped_scan(self, step: TaskStep, details: str,
                             location_id: str = "", extra_data: dict = None):
        """Record a skipped bio_scan step in the database."""
        try:
            client = get_bio_sensor_client()
            if not client:
                logger.warning(f"Cannot record skipped scan {step.step_id} - MQTT client not available")
                return
            data = {
                "status": "N/A",
                "bpm": None,
                "rpm": None,
                "details": details,
                "location_id": location_id or getattr(self, "target_bed", ""),
                "bed_name": step.params.get("bed_key"),
            }
            if extra_data:
                data.update(extra_data)
            client._save_scan_data(
                task_id=self.current_task_id,
                data=data,
                retry_count=0,
                is_valid=False,
            )
            logger.info(f"Recorded skipped bio_scan {step.step_id} in database")
        except Exception as e:
            logger.error(f"Failed to record skipped bio_scan {step.step_id}: {e}")

    # ── Shelf drop handler ────────────────────────────────────────────

    async def _classify_release(self) -> tuple[str, dict]:
        """Classify the release, reading the robot BEFORE anything is cancelled.

        cancel_command wipes the return-home evidence, so this has to run
        first. Returns ``(kind, battery_dict)``. Both reads coming back as
        connection failures means the robot is gone, not that the shelf fell:
        that is the ``disconnected`` verdict. A robot that answers is
        classified exactly as before.
        """
        battery: dict = {}
        command_state: dict = {}
        battery_err: object = None
        state_err: object = None
        try:
            battery = await self.fleet.get_battery_info(self.robot_id)
            battery_err = (battery or {}).get("error")
        except Exception as e:
            battery_err = e
            logger.warning(f"[SHELF DROP] Battery read failed: {e}")
        try:
            command_state = await self.fleet.get_command_state(self.robot_id)
            state_err = (command_state or {}).get("error")
        except Exception as e:
            state_err = e
            logger.warning(f"[SHELF DROP] Command state read failed: {e}")
        disconnected = self._disconnect_suspected or (
            is_connection_error(battery_err) and is_connection_error(state_err)
        )
        kind = classify_shelf_release(
            battery or {}, command_state or {}, disconnected=disconnected
        )
        return kind, (battery or {})

    async def _last_known_robot_pose(self) -> Optional[dict]:
        """Last robot pose the controller managed to read, or None.

        RobotController's state loop keeps the last successful pose and stops
        polling while disconnected, so its cached snapshot is exactly the
        "last known position" — no extra cache to maintain here.
        """
        try:
            st = await self.fleet.get_controller_state(self.robot_id)
        except Exception as e:
            logger.warning(f"[SHELF DROP] Last-known pose unavailable: {e}")
            return None
        if not st or not st.get("last_updated"):
            return None
        return {
            "x": st.get("pose_x"),
            "y": st.get("pose_y"),
            "theta": st.get("pose_theta"),
        }

    async def _handle_shelf_drop(self, task: Task, step_index: int,
                                   trigger_step: Optional[TaskStep] = None,
                                   error_code: int = 0):
        """Handle shelf drop: collect remaining beds, notify, record DB, send robot home."""
        kind, battery = await self._classify_release()
        low_battery = kind == "low_battery_return"
        disconnected = kind == "disconnected"
        battery_pct = battery.get("percentage")

        # Cancel any in-flight robot command
        try:
            await self.fleet.cancel_command(self.robot_id)
            logger.info(f"[SHELF DROP] Cancelled current command on robot {self.robot_id}")
        except Exception as ce:
            logger.debug(f"[SHELF DROP] cancel_command failed (non-critical): {ce}")

        source = f"error {error_code}" if error_code else "state watcher"
        if disconnected:
            logger.warning(
                f"[SHELF DROP] Release via {source} on robot {self.robot_id} came "
                f"from an unreachable robot — shelf state unknown, pausing task"
            )
        elif low_battery:
            logger.warning(
                f"[SHELF DROP] Release via {source} on robot {self.robot_id} is a "
                f"low-battery auto-recharge (battery={battery_pct}), pausing task"
            )
        else:
            logger.error(f"[SHELF DROP] Detected via {source} on robot {self.robot_id}, pausing task")

        location_id = trigger_step.params.get("location_id", "unknown") if trigger_step else "unknown"
        shelf_id = trigger_step.params.get("shelf_id", "unknown") if trigger_step else "unknown"
        if shelf_id == "unknown":
            shelf_id = getattr(self, "_current_shelf_id", "unknown")

        shelf_pose = await self._query_shelf_pose(shelf_id)
        # No shelf pose means the map has nothing to point the operator at —
        # the frontend must say "position unknown" instead of "look at the
        # marker", so hand it the robot's last known position to draw instead.
        last_known_robot_pose = None if shelf_pose else await self._last_known_robot_pose()
        remaining_beds = self._collect_remaining_beds(task, step_index, trigger_step, location_id)

        # Store shelf-drop context in task metadata
        task.metadata = {
            "shelf_drop": True,
            "shelf_id": shelf_id,
            "bed_key": location_id,
            "room": location_id,
            "dropped_at": get_now().isoformat(),
            "remaining_beds": remaining_beds,
            "shelf_pose": shelf_pose,
            "pose_unknown": shelf_pose is None,
            "last_known_robot_pose": last_known_robot_pose,
            "low_battery": low_battery,
            "battery_pct": battery_pct,
            "disconnect": disconnected,
        }
        if disconnected:
            # One "robot is offline" fact needs one alert: an older
            # disconnect-type alert is the same outage (or a previous one)
            # already superseded by this task. Real drops are left standing.
            # Swept BEFORE this task turns SHELF_DROPPED, so the status guard
            # naturally skips the current task.
            clear_shelf_dropped_tasks(
                only_disconnect=True, reason="superseded by newer disconnect"
            )
        task.status = TaskStatus.SHELF_DROPPED

        if disconnected:
            severity = Severity.WARN
            source_kind = Source.ROBOT_OFFLINE
            title = "🔌 機器人失聯（巡房中斷）"
            body = (
                f"棚車狀態未知\n"
                f"中斷位置：{location_id}\n"
                f"剩餘 {len(remaining_beds)} 床尚未巡視\n"
                f"請確認機器人電源與網路"
            )
        elif low_battery:
            pct_txt = f"{battery_pct:.0f}%" if isinstance(battery_pct, (int, float)) else "未知"
            severity = Severity.WARN
            source_kind = Source.SHELF_DROP
            title = "🔋 電量不足，任務中止待充電"
            body = (
                f"機器人電量 {pct_txt}，已自行放下貨架返回充電\n"
                f"中止位置：{location_id}\n"
                f"剩餘 {len(remaining_beds)} 床尚未巡視\n"
                f"充電完成後可續巡"
            )
        else:
            severity = Severity.CRITICAL
            source_kind = Source.SHELF_DROP
            title = "⚠️ 貨架掉落，請協助歸位"
            body = (
                f"掉落位置：{location_id}\n"
                f"剩餘 {len(remaining_beds)} 床尚未巡視\n"
                f"機器人將返回充電站"
            )
        try:
            await dispatcher.dispatch(AnomalyEvent(
                severity=severity,
                source=source_kind,
                bed_key=location_id,
                task_id=task.task_id,
                title=title,
                body=body,
                raw={
                    "shelf_id": shelf_id,
                    "remaining_beds": remaining_beds,
                    "low_battery": low_battery,
                    "battery_pct": battery_pct,
                    "disconnect": disconnected,
                },
            ))
        except Exception:
            # Operator-critical path — must keep going even if dispatcher breaks.
            logger.exception("Failed to dispatch shelf-drop anomaly")

        # Record all skipped bio_scan steps to DB
        steps_to_skip = []
        if trigger_step and trigger_step.skip_on_failure:
            for skip_id in trigger_step.skip_on_failure:
                s = next((s for s in task.steps if s.step_id == skip_id), None)
                if s and s.action == StepAction.BIO_SCAN:
                    steps_to_skip.append(s)
        for future in task.steps[step_index + 1:]:
            if future.action == StepAction.BIO_SCAN and future.status == StepStatus.PENDING and future not in steps_to_skip:
                steps_to_skip.append(future)

        if disconnected:
            skip_detail = "機器人失聯，巡房中斷"
        elif low_battery:
            skip_detail = "電量不足，巡房中止待充電"
        else:
            skip_detail = "貨架掉落，巡房中斷"
        for s in steps_to_skip:
            self._record_skipped_scan(s, skip_detail, location_id=s.params.get("location_id", ""))
            s.status = StepStatus.SKIPPED

        # Robot return home — controller failures come back as {"ok": False},
        # not exceptions, so check the result explicitly. An unreachable robot
        # takes no commands, so sending it home would only burn the
        # controller's 240s timeout inside the drop handler.
        if disconnected:
            logger.info(
                f"[SHELF DROP] Robot {self.robot_id} unreachable — skipping return_home"
            )
            return
        try:
            rh = await self.fleet.return_home(self.robot_id)
            if rh.get("ok"):
                logger.info(f"[SHELF DROP] Robot {self.robot_id} sent home")
            else:
                logger.error(f"[SHELF DROP] return_home failed: {rh.get('error')}")
        except Exception as rh_err:
            logger.error(f"[SHELF DROP] Failed to send robot home: {rh_err}")

    # ── Task execution ────────────────────────────────────────────────

    async def run_task(self, task: Task) -> Task:
        logger.info(f"===> Starting task: {task.task_id} on robot {task.robot_id}")
        await self._refresh_name_cache()
        task.status = TaskStatus.IN_PROGRESS
        current_tasks[task.robot_id] = task.task_id
        self.current_task_id = task.task_id
        self.task_start_time = get_now().strftime("%Y%m%d%H%M%S")
        slot = self.fleet.get_slot_or_none(self.robot_id) if hasattr(self.fleet, "get_slot_or_none") else None
        slot_evt = slot.shelf_drop_event if slot is not None else None
        self.shelf_drop_event = slot_evt if isinstance(slot_evt, asyncio.Event) else asyncio.Event()
        self.shelf_drop_event.clear()
        self._shelf_release_expected = False
        self._disconnect_suspected = False
        self._state_watcher_stop = False
        self._state_watcher_task = asyncio.create_task(self._watch_shelf_state())

        try:
            step_index = 0
            skipped_steps = set()
            skip_reasons = {}

            while step_index < len(task.steps):
                step = task.steps[step_index]

                if task.status == TaskStatus.CANCELLED:
                    logger.info(f"[!] Task {task.task_id} on robot {self.robot_id} cancelled mid-execution")
                    break

                # --- SHELF DROP via slot event / state watcher ---
                if self.shelf_drop_event.is_set():
                    await self._handle_shelf_drop(task, step_index)
                    break

                # Check if this step should be skipped
                if step.step_id in skipped_steps:
                    logger.info(f"[SKIP] Robot {self.robot_id}, Step {step.step_id} skipped due to conditional logic")
                    step.status = StepStatus.SKIPPED
                    skip_reason = skip_reasons.get(step.step_id, {})

                    if step.action == StepAction.BIO_SCAN:
                        self._record_skipped_scan(step, "機器人無法移動到床邊", extra_data={
                            "error_source": skip_reason.get("failed_step_id"),
                            "original_error_code": skip_reason.get("error_code"),
                            "original_error_message": skip_reason.get("error_message"),
                        })

                    step.result = StepResult(
                        success=False,
                        error_code=skip_reason.get("error_code", 0),
                        error_message=skip_reason.get("error_message", "Step skipped due to previous step failure"),
                        data={
                            "reason": "conditional_skip",
                            "caused_by_step": skip_reason.get("failed_step_id"),
                            "original_error": skip_reason.get("original_error")
                        },
                        timestamp=get_now().isoformat()
                    )
                    step_index += 1
                    continue

                params_str = self._format_params(step.params)
                logger.info(f"---> Robot {self.robot_id}, Step: {step.step_id} | Action: {step.action}({params_str})")
                step.status = StepStatus.EXECUTING

                try:
                    step_result = await self._execute_step(step)
                    step.result = step_result
                    step.status = StepStatus.SUCCESS if step_result.success else StepStatus.FAIL

                    # Shelf drop detected during step execution
                    if self.shelf_drop_event.is_set():
                        await self._handle_shelf_drop(task, step_index, trigger_step=step)
                        break

                    if step_result.success:
                        logger.info(f"[✓] Robot {self.robot_id}, Step {step.step_id} completed successfully")
                    else:
                        logger.warning(f"[!] Robot {self.robot_id}, Step {step.step_id} failed: {step_result.error_message} (code: {step_result.error_code})")

                        if step.skip_on_failure:
                            skipped_steps.update(step.skip_on_failure)
                            logger.info(f"[CONDITIONAL] Step {step.step_id} failed, will skip steps: {step.skip_on_failure}")
                            for skip_step_id in step.skip_on_failure:
                                skip_reasons[skip_step_id] = {
                                    "failed_step_id": step.step_id,
                                    "error_code": step_result.error_code,
                                    "error_message": step_result.error_message,
                                    "original_error": step_result.data,
                                }
                        elif step.action in NON_CRITICAL_ACTIONS:
                            logger.warning(f"[NON-CRITICAL] Step {step.step_id} ({step.action}) failed, continuing to next step")
                        else:
                            if task.status != TaskStatus.CANCELLED:
                                task.status = TaskStatus.FAILED
                            break

                except Exception as e:
                    logger.error(f"[X] Robot {self.robot_id}, Exception in step {step.step_id}: {str(e)}", exc_info=True)
                    step.result = StepResult(
                        success=False, error_code=-1,
                        error_message=f"TaskEngine exception: {str(e)}",
                        data={"step_id": step.step_id, "action": step.action},
                        timestamp=get_now().isoformat()
                    )
                    step.status = StepStatus.FAIL

                    if step.skip_on_failure:
                        skipped_steps.update(step.skip_on_failure)
                        logger.info(f"[CONDITIONAL] Step {step.step_id} exception, will skip steps: {step.skip_on_failure}")
                        for skip_step_id in step.skip_on_failure:
                            skip_reasons[skip_step_id] = {
                                "failed_step_id": step.step_id,
                                "error_code": step.result.error_code,
                                "error_message": step.result.error_message,
                                "original_error": step.result.data,
                            }
                    elif step.action in NON_CRITICAL_ACTIONS:
                        logger.warning(f"[NON-CRITICAL] Step {step.step_id} ({step.action}) exception, continuing to next step")
                    else:
                        if task.status != TaskStatus.CANCELLED:
                            task.status = TaskStatus.FAILED
                        break

                step_index += 1

            if task.status == TaskStatus.IN_PROGRESS:
                task.status = TaskStatus.DONE
                logger.info(f"===> Task {task.task_id} completed successfully on robot {self.robot_id}")

            # Collect metrics from kachaka_core controller
            if task.status in (TaskStatus.DONE, TaskStatus.FAILED):
                try:
                    m = await self.fleet.get_metrics(self.robot_id)
                    if task.metadata is None:
                        task.metadata = {}
                    task.metadata["metrics"] = {
                        "poll_count": m["poll_count"],
                        "avg_rtt_ms": round(sum(m["poll_rtt_list"]) / len(m["poll_rtt_list"]), 1) if m["poll_rtt_list"] else 0,
                        "poll_success_rate": round(m["poll_success_count"] / m["poll_count"], 3) if m["poll_count"] else 1.0,
                    }
                    await self.fleet.reset_metrics(self.robot_id)
                except Exception:
                    pass

        finally:
            tag = f"Task {task.task_id}"
            self._state_watcher_stop = True
            if self._state_watcher_task is not None:
                self._state_watcher_task.cancel()
                try:
                    await self._state_watcher_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._state_watcher_task = None
            cancelled = task.status == TaskStatus.CANCELLED

            # Cancelled cleanup — queued as its own task so it is visible in
            # the dashboard and, crucially, decides "is a shelf held" from the
            # robot itself instead of _current_shelf_id (which misses a cancel
            # during the first move_shelf). Guard keeps a cancelled cleanup
            # from spawning another cleanup.
            if cancelled and (task.metadata or {}).get("mode") != "cleanup":
                try:
                    await submit_cleanup_task(self.robot_id)
                except Exception as e:
                    logger.error(f"[{tag}] Failed to queue cancel cleanup: {e}")

            try:
                bio_steps = [s for s in task.steps if s.action == StepAction.BIO_SCAN]
                total_beds = len(bio_steps)
                success_beds = sum(1 for s in bio_steps if s.status == StepStatus.SUCCESS)
                if cancelled:
                    title = "🚫 巡房已取消"
                elif task.status == TaskStatus.DONE:
                    title = "✅ 巡房完成"
                else:
                    title = "⚠️ 巡房中斷"
                buckets = self._run_outcome_buckets(task.task_id)
                if buckets is not None:
                    # Same per-bed buckets as the history view: reaching the
                    # bed is the success bar; restless / empty-bed are reports.
                    body = f"本次巡房 {total_beds} 床\n正常量測 {len(buckets['valid'])} 床"
                    if buckets["restless"]:
                        body += f"\n躁動通報 {len(buckets['restless'])} 床：{'、'.join(buckets['restless'])}"
                    if buckets["no_reading"]:
                        body += f"\n無量測值 {len(buckets['no_reading'])} 床：{'、'.join(buckets['no_reading'])}"
                    if buckets["unreachable"]:
                        body += f"\n機器人無法到位 {len(buckets['unreachable'])} 床：{'、'.join(buckets['unreachable'])}"
                    not_executed = total_beds - sum(len(v) for v in buckets.values())
                    if not_executed > 0:
                        body += f"\n未執行 {not_executed} 床"
                else:
                    # DB unavailable — fall back to the step-status view
                    body = (
                        f"本次巡房 {total_beds} 床\n"
                        f"{'已完成' if cancelled else '成功讀取'} {success_beds} 床"
                    )
                    missed = [
                        str(s.params.get("bed_key", "?"))
                        for s in bio_steps
                        if s.status != StepStatus.SUCCESS
                    ]
                    if missed:
                        body += f"\n未量測：{'、'.join(missed)}"
                await dispatcher.dispatch(AnomalyEvent(
                    severity=Severity.INFO,
                    source=Source.TASK_SUMMARY,
                    task_id=task.task_id,
                    title=title,
                    body=body,
                    raw={
                        "cancelled": cancelled,
                        "total_beds": total_beds,
                        "success_beds": success_beds,
                    },
                ))
            except Exception:
                logger.exception("Failed to dispatch task-summary anomaly")
            current_tasks.pop(self.robot_id, None)
            logger.info(f"Robot {self.robot_id} is now free.")
        return task

    # ── Step execution ────────────────────────────────────────────────

    def _make_result(self, api_result: dict, action: str, data: dict) -> StepResult:
        """Create StepResult from a robot API result dict."""
        return StepResult(
            success=api_result.get("ok", False),
            error_code=api_result.get("error_code", 0),
            error_message=api_result.get("error", "") if not api_result.get("ok") else "",
            data=data,
            timestamp=get_now().isoformat(),
        )

    async def _execute_step(self, step: TaskStep) -> StepResult:
        action = step.action
        handler = self._action_handlers.get(action)
        if handler is None:
            logger.error(f"Unknown action: {action} for robot {self.robot_id}")
            return StepResult(
                success=False, error_code=-1,
                error_message=f"Unknown action: {action}",
                data={"action": action}, timestamp=get_now().isoformat(),
            )
        try:
            return await handler(step)
        except ValueError as e:
            logger.error(f"[!] Robot {self.robot_id} not found: {str(e)}")
            return StepResult(
                success=False, error_code=-1,
                error_message=f"Robot {self.robot_id} not found: {str(e)}",
                data={"action": action, "params": step.params},
                timestamp=get_now().isoformat(),
            )
        except Exception as e:
            logger.error(f"[X] Unexpected error during action {action} for robot {self.robot_id}: {str(e)}", exc_info=True)
            return StepResult(
                success=False, error_code=-1,
                error_message=f"Unexpected error: {str(e)}",
                data={"action": action, "params": step.params},
                timestamp=get_now().isoformat(),
            )

    # ── Action handlers ───────────────────────────────────────────────

    async def _do_speak(self, step: TaskStep) -> StepResult:
        text = step.params["speak_text"]
        result = await self.fleet.speak(self.robot_id, text)
        return self._make_result(result, step.action, {"speak_text": text})

    async def _do_move_to_pose(self, step: TaskStep) -> StepResult:
        x, y, yaw = float(step.params["x"]), float(step.params["y"]), float(step.params["yaw"])
        result = await self.fleet.move_to_pose(self.robot_id, x, y, yaw)
        return self._make_result(result, step.action, {"x": x, "y": y, "yaw": yaw})

    async def _do_move_to_location(self, step: TaskStep) -> StepResult:
        location_id = step.params["location_id"]
        result = await self.fleet.move_to_location(self.robot_id, location_id)
        return self._make_result(result, step.action, {"location_id": location_id})

    async def _do_dock_shelf(self, step: TaskStep) -> StepResult:
        result = await self.fleet.dock_shelf(self.robot_id)
        return self._make_result(result, step.action, {})

    async def _do_undock_shelf(self, step: TaskStep) -> StepResult:
        result = await self.fleet.undock_shelf(self.robot_id)
        return self._make_result(result, step.action, {})

    async def _do_reset_shelf_pose(self, step: TaskStep) -> StepResult:
        shelf_id = step.params["shelf_id"]
        result = await self.fleet.reset_shelf_pose(self.robot_id, shelf_id)
        # A successful reset proves the robot answers again, so offline-type
        # alerts from earlier runs are stale — sweeping them here lets the
        # first run after an offline weekend clear the backlog by itself.
        # only_disconnect: the reset is a pure pose write with no physical
        # verification, and this step also opens non-patrol tasks (the
        # shelf-to-NS button builds it directly), so it must never eat a real
        # CRITICAL drop — that takes an explicit recover-shelf.
        if result.get("ok"):
            clear_shelf_dropped_tasks(
                only_disconnect=True, reason="reset_shelf_pose step succeeded"
            )
        return self._make_result(result, step.action, {"shelf_id": shelf_id})

    async def _do_move_shelf(self, step: TaskStep) -> StepResult:
        shelf_id = step.params["shelf_id"]
        location_id = step.params["location_id"]
        self.target_bed = location_id
        result = await self.fleet.move_shelf(self.robot_id, shelf_id, location_id)
        if result.get("ok"):
            self._current_shelf_id = shelf_id
            if self.shelf_drop_event is not None:
                self.shelf_drop_event.clear()
        return self._make_result(result, step.action, {"shelf_id": shelf_id, "location_id": location_id})

    async def _do_return_shelf(self, step: TaskStep) -> StepResult:
        shelf_id = step.params["shelf_id"]
        # Mute the drop watcher for the placement: the moving-shelf id
        # disappearing during return_shelf is the shelf being set down.
        self._shelf_release_expected = True
        result = await self.fleet.return_shelf(self.robot_id, shelf_id)
        # kachaka_core's controller arms its shelf monitor on move_shelf and
        # never disarms it, so its poll loop ALSO sees the placement as a
        # "drop" and sets the slot event (2026-07-24 noon false alarm, second
        # source). A completed placement — or a failed one with the shelf
        # verified at/on home — means any pending drop signal is stale.
        if result.get("ok"):
            dropped = False
        else:
            dropped = await self._shelf_dropped_en_route(shelf_id)
        if dropped:
            self._shelf_release_expected = False
            if self.shelf_drop_event is not None:
                self.shelf_drop_event.set()
        elif self.shelf_drop_event is not None and self.shelf_drop_event.is_set():
            logger.info("[SHELF] Clearing stale drop signal raised during normal placement")
            self.shelf_drop_event.clear()
        return self._make_result(result, step.action, {"shelf_id": shelf_id})

    def _run_outcome_buckets(self, task_id: str) -> Optional[dict]:
        """Per-bed outcome buckets for one run, from the scan DB.

        Same classification as the frontend history view: one outcome row per
        bed (the valid row if any, else the final attempt), bucketed into
        valid / restless (status 2) / unreachable (skipped, status 'N/A') /
        no-reading (everything else). None when the DB can't be read.
        """
        client = get_bio_sensor_client()
        if client is None:
            return None
        try:
            conn = connect_db(client.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT bed_name, status, is_valid, retry_count "
                "FROM sensor_scan_data WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            conn.close()
        except Exception:
            logger.exception("Failed to load scan rows for run summary")
            return None
        best: Dict[str, Any] = {}
        for r in rows:
            bed = r["bed_name"] or "?"
            cur = best.get(bed)
            if (
                cur is None
                or (r["is_valid"] and not cur["is_valid"])
                or (bool(r["is_valid"]) == bool(cur["is_valid"])
                    and (r["retry_count"] or 0) > (cur["retry_count"] or 0))
            ):
                best[bed] = r
        buckets: Dict[str, list] = {"valid": [], "restless": [], "unreachable": [], "no_reading": []}
        for bed, r in best.items():
            if r["is_valid"]:
                buckets["valid"].append(bed)
            elif str(r["status"]) == "2":
                buckets["restless"].append(bed)
            elif str(r["status"]) == "N/A":
                buckets["unreachable"].append(bed)
            else:
                buckets["no_reading"].append(bed)
        for v in buckets.values():
            v.sort()
        return buckets

    async def _shelf_dropped_en_route(self, shelf_id: str) -> bool:
        """After a failed return_shelf: did the shelf actually fall off?

        False when the robot still carries it (plain navigation failure) or
        when it already sits at its home (the robot finished the placement
        after the app-side timeout). Unknown → True: a missed drop leaves a
        shelf loose in a hospital corridor, so err toward alerting — except
        for an unreadable pose, which is not evidence of anything (see below).
        """
        try:
            slot = self.fleet.get_slot_or_none(self.robot_id) if hasattr(self.fleet, "get_slot_or_none") else None
            if slot is not None:
                mid = await asyncio.to_thread(slot.conn.client.get_moving_shelf_id)
                if mid:
                    return False
            shelves_res = await self.fleet.get_shelves(self.robot_id)
            shelf = next(
                (s for s in shelves_res.get("shelves", [])
                 if s.get("id") == shelf_id or s.get("name") == shelf_id),
                None,
            )
            if shelf is None:
                return True
            locs_res = await self.fleet.get_locations(self.robot_id)
            home = next(
                (l for l in locs_res.get("locations", [])
                 if l.get("id") == shelf.get("home_location_id")),
                None,
            )
            if home is None:
                return True
            sp = await self._query_shelf_pose(shelf_id)
            if sp is None:
                # The shelf list carries no pose, so this check used to read
                # (0, 0, 0) and every failed return_shelf became a "drop".
                # Without a real pose there is nothing to verify — skip it.
                logger.warning(
                    f"[SHELF] Shelf {shelf_id} pose unavailable — skipping pose "
                    f"verification, not reporting a drop"
                )
                return False
            hp = home.get("pose", {}) or {}
            dx = sp["x"] - hp.get("x", 0)
            dy = sp["y"] - hp.get("y", 0)
            return (dx * dx + dy * dy) ** 0.5 > 1.5
        except Exception as e:
            logger.warning("[SHELF] Could not verify shelf state after failed return", exc_info=True)
            if is_connection_error(e):
                # The robot is gone, so the verification never ran: the run
                # still stops, but as "state unknown", not a CRITICAL drop
                # (2026-08-10 新營: an offline robot raised a drop alarm for a
                # shelf nobody could confirm had moved).
                self._disconnect_suspected = True
            return True

    async def _do_return_home(self, step: TaskStep) -> StepResult:
        result = await self.fleet.return_home(self.robot_id)
        # 10267 = "It is already charged": the robot is already on the charger,
        # which is exactly what return_home wants — not a failure.
        if isinstance(result, dict) and result.get("error_code") == 10267:
            result = {**result, "ok": True, "success": True}
        return self._make_result(result, step.action, {})

    async def _await_or_drop(self, coro):
        """Await ``coro``, aborting it as soon as shelf_drop_event fires.

        Returns None when the drop won. A bio_scan waits out its initial
        window plus the retry loop (minutes), and the drop event used to be
        consumed only at the next step boundary — one field drop sat unhandled
        for 3m22s. Racing the two here brings the reaction back to ~instant.
        """
        task = asyncio.ensure_future(coro)
        if self.shelf_drop_event is None:
            return await task
        drop = asyncio.ensure_future(self.shelf_drop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {task, drop}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # Worker cancelled (shutdown) — take the wrapped call down with us,
            # exactly as a plain `await` would have.
            task.cancel()
            raise
        finally:
            drop.cancel()
        if task in done:
            return task.result()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return None

    async def _do_bio_scan(self, step: TaskStep) -> StepResult:
        client = get_bio_sensor_client()
        if client is None:
            return StepResult(
                success=False, error_code=-1,
                error_message="Bio-sensor MQTT client is not available (mqtt_enabled=false)",
                data={}, timestamp=get_now().isoformat(),
            )
        bed_key = step.params.get("bed_key")
        outcome = await self._await_or_drop(client.get_valid_scan_data(
            target_bed=self.target_bed, task_id=self.current_task_id, bed_name=bed_key,
        ))
        if outcome is None:
            # run_task sees the same event right after this step returns and
            # routes into _handle_shelf_drop.
            logger.warning(f"Bio scan on robot {self.robot_id} aborted by shelf release")
            return StepResult(
                success=False, error_code=-1,
                error_message="Bio scan interrupted by shelf release",
                data={"bed_key": bed_key}, timestamp=get_now().isoformat(),
            )
        success = outcome.valid_record is not None
        logger.info(
            f"Bio scan outcome for robot {self.robot_id}: valid={success} "
            f"retry_count={outcome.retry_count}"
        )

        event = _bio_scan_evaluator.evaluate(outcome)
        if event:
            await dispatcher.dispatch(event)

        return StepResult(
            success=success,
            error_code=0 if success else -1,
            error_message=(
                "Bio scan completed successfully" if success
                else "No valid data obtained after all retries"
            ),
            data=outcome.valid_record or {
                "task_id": outcome.task_id, "details": outcome.last_failure_reason,
            },
            timestamp=get_now().isoformat(),
        )

    async def _do_wait(self, step: TaskStep) -> StepResult:
        seconds = float(step.params.get("seconds", "1.0"))
        await asyncio.sleep(seconds)
        return StepResult(
            success=True, error_code=0,
            error_message="Wait completed successfully",
            data={"seconds": seconds}, timestamp=get_now().isoformat(),
        )


async def task_worker(robot_id: str):
    queue = task_queues[robot_id]
    engine = engines[robot_id]
    logger.info(f"Task worker started for robot {robot_id}")
    while True:
        task = await queue.get()
        logger.info(f"Robot {robot_id} worker: got task {task.task_id} from its queue.")
        if task.status != TaskStatus.CANCELLED:
            updated_task = await engine.run_task(task)
            tasks_db[task.task_id] = updated_task
        else:
            logger.info(f"Robot {robot_id} worker: task {task.task_id} was already cancelled. Not running.")
        queue.task_done()
