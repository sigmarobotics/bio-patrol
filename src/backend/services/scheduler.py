"""
Scheduler Service
Manages recurring patrol tasks using APScheduler, driven by schedule.json.
"""
import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from services.bio_sensor_mqtt import SENSOR_OFFLINE_PREFIX, SENSOR_SILENT_PREFIX

logger = logging.getLogger(__name__)

# APScheduler defaults to 1 s: a job whose run time slips past that — an event
# loop stalled on a blocking call, a clock step after boot — is dropped without
# executing. For a twice-daily patrol that window is far too tight; 15 min
# tolerates any realistic delay while still keeping the patrol near its slot.
MISFIRE_GRACE_SECONDS = 900

# AsyncIOScheduler arms its next wakeup with loop.call_later — monotonic time —
# computed from the wall clock at that moment. The Pi has no RTC battery: it
# boots on the last saved timestamp and NTP steps the clock once the network is
# up (新營 2026-10-24: +38 h), leaving the timer aimed at a stale slot so every
# patrol until then is silently skipped. Cron jobs computed under the wrong
# clock also keep a wrong next_run_time (a backward step would push tonight's
# slot to tomorrow), so on a step every job is recomputed, not just re-armed.
CLOCK_CHECK_SECONDS = 30
CLOCK_STEP_TOLERANCE_SECONDS = 60


def _clock_offset() -> float:
    """Wall clock minus monotonic clock; constant unless the wall clock steps."""
    return time.time() - time.monotonic()

# start_patrol 的拒絕理由是英文 detail（給 API 呼叫者看的），通報是給現場人員看的，
# 所以已知的兩種拒絕各給一句繁中；認不出來的就原樣帶出 detail。
# 感測器類拒絕（IT-23）的 detail 本來就是繁中，原樣帶出，並觸發排程重試。
_SENSOR_REFUSAL_PREFIXES = (SENSOR_SILENT_PREFIX, SENSOR_OFFLINE_PREFIX)
_BATTERY_REFUSAL_PREFIX = "Battery too low to start patrol: "
_REFUSAL_ZH = {
    "No enabled beds in patrol config": "巡邏設定中沒有啟用的床位",
}


def _refusal_reason(detail: str) -> str:
    """把 start_patrol 的 HTTPException detail 轉成一句繁中原因。"""
    if detail.startswith(_SENSOR_REFUSAL_PREFIXES):
        return detail
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
        self._clock_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the scheduler and load schedules from JSON."""
        if not self.is_running:
            # Baseline before any job is computed: a step during reload's
            # awaits must still be seen as a step.
            offset = _clock_offset()
            self.scheduler.start()
            self.is_running = True
            self._clock_task = asyncio.create_task(self._watch_clock_steps(offset))
            logger.info("Task scheduler started")
            await self.reload_from_json()

    async def stop(self):
        """Stop the scheduler."""
        if self.is_running:
            if self._clock_task:
                self._clock_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._clock_task
                self._clock_task = None
            self.scheduler.shutdown()
            self.is_running = False
            logger.info("Task scheduler stopped")

    async def _watch_clock_steps(self, offset: float):
        """Realign jobs whenever the wall clock steps (see CLOCK_CHECK_SECONDS)."""
        while True:
            await asyncio.sleep(CLOCK_CHECK_SECONDS)
            new_offset = _clock_offset()
            step = new_offset - offset
            offset = new_offset
            if abs(step) > CLOCK_STEP_TOLERANCE_SECONDS:
                logger.warning(f"Wall clock stepped by {step:+.0f}s, realigning scheduler jobs")
                await self._realign_after_clock_step(step)

    async def _realign_after_clock_step(self, step: float):
        """Recompute every job's next run from the corrected clock, then re-arm.

        Cron slots that passed while the clock was wrong (or the Pi was off)
        beyond the misfire grace are reported, not run late; a slot still
        inside the grace runs now. Sensor-retry DateTriggers keep their delay.
        """
        now = datetime.now(self.scheduler.timezone)
        grace = timedelta(seconds=MISFIRE_GRACE_SECONDS)
        for job in self.scheduler.get_jobs():
            if job.next_run_time is None:
                continue
            if isinstance(job.trigger, DateTrigger):
                job.modify(next_run_time=job.next_run_time + timedelta(seconds=step))
                continue
            fresh = job.trigger.get_next_fire_time(None, now - grace)
            if fresh <= now and job.next_run_time > fresh:
                # That slot already ran before the step; don't run it twice.
                fresh = job.trigger.get_next_fire_time(None, now)
            if job.id.startswith("patrol_") and job.next_run_time < now - grace:
                schedule_id = job.id[len("patrol_"):]
                missed = job.next_run_time.strftime("%m/%d %H:%M")
                logger.warning(f"Scheduled patrol '{schedule_id}' slot {missed} missed during clock step")
                await notify_schedule_issue(
                    "⚠️ 排程巡房已錯過",
                    f"排程時間：{missed}\n原因：主機時鐘剛校正（多半是剛開機），"
                    f"該時段已過，如仍需巡房請手動啟動",
                    raw={"schedule_id": schedule_id, "missed": job.next_run_time.isoformat()},
                )
            job.modify(next_run_time=fresh)
        self.scheduler.wakeup()

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
                if job.id.endswith("_retry"):
                    # 通報過「N 分鐘後重試」，取消了就要講，不然之後沒下文。
                    schedule_id = job.id[len("patrol_"):-len("_retry")]
                    logger.warning(f"Schedule reload cancelled pending sensor retry for '{schedule_id}'")
                    await notify_schedule_issue(
                        "⚠️ 排程已重新載入，今晚待重試的巡房已取消",
                        f"排程「{schedule_id}」原本等待感測器恢復後重試，如仍需巡房請手動啟動",
                        raw={"schedule_id": schedule_id},
                    )

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

    async def _sensor_refused(self, schedule_id: str, attempt: int, reason: str):
        """IT-23：感測墊沒回應（多半是行動電源沒電）就隔 N 分鐘再試，用罄才放棄今晚。
        重試 job 叫 patrol_<sid>_retry，reload_from_json 的 patrol_ 清除會一併帶走。"""
        from settings.config import get_runtime_settings

        cfg = get_runtime_settings()
        retry_count = int(cfg.get("sensor_precheck_retry_count", 3) or 0)
        interval = max(1, int(cfg.get("sensor_precheck_retry_minutes", 10) or 0))
        when = self._schedule_time(schedule_id)
        raw = {"schedule_id": schedule_id, "reason": reason, "attempt": attempt}

        if attempt < retry_count:
            await notify_schedule_issue(
                "🔌 感測器無回應，排程巡房未啟動",
                f"排程時間：{when}\n原因：{reason}\n"
                f"{interval} 分鐘後第 {attempt + 1}/{retry_count} 次重試",
                raw=raw,
            )
            self.scheduler.add_job(
                self._run_patrol,
                DateTrigger(run_date=datetime.now() + timedelta(minutes=interval)),
                id=f"patrol_{schedule_id}_retry",
                args=[schedule_id, attempt + 1],
                replace_existing=True,
                misfire_grace_time=MISFIRE_GRACE_SECONDS,
            )
            logger.info(
                f"Scheduled patrol '{schedule_id}' retry {attempt + 1}/{retry_count} "
                f"in {interval} min"
            )
        else:
            await notify_schedule_issue(
                "🔌 感測器仍無回應，今晚放棄巡房",
                f"排程時間：{when}\n原因：{reason}\n已重試 {attempt} 次",
                raw=raw,
            )

    async def _run_patrol(self, schedule_id: str, attempt: int = 0):
        """Execute a scheduled patrol through the same entry point as a manual
        start, so the unattended night run gets the duplicate-run dedup, the
        low-battery gate and the robot-offline gate instead of queueing a
        second route behind a live one. `attempt` > 0 is an IT-23 sensor retry."""
        logger.info(f"Scheduled patrol triggered: {schedule_id} (attempt {attempt})")
        try:
            from fastapi import HTTPException
            from routers.patrol import PatrolStartRequest, start_patrol

            if attempt > 0:
                # Someone already started a patrol by hand since the refusal —
                # that run is the night's patrol; this retry has nothing to do.
                from common_types import TaskStatus
                from services.task_runtime import tasks_db
                for t in tasks_db.values():
                    if (t.status in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS)
                            and (t.metadata or {}).get("mode") == "patrol"):
                        logger.info(
                            f"Scheduled patrol '{schedule_id}' retry dropped: "
                            f"task {t.task_id} is already {t.status.value}"
                        )
                        return

            try:
                res = await start_patrol(PatrolStartRequest(mode="patrol"))
            except HTTPException as e:
                # No enabled beds / battery too low — expected refusals, not bugs
                logger.warning(f"Scheduled patrol '{schedule_id}' not started: {e.detail}")
                reason = _refusal_reason(str(e.detail))
                if str(e.detail).startswith(_SENSOR_REFUSAL_PREFIXES):
                    await self._sensor_refused(schedule_id, attempt, reason)
                else:
                    await self._notify_not_started(schedule_id, reason)
                return

            if res.get("status") == "already_running":
                if attempt > 0 and res.get("mode") == "patrol":
                    # A manual patrol won the start lock after the entry guard
                    # ran — same silent drop as above. Any other live run
                    # (cleanup, demo, resume) still ends the retry chain, so
                    # it is reported like a first-attempt refusal.
                    logger.info(
                        f"Scheduled patrol '{schedule_id}' retry dropped: task "
                        f"{res.get('task_id')} is already running"
                    )
                    return
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
