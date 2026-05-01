"""AnomalyDispatcher — fan-out to registered sinks with per-sink isolation."""
from __future__ import annotations

import asyncio
import logging

from services.notifications.events import AnomalyEvent
from services.notifications.sinks import Sink

logger = logging.getLogger("services.notifications.dispatcher")


class AnomalyDispatcher:
    def __init__(self):
        self._sinks: list[Sink] = []
        self._pending: set[asyncio.Task] = set()

    def register(self, sink: Sink) -> None:
        self._sinks.append(sink)

    async def dispatch(self, event: AnomalyEvent) -> None:
        for sink in self._sinks:
            task = asyncio.create_task(self._safe_send(sink, event))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def drain(self, timeout: float) -> None:
        """Wait up to `timeout` seconds for all in-flight sends to complete."""
        pending = list(self._pending)
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Notification dispatcher drain timed out; %d tasks abandoned",
                len(pending),
            )

    async def _safe_send(self, sink: Sink, event: AnomalyEvent) -> None:
        try:
            if not await sink.is_enabled():
                return
            await sink.send(event)
        except Exception:
            logger.exception("Sink %s failed", sink.__class__.__name__)


# Module-level singleton consumed by main.py + producers.
dispatcher = AnomalyDispatcher()
