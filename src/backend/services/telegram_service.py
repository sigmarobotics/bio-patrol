"""Telegram notification service.

Sends messages via Telegram Bot API when enabled in settings.
The optional ``chat_id`` parameter lets the new AnomalyDispatcher path target
a specific chat; legacy direct callers omit it and fall back to settings.
"""
import logging

import httpx

from settings.config import get_runtime_settings

logger = logging.getLogger(__name__)


async def send_telegram_message(message: str, chat_id: str | None = None):
    """Send a Telegram message if enabled in runtime settings.

    chat_id: optional override; defaults to settings.telegram_user_id.
    """
    try:
        cfg = get_runtime_settings()

        if not cfg.get("enable_telegram", False):
            logger.debug("Telegram notifications disabled")
            return

        token = cfg.get("telegram_bot_token", "")
        effective_chat_id = chat_id or cfg.get("telegram_user_id", "")

        if not token or not effective_chat_id:
            logger.warning("Telegram enabled but bot_token or chat_id not set")
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": effective_chat_id, "text": message, "parse_mode": "HTML"}

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info("Telegram message sent successfully")
            else:
                logger.warning(f"Telegram API returned {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
