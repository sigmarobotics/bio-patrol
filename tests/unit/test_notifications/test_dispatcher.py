"""Unit tests for AnomalyDispatcher: per-sink isolation, pending tracking, drain."""
from __future__ import annotations

import asyncio

from services.notifications.dispatcher import AnomalyDispatcher
from services.notifications.events import AnomalyEvent


class _RecorderSink:
    def __init__(self, enabled: bool = True, raise_in_send: bool = False, sleep: float = 0.0):
        self.enabled = enabled
        self.raise_in_send = raise_in_send
        self.sleep = sleep
        self.received: list[AnomalyEvent] = []

    async def is_enabled(self) -> bool:
        return self.enabled

    async def send(self, event: AnomalyEvent) -> None:
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.raise_in_send:
            raise RuntimeError("boom")
        self.received.append(event)


async def _run_with_drain(dispatcher: AnomalyDispatcher, events: list[AnomalyEvent]):
    for ev in events:
        await dispatcher.dispatch(ev)
    await dispatcher.drain(timeout=2.0)


def test_dispatch_with_no_sinks_is_noop():
    d = AnomalyDispatcher()
    asyncio.run(_run_with_drain(d, [AnomalyEvent()]))
    # no exception, nothing to assert beyond the lack of failure


def test_dispatch_fans_out_to_every_enabled_sink():
    d = AnomalyDispatcher()
    a, b = _RecorderSink(), _RecorderSink()
    d.register(a); d.register(b)
    e = AnomalyEvent()
    asyncio.run(_run_with_drain(d, [e]))
    assert a.received == [e]
    assert b.received == [e]


def test_disabled_sink_does_not_get_send():
    d = AnomalyDispatcher()
    on, off = _RecorderSink(enabled=True), _RecorderSink(enabled=False)
    d.register(on); d.register(off)
    e = AnomalyEvent()
    asyncio.run(_run_with_drain(d, [e]))
    assert on.received == [e]
    assert off.received == []


def test_one_sink_failing_does_not_block_others():
    d = AnomalyDispatcher()
    bad, good = _RecorderSink(raise_in_send=True), _RecorderSink()
    d.register(bad); d.register(good)
    e = AnomalyEvent()
    asyncio.run(_run_with_drain(d, [e]))
    assert good.received == [e]


def test_dispatch_does_not_await_send_completion():
    """dispatch() returns before slow sinks finish — drain() is what awaits them."""
    d = AnomalyDispatcher()
    slow = _RecorderSink(sleep=0.2)
    d.register(slow)
    e = AnomalyEvent()

    async def run():
        await d.dispatch(e)
        # at this point the send is still sleeping
        assert slow.received == []
        await d.drain(timeout=1.0)
        assert slow.received == [e]

    asyncio.run(run())


def test_drain_returns_quickly_when_no_pending():
    d = AnomalyDispatcher()
    asyncio.run(d.drain(timeout=0.1))
    # no exception
