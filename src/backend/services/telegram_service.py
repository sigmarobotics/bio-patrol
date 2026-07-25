"""Telegram notification service.

Sends messages via Telegram Bot API when enabled in settings.
The optional ``chat_id`` parameter lets the new AnomalyDispatcher path target
a specific chat; legacy direct callers omit it and fall back to settings.

A single module-level ``httpx.AsyncClient`` is reused across calls so
connection pool + TLS handshake survive between sends. Lifespan calls
``aclose_client`` on shutdown.
"""
import logging
from typing import Optional

import httpx

from settings.config import get_runtime_settings

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 10.0
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
    return _client


async def aclose_client() -> None:
    """Close the shared HTTP client. Called from lifespan shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def send_telegram_message(message: str, chat_id: str | None = None):
    """Send a Telegram message if enabled in runtime settings.

    chat_id: optional override; defaults to settings.telegram_user_id.
    When notify_hub_url + notify_hub_token are both set, the message is
    relayed through the cloud hub's /api/notify instead of calling the
    Telegram API directly — the hub owns the bot token.
    """
    try:
        cfg = get_runtime_settings()

        if not cfg.get("enable_telegram", False):
            logger.debug("Telegram notifications disabled")
            return

        effective_chat_id = chat_id or cfg.get("telegram_user_id", "")

        hub_url = (cfg.get("notify_hub_url") or "").rstrip("/")
        hub_token = cfg.get("notify_hub_token") or ""
        if hub_url and hub_token:
            payload = {"text": message, "source": "bio-patrol", "parse_mode": "HTML"}
            if effective_chat_id:
                payload["chat_id"] = str(effective_chat_id)
            resp = await _get_client().post(
                f"{hub_url}/api/notify",
                json=payload,
                headers={"Authorization": f"Bearer {hub_token}"},
            )
            if resp.status_code == 200:
                logger.info("Notification relayed via hub")
            else:
                logger.warning(f"Hub notify returned {resp.status_code}: {resp.text}")
            return

        token = cfg.get("telegram_bot_token", "")

        if not token or not effective_chat_id:
            logger.warning("Telegram enabled but bot_token or chat_id not set")
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": effective_chat_id, "text": message, "parse_mode": "HTML"}

        resp = await _get_client().post(url, json=payload)
        if resp.status_code == 200:
            logger.info("Telegram message sent successfully")
        else:
            logger.warning(f"Telegram API returned {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")
