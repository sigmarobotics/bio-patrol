"""TelegramSink — formats AnomalyEvent as HTML and posts via telegram_service."""
from __future__ import annotations

import asyncio
import html

from services.notifications.events import AnomalyEvent, display_title
from services.notifications.recipients import RecipientResolver
from services.telegram_service import send_telegram_message
from settings.config import get_runtime_settings


class TelegramSink:
    def __init__(self, resolver: RecipientResolver):
        self._resolver = resolver

    async def is_enabled(self) -> bool:
        return bool(get_runtime_settings().get("enable_telegram", False))

    async def send(self, event: AnomalyEvent) -> None:
        if event.demo:
            await self._send_demo(event)
            return
        chat_ids = await self._resolver.resolve(event, channel="telegram")
        if not chat_ids:
            return
        message = self._format(event)
        await asyncio.gather(
            *(send_telegram_message(message, chat_id=cid) for cid in chat_ids),
            return_exceptions=True,
        )

    async def _send_demo(self, event: AnomalyEvent) -> None:
        """IT-21: a demo event may go to its own bot/chat. Empty settings fall
        back to the real bot and recipients; the demo chat is checked first so
        a site without telegram_user_id still reaches it."""
        cfg = get_runtime_settings()
        demo_chat = cfg.get("demo_telegram_chat_id") or ""
        chat_ids = [demo_chat] if demo_chat else await self._resolver.resolve(
            event, channel="telegram")
        if not chat_ids:
            return
        bot_token = cfg.get("demo_telegram_bot_token") or None
        message = self._format(event)
        await asyncio.gather(
            *(send_telegram_message(message, chat_id=cid, bot_token=bot_token)
              for cid in chat_ids),
            return_exceptions=True,
        )

    def _format(self, event: AnomalyEvent) -> str:
        # Bed/location names from user config flow into these strings; escape
        # them or a name containing &/</> kills the whole HTML-mode message.
        body_block = f"\n\n{html.escape(event.body)}" if event.body else ""
        return (f"<b>{html.escape(display_title(event))}</b>{body_block}"
                f"{self._format_readings(event.readings)}")

    @staticmethod
    def _format_readings(readings: dict | None) -> str:
        """IT-22: per-bed vitals in a collapsed-by-default quote, so the run
        summary stays short until someone taps it open."""
        valid = (readings or {}).get("valid") or []
        restless = (readings or {}).get("restless") or []
        if not valid and not restless:
            return ""

        def line(bed, bpm, rpm):
            if not bpm and not rpm:
                return f"{bed}　無讀值"
            return f"{bed}　心跳 {bpm}　呼吸 {rpm}"

        lines = [line(*r) for r in valid]
        if restless:
            lines.append("── 躁動 ──")
            lines += [line(*r) for r in restless]
        quoted = html.escape("\n".join(lines))
        return f"\n\n<b>量測數值</b>（點開展開）\n<blockquote expandable>{quoted}</blockquote>"
