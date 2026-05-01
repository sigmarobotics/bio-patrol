"""TelegramSink — formats AnomalyEvent as HTML and posts via telegram_service."""
from __future__ import annotations

from services.notifications.events import AnomalyEvent
from services.notifications.recipients import RecipientResolver
from services.telegram_service import send_telegram_message
from settings.config import get_runtime_settings


class TelegramSink:
    def __init__(self, resolver: RecipientResolver):
        self._resolver = resolver

    async def is_enabled(self) -> bool:
        return bool(get_runtime_settings().get("enable_telegram", False))

    async def send(self, event: AnomalyEvent) -> None:
        chat_ids = await self._resolver.resolve(event, channel="telegram")
        if not chat_ids:
            return
        message = self._format(event)
        for chat_id in chat_ids:
            await send_telegram_message(message, chat_id=chat_id)

    def _format(self, event: AnomalyEvent) -> str:
        # event_id last-8 footer lets a recipient cross-reference MQTT logs
        return f"<b>{event.title}</b>\n\n{event.body}\n\n<code>{event.event_id[-8:]}</code>"
