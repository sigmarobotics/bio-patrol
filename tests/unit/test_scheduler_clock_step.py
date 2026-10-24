"""A wall-clock step must re-arm the APScheduler timer.

AsyncIOScheduler sleeps on monotonic time. On 2026-10-24 the 新營 Pi booted
with its clock 38 h behind; after NTP fixed it the timer still pointed at the
stale 11:59 slot and the 23:00 patrol never fired.
"""
import asyncio

import services.scheduler as sched


def _watch(monkeypatch, offsets):
    """Run the watchdog over a sequence of clock offsets; return wakeup count."""
    values = list(offsets)
    monkeypatch.setattr(sched, "_clock_offset", lambda: values.pop(0) if len(values) > 1 else values[0])
    monkeypatch.setattr(sched, "CLOCK_CHECK_SECONDS", 0)
    svc = sched.TaskSchedulerService()
    calls = []
    monkeypatch.setattr(svc.scheduler, "wakeup", lambda: calls.append(1))

    async def run():
        task = asyncio.create_task(svc._watch_clock_steps())
        for _ in range(len(offsets) + 5):
            await asyncio.sleep(0)
        task.cancel()

    asyncio.run(run())
    return len(calls)


def test_forward_step_rearms_once(monkeypatch):
    assert _watch(monkeypatch, [0.0, 0.0, 38 * 3600.0, 38 * 3600.0]) == 1


def test_backward_step_rearms(monkeypatch):
    assert _watch(monkeypatch, [0.0, -3600.0, -3600.0]) == 1


def test_ntp_slew_is_ignored(monkeypatch):
    assert _watch(monkeypatch, [0.0, 0.5, 1.0, 1.5]) == 0
