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

# start_patrol 的拒絕理由是英文 detail（給 API 呼叫者看的），通報是給現場人員看的，
# 所以已知的兩種拒絕各給一句繁中；認不出來的就原樣帶出 detail。
_BATTERY_REFUSAL_PREFIX = "Battery too low to start patrol: "
_REFUSAL_ZH = {
    "No enabled beds in patrol config": "巡邏設定中沒有啟用的床位",
}


def _refusal_reason(detail: str) -> str:
    """把 start_patrol 的 HTTPException detail 轉成一句繁中原因。"""
    if detail.startswith(_BATTERY_REFUSAL_PREFIX):
        pct, _, threshold = detail[len(_BATTERY_REFUSAL_PREFIX):].partition(" < ")
        if threshold:
            return f"電量 {pct} 低於門檻 {threshold}"
    return _REFUSAL_ZH.get(detail, detail)


async def notify_schedule_issue(title: str, body: str, raw: Optional[dict] = None) -> None:
    """排程「今晚不會跑」的任何一種形狀都推一則通報。

    走 dispatcher 而不是直接呼叫 send_telegram_message：只設定 LINE 的站點
    （enable_telegram=False）用 telegram 送等於完全靜默的 no-op，而這正是最不能
    漏掉的一類訊息。title/body 由各 sink 自行跳脫，這裡送純文字即可。
    通報失敗不能拖垮呼叫端（排程執行、設定存檔）。
    """
    from services.notifications import AnomalyEvent, Severity, Source, dispatcher

    try:
        await dispatcher.dispatch(AnomalyEvent(
            severity=Severity.WARN,
            source=Source.SCHEDULE_NOT_RUN,
            title=title,
            body=body,
            raw=raw or {},
        ))
    except Exception:
        logger.exception("Failed to dispatch schedule notice")


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
                # 啟用中卻建不出 job：UI 上看起來排好了，到點卻什麼都不會發生。
                logger.warning(f"Invalid time format for schedule '{schedule_id}': {time_str}")
                await notify_schedule_issue(
                    "⚠️ 排程設定有誤，不會執行",
                    f"排程「{schedule_id}」的時間格式無法解析：{time_str}",
                    raw={"schedule_id": schedule_id, "time": time_str},
                )
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

    def _schedule_time(self, schedule_id: str) -> str:
        """設定檔裡這個排程的 HH:MM；查不到就用現在時間（通知一定要帶時間）。"""
        from settings.config import SCHEDULE_FILE
        from settings.defaults import DEFAULT_SCHEDULE
        from utils.json_io import load_json

        data = load_json(SCHEDULE_FILE, DEFAULT_SCHEDULE)
        for entry in data.get("schedules", []):
            if entry.get("id") == schedule_id and entry.get("time"):
                return entry["time"]
        return datetime.now().strftime("%H:%M")

    async def _notify_not_started(self, schedule_id: str, reason: str):
        """排程沒開跑就推一則通報 —— 夜間沒人在看 log，只寫 log 等於沒發生。"""
        await notify_schedule_issue(
            "⚠️ 排程巡房未啟動",
            f"排程時間：{self._schedule_time(schedule_id)}\n原因：{reason}",
            raw={"schedule_id": schedule_id, "reason": reason},
        )

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
                await self._notify_not_started(schedule_id, _refusal_reason(str(e.detail)))
                return

            if res.get("status") == "already_running":
                logger.warning(
                    f"Scheduled patrol '{schedule_id}' skipped: task "
                    f"{res.get('task_id')} is already running"
                )
                await self._notify_not_started(
                    schedule_id, f"已有巡房任務執行中（{res.get('task_id')}）"
                )
                return

            logger.info(
                f"Scheduled patrol '{schedule_id}' created task {res.get('task_id')} "
                f"with {res.get('beds_count')} beds"
            )

        except Exception as e:
            logger.error(f"Error executing scheduled patrol '{schedule_id}': {e}", exc_info=True)
            await self._notify_not_started(schedule_id, f"未預期錯誤：{e}")


# Global scheduler instance
scheduler_service = TaskSchedulerService()
