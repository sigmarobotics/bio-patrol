"""LineSink — formats AnomalyEvent as plain text and pushes via line_service."""
from __future__ import annotations

import asyncio

from services.line_service import send_line_message
from services.notifications.events import AnomalyEvent
from services.notifications.recipients import RecipientResolver
from settings.config import get_runtime_settings


class LineSink:
    def __init__(self, resolver: RecipientResolver):
        self._resolver = resolver

    async def is_enabled(self) -> bool:
        return bool(get_runtime_settings().get("enable_line", False))

    async def send(self, event: AnomalyEvent) -> None:
        targets = await self._resolver.resolve(event, channel="line")
        if not targets:
            return
        message = self._format(event)
        await asyncio.gather(
            *(send_line_message(message, to=t) for t in targets),
            return_exceptions=True,
        )

    def _format(self, event: AnomalyEvent) -> str:
        # LINE text messages carry no HTML — plain text, same format as Telegram.
        body_block = f"\n\n{event.body}" if event.body else ""
        return f"{event.title}{body_block}"
