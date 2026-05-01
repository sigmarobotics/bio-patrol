import asyncio
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime
from services.fleet_api import FleetAPI
from services.notifications.dispatcher import dispatcher
from services.notifications.evaluator import BioScanFailureEvaluator
from common_types import Task, TaskStep, TaskStatus, StepStatus, StepResult, get_now
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
        self._shelf_dropped = False
        self._shelf_monitor_stop = False
        self._shelf_monitor_task: Optional[asyncio.Task] = None

    async def _refresh_name_cache(self):
        """Fetch shelf/location names from robot for readable logs"""
        try:
            result = await self.fleet.get_shelves(self.robot_id)
            if result.get("ok"):
                self._shelf_names = {s["id"]: s["name"] for s in result.get("shelves", [])}
            result = await self.fleet.get_locations(self.robot_id)
            if result.get("ok"):
                self._location_names = {loc["id"]: loc["name"] for loc in result.get("locations", [])}
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

    # ── Shelf monitor ─────────────────────────────────────────────────

    async def _monitor_shelf(self):
        """Background coroutine that polls get_moving_shelf() every 3s.
        Sets _shelf_dropped flag if the robot no longer carries a shelf."""
        logger.info(f"[SHELF MONITOR] Started for robot {self.robot_id}")
        while not self._shelf_monitor_stop:
            await asyncio.sleep(3)
            if self._shelf_monitor_stop:
                break
            try:
                result = await self.fleet.get_moving_shelf(self.robot_id)
                shelf_id = result.get("shelf_id") if result.get("ok") else None
                if not shelf_id:
                    logger.warning(f"[SHELF MONITOR] Robot {self.robot_id} no longer carrying a shelf — shelf dropped!")
                    self._shelf_dropped = True
                    try:
                        await self.fleet.cancel_command(self.robot_id)
                        logger.info(f"[SHELF MONITOR] Cancelled current command on robot {self.robot_id}")
                    except Exception as ce:
                        logger.debug(f"[SHELF MONITOR] cancel_command failed (non-critical): {ce}")
                    break
            except Exception as e:
                logger.debug(f"[SHELF MONITOR] Transient error polling shelf for robot {self.robot_id}: {e}")
        logger.info(f"[SHELF MONITOR] Stopped for robot {self.robot_id}")

    async def _stop_shelf_monitor(self):
        """Stop the shelf monitor background task."""
        self._shelf_monitor_stop = True
        if self._shelf_monitor_task is not None:
            self._shelf_monitor_task.cancel()
            try:
                await self._shelf_monitor_task
            except asyncio.CancelledError:
                pass
            self._shelf_monitor_task = None
        logger.info(f"[SHELF MONITOR] Cleaned up for robot {self.robot_id}")

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
                if step and step.action == "bio_scan":
                    remaining.append({"bed_key": step.params.get("bed_key", ""), "location_id": location_id})
                    collected.add(skip_id)

        # Future unprocessed bio_scan steps
        for future in task.steps[step_index + 1:]:
            if (future.action == "bio_scan"
                    and future.status in (StepStatus.PENDING, StepStatus.SKIPPED)
                    and future.step_id not in collected):
                future_loc = ""
                for ms in task.steps:
                    if ms.action == "move_shelf" and ms.skip_on_failure and future.step_id in ms.skip_on_failure:
                        future_loc = ms.params.get("location_id", "")
                        break
                remaining.append({"bed_key": future.params.get("bed_key", ""), "location_id": future_loc})

        # If no trigger step (polling detection), include current executing bio_scan
        if not trigger_step:
            current = task.steps[step_index] if step_index < len(task.steps) else None
            if current and current.action == "bio_scan" and current.status == StepStatus.EXECUTING:
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
        await self._stop_shelf_monitor()

        # Cancel any in-flight robot command
        try:
            await self.fleet.cancel_command(self.robot_id)
            logger.info(f"[SHELF DROP] Cancelled current command on robot {self.robot_id}")
        except Exception as ce:
            logger.debug(f"[SHELF DROP] cancel_command failed (non-critical): {ce}")

        source = f"error {error_code}" if error_code else "polling monitor"
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

        # Telegram notification
        try:
            from services.telegram_service import send_telegram_message
            await send_telegram_message("⚠️ 貨架掉落，請協助歸位")
        except Exception as tg_err:
            logger.error(f"Failed to send shelf-drop Telegram: {tg_err}")

        # Record all skipped bio_scan steps to DB
        steps_to_skip = []
        if trigger_step and trigger_step.skip_on_failure:
            for skip_id in trigger_step.skip_on_failure:
                s = next((s for s in task.steps if s.step_id == skip_id), None)
                if s and s.action == "bio_scan":
                    steps_to_skip.append(s)
        for future in task.steps[step_index + 1:]:
            if future.action == "bio_scan" and future.status == StepStatus.PENDING and future not in steps_to_skip:
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
        self._shelf_dropped = False
        self._shelf_monitor_stop = False
        self._shelf_monitor_task = None

        try:
            step_index = 0
            skipped_steps = set()
            skip_reasons = {}

            while step_index < len(task.steps):
                step = task.steps[step_index]

                if task.status == TaskStatus.CANCELLED:
                    logger.info(f"[!] Task {task.task_id} on robot {self.robot_id} cancelled mid-execution")
                    break

                # --- SHELF DROP via polling monitor ---
                if self._shelf_dropped:
                    await self._handle_shelf_drop(task, step_index)
                    break

                # Check if this step should be skipped
                if step.step_id in skipped_steps:
                    logger.info(f"[SKIP] Robot {self.robot_id}, Step {step.step_id} skipped due to conditional logic")
                    step.status = StepStatus.SKIPPED
                    skip_reason = skip_reasons.get(step.step_id, {})

                    if step.action == "bio_scan":
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
                    skip_reason = skip_reasons.get(step.step_id) if step.step_id in skip_reasons else None
                    step_result = await self._execute_step(step, skip_reason)
                    step.result = step_result
                    step.status = StepStatus.SUCCESS if step_result.success else StepStatus.FAIL

                    # Shelf drop detected during step execution
                    if self._shelf_dropped:
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
                        elif step.action in ("bio_scan", "wait", "speak", "return_shelf"):
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
                    elif step.action in ("bio_scan", "wait", "speak", "return_shelf"):
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
            await self._stop_shelf_monitor()

            # Cancelled cleanup: return shelf and go home
            if task.status == TaskStatus.CANCELLED and getattr(self, "_current_shelf_id", None):
                try:
                    await self.fleet.return_shelf(self.robot_id, self._current_shelf_id)
                    logger.info(f"[{tag}] Cancelled: returned shelf {self._current_shelf_id}")
                    await self.fleet.return_home(self.robot_id)
                    logger.info(f"[{tag}] Cancelled: robot sent home")
                except Exception as e:
                    logger.error(f"[{tag}] Cancelled cleanup error: {e}")

            try:
                from services.telegram_service import send_telegram_message
                bio_steps = [s for s in task.steps if s.action == "bio_scan"]
                total_beds = len(bio_steps)
                success_beds = sum(1 for s in bio_steps if s.status == StepStatus.SUCCESS)
                if task.status == TaskStatus.CANCELLED:
                    await send_telegram_message(f"🚫 巡房已取消\n本次巡房 {total_beds} 床，已完成 {success_beds} 床")
                else:
                    await send_telegram_message(f"✅ 巡房完成\n本次巡房 {total_beds} 床，成功讀取 {success_beds} 床")
            except Exception as tg_err:
                logger.error(f"Failed to send task-completion Telegram: {tg_err}")
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

    async def _execute_step(self, step: TaskStep, skip_reason=None) -> StepResult:
        action = step.action
        params = step.params
        try:
            if action == "speak":
                result = await self.fleet.speak(self.robot_id, params["speak_text"])
                return self._make_result(result, action, {"speak_text": params["speak_text"]})

            elif action == "move_to_pose":
                result = await self.fleet.move_to_pose(self.robot_id, float(params["x"]), float(params["y"]), float(params["yaw"]))
                return self._make_result(result, action, {"x": float(params["x"]), "y": float(params["y"]), "yaw": float(params["yaw"])})

            elif action == "move_to_location":
                result = await self.fleet.move_to_location(self.robot_id, params["location_id"])
                return self._make_result(result, action, {"location_id": params["location_id"]})

            elif action == "dock_shelf":
                result = await self.fleet.dock_shelf(self.robot_id)
                return self._make_result(result, action, {})

            elif action == "undock_shelf":
                result = await self.fleet.undock_shelf(self.robot_id)
                return self._make_result(result, action, {})

            elif action == "move_shelf":
                self.target_bed = params["location_id"]

                result = await self.fleet.move_shelf(self.robot_id, params["shelf_id"], params["location_id"])

                # Start shelf monitor after first successful move_shelf
                if result.get("ok") and self._shelf_monitor_task is None:
                    self._current_shelf_id = params["shelf_id"]
                    self._shelf_monitor_stop = False
                    self._shelf_dropped = False
                    self._shelf_monitor_task = asyncio.create_task(self._monitor_shelf())

                return self._make_result(result, action, {"shelf_id": params["shelf_id"], "location_id": params["location_id"]})

            elif action == "return_shelf":
                # Stop shelf monitor before return_shelf — no longer needed
                await self._stop_shelf_monitor()

                result = await self.fleet.return_shelf(self.robot_id, params["shelf_id"])
                return self._make_result(result, action, {"shelf_id": params["shelf_id"]})

            elif action == "return_home":
                result = await self.fleet.return_home(self.robot_id)
                return self._make_result(result, action, {})

            elif action == "bio_scan":
                client = get_bio_sensor_client()
                if client is None:
                    return StepResult(
                        success=False, error_code=-1,
                        error_message="Bio-sensor MQTT client is not available (mqtt_enabled=false)",
                        data={}, timestamp=get_now().isoformat()
                    )
                bed_key = params.get("bed_key")
                outcome = await client.get_valid_scan_data(target_bed=self.target_bed, task_id=self.current_task_id, bed_name=bed_key)
                logger.info(f"Bio scan outcome for robot {self.robot_id}: valid={outcome.valid_record is not None} retry_count={outcome.retry_count}")

                success = outcome.valid_record is not None
                if success:
                    logger.info(f"Bio scan completed successfully for robot {self.robot_id}")
                else:
                    logger.warning(f"Bio scan failed - no valid data obtained for robot {self.robot_id}")

                event = _bio_scan_evaluator.evaluate(outcome)
                if event:
                    await dispatcher.dispatch(event)

                return StepResult(
                    success=success,
                    error_code=0 if success else -1,
                    error_message="Bio scan completed successfully" if success else "No valid data obtained after all retries",
                    data=outcome.valid_record or {"task_id": outcome.task_id, "details": outcome.last_failure_reason},
                    timestamp=get_now().isoformat()
                )

            elif action == "wait":
                seconds = float(params.get("seconds", "1.0"))
                await asyncio.sleep(seconds)
                return StepResult(
                    success=True, error_code=0,
                    error_message="Wait completed successfully",
                    data={"seconds": seconds}, timestamp=get_now().isoformat()
                )

            else:
                logger.error(f"Unknown action: {action} for robot {self.robot_id}")
                return StepResult(
                    success=False, error_code=-1,
                    error_message=f"Unknown action: {action}",
                    data={"action": action}, timestamp=get_now().isoformat()
                )

        except ValueError as e:
            logger.error(f"[!] Robot {self.robot_id} not found: {str(e)}")
            return StepResult(
                success=False, error_code=-1,
                error_message=f"Robot {self.robot_id} not found: {str(e)}",
                data={"action": action, "params": params},
                timestamp=get_now().isoformat()
            )
        except Exception as e:
            logger.error(f"[X] Unexpected error during action {action} for robot {self.robot_id}: {str(e)}", exc_info=True)
            return StepResult(
                success=False, error_code=-1,
                error_message=f"Unexpected error: {str(e)}",
                data={"action": action, "params": params},
                timestamp=get_now().isoformat()
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
