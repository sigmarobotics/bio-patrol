"""DemoPreviewSink — keeps demo events for the /demo panel's notification
preview (IT-21). Stores the title exactly as TelegramSink renders it, so the
panel shows what Telegram received. Non-demo events are ignored."""
from __future__ import annotations

import asyncio

from services import demo_data
from services.notifications.events import AnomalyEvent


class DemoPreviewSink:
    async def is_enabled(self) -> bool:
        return True

    async def send(self, event: AnomalyEvent) -> None:
        if not event.demo:
            return
        await asyncio.to_thread(demo_data.save_notification, event)
