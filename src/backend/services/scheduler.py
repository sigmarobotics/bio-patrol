"""
Scheduler Service
Manages recurring patrol tasks using APScheduler, driven by schedule.json.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# APScheduler defaults to 1 s: a job whose run time slips past that — an event
# loop stalled on a blocking call, a clock step after boot — is dropped without
# executing. For a twice-daily patrol that window is far too tight; 15 min
# tolerates any realistic delay while still keeping the patrol near its slot.
MISFIRE_GRACE_SECONDS = 900


class TaskSchedulerService:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.is_running = False

    async def start(self):
        """Start the scheduler and load schedules from JSON."""
        if not self.is_running:
            self.scheduler.start()
            self.is_running = True
            logger.info("Task scheduler started")
            await self.reload_from_json()

    async def stop(self):
        """Stop the scheduler."""
        if self.is_running:
            self.scheduler.shutdown()
            self.is_running = False
            logger.info("Task scheduler stopped")

    async def reload_from_json(self):
        """
        Read schedule.json and sync APScheduler jobs.
        Removes all existing patrol jobs, then re-creates jobs for enabled entries.
        """
        from settings.config import SCHEDULE_FILE
        from settings.defaults import DEFAULT_SCHEDULE
        from utils.json_io import load_json

        data = load_json(SCHEDULE_FILE, DEFAULT_SCHEDULE)
        schedules = data.get("schedules", [])

        # Remove all existing patrol-schedule jobs
        existing_jobs = self.scheduler.get_jobs()
        for job in existing_jobs:
            if job.id.startswith("patrol_"):
                self.scheduler.remove_job(job.id)
                logger.info(f"Removed scheduler job: {job.id}")

        # Add jobs for each enabled schedule
        added = 0
        for entry in schedules:
            schedule_id = entry.get("id", "")
            enabled = entry.get("enabled", False)
            time_str = entry.get("time", "")
            schedule_type = entry.get("type", "daily")

            if not enabled or not time_str or not schedule_id:
                continue

            try:
                hour, minute = map(int, time_str.split(":"))
            except (ValueError, TypeError):
                logger.warning(f"Invalid time format for schedule '{schedule_id}': {time_str}")
                continue

            job_id = f"patrol_{schedule_id}"

            if schedule_type == "daily":
                self.scheduler.add_job(
                    func=self._run_patrol,
                    trigger=CronTrigger(hour=hour, minute=minute),
                    id=job_id,
                    args=[schedule_id],
                    replace_existing=True,
                    misfire_grace_time=MISFIRE_GRACE_SECONDS,
                )
            elif schedule_type == "weekday":
                self.scheduler.add_job(
                    func=self._run_patrol,
                    trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
                    id=job_id,
                    args=[schedule_id],
                    replace_existing=True,
                    misfire_grace_time=MISFIRE_GRACE_SECONDS,
                )
            else:
                # Treat as daily fallback
                self.scheduler.add_job(
                    func=self._run_patrol,
                    trigger=CronTrigger(hour=hour, minute=minute),
                    id=job_id,
                    args=[schedule_id],
                    replace_existing=True,
                    misfire_grace_time=MISFIRE_GRACE_SECONDS,
                )

            added += 1
            logger.info(f"Scheduled patrol '{schedule_id}' at {time_str} ({schedule_type})")

        logger.info(f"Schedule reload complete: {added} active schedule(s) from {len(schedules)} total")

    async def _run_patrol(self, schedule_id: str):
        """Execute a scheduled patrol through the same entry point as a manual
        start, so the unattended night run gets the duplicate-run dedup and the
        low-battery gate instead of queueing a second route behind a live one."""
        logger.info(f"Scheduled patrol triggered: {schedule_id}")
        try:
            from fastapi import HTTPException
            from routers.patrol import PatrolStartRequest, start_patrol

            try:
                res = await start_patrol(PatrolStartRequest(mode="patrol"))
            except HTTPException as e:
                # No enabled beds / battery too low — expected refusals, not bugs
                logger.warning(f"Scheduled patrol '{schedule_id}' not started: {e.detail}")
                return

            if res.get("status") == "already_running":
                logger.warning(
                    f"Scheduled patrol '{schedule_id}' skipped: task "
                    f"{res.get('task_id')} is already running"
                )
                return

            logger.info(
                f"Scheduled patrol '{schedule_id}' created task {res.get('task_id')} "
                f"with {res.get('beds_count')} beds"
            )

        except Exception as e:
            logger.error(f"Error executing scheduled patrol '{schedule_id}': {e}", exc_info=True)


# Global scheduler instance
scheduler_service = TaskSchedulerService()
