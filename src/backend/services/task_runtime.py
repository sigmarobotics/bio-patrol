import asyncio
import logging
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

logger = logging.getLogger("kachaka.task_runtime")

_bio_scan_evaluator = BioScanFailureEvaluator()

# --- global states ---
tasks_db: Dict[str, Task] = {}
engines: Dict[str, "TaskEngine"] = {}
task_queues: Dict[str, asyncio.Queue] = {}
current_tasks: Dict[str, str] = {}  # robot_id -> task_id


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


class TaskEngine:
    def __init__(self, fleet_api: FleetAPI, robot_id: str):
        self.fleet = fleet_api
        self.robot_id = robot_id
        self._shelf_names: Dict[str, str] = {}
        self._location_names: Dict[str, str] = {}
        self.shelf_drop_event: Optional[asyncio.Event] = None
        self._state_watcher_task: Optional[asyncio.Task] = None
        self._state_watcher_stop = False
        self._action_handlers: Dict[str, Any] = {
            StepAction.SPEAK.value: self._do_speak,
            StepAction.MOVE_TO_POSE.value: self._do_move_to_pose,
            StepAction.MOVE_TO_LOCATION.value: self._do_move_to_location,
            StepAction.DOCK_SHELF.value: self._do_dock_shelf,
            StepAction.UNDOCK_SHELF.value: self._do_undock_shelf,
            StepAction.MOVE_SHELF.value: self._do_move_shelf,
            StepAction.RETURN_SHELF.value: self._do_return_shelf,
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
        while not self._state_watcher_stop:
            try:
                mid = await asyncio.to_thread(slot.conn.client.get_moving_shelf_id)
                mid = mid or None
                if mid:
                    last_seen_id = mid
                    confirmed_docked = True
                elif confirmed_docked and last_seen_id:
                    if self.shelf_drop_event is not None:
                        self.shelf_drop_event.set()
                    confirmed_docked = False
                    last_seen_id = None
            except Exception:
                logger.debug("[STATE WATCHER] Transient error", exc_info=True)
            try:
                # 3s matches the legacy _monitor_shelf cadence; in-command
                # drops are caught faster by the controller's on_shelf_dropped.
                await asyncio.sleep(3.0)
            except asyncio.CancelledError:
                break

    # ── Shelf drop helpers ────────────────────────────────────────────

    async def _query_shelf_pose(self, shelf_id: str) -> Optional[dict]:
        """Query current shelf position from the robot."""
        try:
            result = await self.fleet.get_shelves(self.robot_id)
            if result.get("ok"):
                for s in result.get("shelves", []):
                    if s.get("id") == shelf_id:
                        pose = s.get("pose", {})
                        shelf_pose = {"x": pose.get("x", 0), "y": pose.get("y", 0), "theta": pose.get("theta", 0)}
                        logger.info(f"[SHELF DROP] Shelf {shelf_id} pose: {shelf_pose}")
                        return shelf_pose
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

    async def _handle_shelf_drop(self, task: Task, step_index: int,
                                   trigger_step: Optional[TaskStep] = None,
                                   error_code: int = 0):
        """Handle shelf drop: collect remaining beds, notify, record DB, send robot home."""
        # Cancel any in-flight robot command
        try:
            await self.fleet.cancel_command(self.robot_id)
            logger.info(f"[SHELF DROP] Cancelled current command on robot {self.robot_id}")
        except Exception as ce:
            logger.debug(f"[SHELF DROP] cancel_command failed (non-critical): {ce}")

        source = f"error {error_code}" if error_code else "state watcher"
        logger.error(f"[SHELF DROP] Detected via {source} on robot {self.robot_id}, pausing task")

        location_id = trigger_step.params.get("location_id", "unknown") if trigger_step else "unknown"
        shelf_id = trigger_step.params.get("shelf_id", "unknown") if trigger_step else "unknown"
        if shelf_id == "unknown":
            shelf_id = getattr(self, "_current_shelf_id", "unknown")

        shelf_pose = await self._query_shelf_pose(shelf_id)
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
        }
        task.status = TaskStatus.SHELF_DROPPED

        try:
            await dispatcher.dispatch(AnomalyEvent(
                severity=Severity.CRITICAL,
                source=Source.SHELF_DROP,
                bed_key=location_id,
                task_id=task.task_id,
                title="⚠️ 貨架掉落，請協助歸位",
                body=(
                    f"掉落位置：{location_id}\n"
                    f"剩餘 {len(remaining_beds)} 床尚未巡視\n"
                    f"機器人已返回充電站"
                ),
                raw={"shelf_id": shelf_id, "remaining_beds": remaining_beds},
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

        for s in steps_to_skip:
            self._record_skipped_scan(s, "貨架掉落，巡房中斷", location_id=s.params.get("location_id", ""))
            s.status = StepStatus.SKIPPED

        # Robot return home
        try:
            await self.fleet.return_home(self.robot_id)
            logger.info(f"[SHELF DROP] Robot {self.robot_id} sent home")
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

            # Cancelled cleanup: return shelf and go home
            if cancelled and getattr(self, "_current_shelf_id", None):
                try:
                    await self.fleet.return_shelf(self.robot_id, self._current_shelf_id)
                    logger.info(f"[{tag}] Cancelled: returned shelf {self._current_shelf_id}")
                    await self.fleet.return_home(self.robot_id)
                    logger.info(f"[{tag}] Cancelled: robot sent home")
                except Exception as e:
                    logger.error(f"[{tag}] Cancelled cleanup error: {e}")

            try:
                bio_steps = [s for s in task.steps if s.action == StepAction.BIO_SCAN]
                total_beds = len(bio_steps)
                success_beds = sum(1 for s in bio_steps if s.status == StepStatus.SUCCESS)
                title = "🚫 巡房已取消" if cancelled else "✅ 巡房完成"
                body = (
                    f"本次巡房 {total_beds} 床\n"
                    f"{'已完成' if cancelled else '成功讀取'} {success_beds} 床"
                )
                # Beds the nursing staff must follow up manually
                missed = [
                    s.params.get("bed_key", "?")
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
        result = await self.fleet.return_shelf(self.robot_id, shelf_id)
        return self._make_result(result, step.action, {"shelf_id": shelf_id})

    async def _do_return_home(self, step: TaskStep) -> StepResult:
        result = await self.fleet.return_home(self.robot_id)
        return self._make_result(result, step.action, {})

    async def _do_bio_scan(self, step: TaskStep) -> StepResult:
        client = get_bio_sensor_client()
        if client is None:
            return StepResult(
                success=False, error_code=-1,
                error_message="Bio-sensor MQTT client is not available (mqtt_enabled=false)",
                data={}, timestamp=get_now().isoformat(),
            )
        bed_key = step.params.get("bed_key")
        outcome = await client.get_valid_scan_data(
            target_bed=self.target_bed, task_id=self.current_task_id, bed_name=bed_key,
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
