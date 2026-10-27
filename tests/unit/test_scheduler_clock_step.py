"""A wall-clock step must re-arm the scheduler, and a dropped slot must page.

AsyncIOScheduler sleeps on monotonic time. On 2026-10-24 the 新營 Pi booted
with its clock 38 h behind; after NTP fixed it the timer still pointed at the
stale 11:59 slot and the 23:00 patrol never fired.
"""
import asyncio
from datetime import datetime, timedelta

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import services.scheduler as sched
from services.notifications import Source, dispatcher


async def _noop():
    pass


def _realign(monkeypatch, next_run_from_now, slot_from_now, step):
    """One daily job whose next_run_time was computed under a wrong clock;
    realign it on a real scheduler, return (next_run_time, slot, notices)."""
    notices = []

    async def _fake_dispatch(event):
        notices.append(event)

    monkeypatch.setattr(dispatcher, "dispatch", _fake_dispatch)

    async def run():
        svc = sched.TaskSchedulerService()
        svc.scheduler.start()
        now = datetime.now(svc.scheduler.timezone).replace(microsecond=0)
        slot = now + slot_from_now
        job = svc.scheduler.add_job(
            _noop, CronTrigger(hour=slot.hour, minute=slot.minute, second=slot.second,
                               timezone=svc.scheduler.timezone),
            id="patrol_night", misfire_grace_time=sched.MISFIRE_GRACE_SECONDS)
        job.modify(next_run_time=now + next_run_from_now)
        await asyncio.sleep(0.05)
        svc._realign_after_clock_step(step)
        await asyncio.sleep(0.05)
        fresh = svc.scheduler.get_job("patrol_night").next_run_time
        svc.scheduler.shutdown(wait=False)
        return fresh, slot

    fresh, slot = asyncio.run(run())
    return fresh, slot, notices


def test_forward_step_moves_on_and_pages_recent_miss(monkeypatch):
    # Clock was 38 h behind: run times now-38h (stale) and now-23h went by.
    fresh, slot, notices = _realign(monkeypatch, -timedelta(hours=38), timedelta(hours=1), 38 * 3600.0)
    assert fresh == slot
    assert [e.source for e in notices] == [Source.SCHEDULE_NOT_RUN]
    assert notices[0].title == "⚠️ 排程巡房已錯過"


def test_backward_step_pulls_tonight_back(monkeypatch):
    # Clock was 2 h ahead: tonight's slot looked past, job pushed to tomorrow.
    fresh, slot, notices = _realign(monkeypatch, timedelta(hours=23), timedelta(hours=1), -7200.0)
    assert fresh == slot
    assert notices == []


def test_watchdog_reacts_only_to_steps_and_survives_errors(monkeypatch):
    values = [0.5, 1.0, 3600.0, 3600.0, 7200.0]
    steps = []

    def _fake_realign(step):
        steps.append(step)
        raise RuntimeError("boom")

    async def run():
        svc = sched.TaskSchedulerService()
        monkeypatch.setattr(svc, "_realign_after_clock_step", _fake_realign)
        task = asyncio.create_task(svc._watch_clock_steps(0.0))
        while values:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
        task.cancel()

    monkeypatch.setattr(sched, "_clock_offset", lambda: values.pop(0) if values else 7200.0)
    monkeypatch.setattr(sched, "CLOCK_CHECK_SECONDS", 0)
    asyncio.run(run())
    assert steps == [3599.0, 3600.0]


def test_backward_step_pulls_pending_sensor_retry_back():
    # Retry promised "10 分鐘後" under a clock 2 h fast: its absolute run time
    # sits 2 h 10 min ahead once NTP steps back, so it must move with the step.
    async def run():
        svc = sched.TaskSchedulerService()
        svc.scheduler.start()
        now = datetime.now(svc.scheduler.timezone).replace(microsecond=0)
        svc.scheduler.add_job(
            _noop, DateTrigger(run_date=now + timedelta(hours=2, minutes=10)),
            id="patrol_night_retry", misfire_grace_time=sched.MISFIRE_GRACE_SECONDS)
        await asyncio.sleep(0.05)
        svc._realign_after_clock_step(-7200.0)
        fresh = svc.scheduler.get_job("patrol_night_retry").next_run_time
        svc.scheduler.shutdown(wait=False)
        return fresh, now

    fresh, now = asyncio.run(run())
    assert fresh == now + timedelta(minutes=10)
