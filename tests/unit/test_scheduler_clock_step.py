"""A wall-clock step must realign every scheduled patrol.

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


def _realign(monkeypatch, next_run_from_now, slot_from_now, trigger=None, step=3600.0):
    """Start a real scheduler with one job whose next_run_time was computed under a
    wrong clock, realign it, return (fresh next_run_time, slot, now, notices)."""
    notices = []

    async def _fake_dispatch(event):
        notices.append(event)

    monkeypatch.setattr(dispatcher, "dispatch", _fake_dispatch)

    async def run():
        svc = sched.TaskSchedulerService()
        svc.scheduler.start(paused=True)
        now = datetime.now(svc.scheduler.timezone).replace(microsecond=0)
        slot = now + slot_from_now
        trig = trigger or CronTrigger(hour=slot.hour, minute=slot.minute, second=slot.second,
                                      timezone=svc.scheduler.timezone)
        job = svc.scheduler.add_job(_noop, trig, id="patrol_night")
        job.modify(next_run_time=now + next_run_from_now)
        await svc._realign_after_clock_step(step)
        fresh = svc.scheduler.get_job("patrol_night").next_run_time
        svc.scheduler.shutdown(wait=False)
        return fresh, slot, now

    fresh, slot, now = asyncio.run(run())
    return fresh, slot, now, notices


def test_forward_step_reports_missed_slot_and_moves_on(monkeypatch):
    # Clock was 38 h behind: job still waits for a slot long gone.
    fresh, slot, now, notices = _realign(monkeypatch, -timedelta(hours=38), timedelta(hours=1))
    assert fresh == slot
    assert [e.source for e in notices] == [Source.SCHEDULE_NOT_RUN]
    assert notices[0].title == "⚠️ 排程巡房已錯過"


def test_backward_step_pulls_tonight_back(monkeypatch):
    # Clock was 2 h ahead: tonight's slot looked past, job pushed to tomorrow.
    fresh, slot, now, notices = _realign(monkeypatch, timedelta(hours=23), timedelta(hours=1), step=-7200.0)
    assert fresh == slot
    assert notices == []


def test_slot_inside_grace_still_runs(monkeypatch):
    fresh, slot, now, notices = _realign(monkeypatch, -timedelta(minutes=5), -timedelta(minutes=5))
    assert fresh == slot and fresh <= now
    assert notices == []


def test_slot_that_already_ran_is_not_repeated(monkeypatch):
    fresh, slot, now, notices = _realign(monkeypatch, timedelta(hours=24) - timedelta(minutes=5),
                                         -timedelta(minutes=5))
    assert fresh == slot + timedelta(days=1)
    assert notices == []


def test_sensor_retry_keeps_its_delay(monkeypatch):
    run_date = datetime.now() + timedelta(minutes=10)
    fresh, _, now, notices = _realign(monkeypatch, -timedelta(hours=38) + timedelta(minutes=10),
                                      timedelta(0), trigger=DateTrigger(run_date=run_date),
                                      step=38 * 3600.0)
    assert timedelta(minutes=9) < fresh - now <= timedelta(minutes=10)
    assert notices == []


def test_watchdog_only_reacts_to_steps(monkeypatch):
    values = [0.5, 1.0, 3600.0, 3600.0]
    steps = []

    async def _fake_realign(step):
        steps.append(step)

    async def run():
        svc = sched.TaskSchedulerService()
        monkeypatch.setattr(svc, "_realign_after_clock_step", _fake_realign)
        task = asyncio.create_task(svc._watch_clock_steps(0.0))
        while values:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)
        task.cancel()

    monkeypatch.setattr(sched, "_clock_offset", lambda: values.pop(0) if values else 3600.0)
    monkeypatch.setattr(sched, "CLOCK_CHECK_SECONDS", 0)
    asyncio.run(run())
    assert steps == [3599.0]
