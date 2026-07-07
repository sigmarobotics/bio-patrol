"""LINE notification service.

Pushes text messages via the LINE Messaging API (push message). Both callers
(LineSink via the dispatcher, and the /settings/test-line endpoint) gate on
``enable_line`` before calling, so this module only needs the token.
Mirrors telegram_service: a single module-level ``httpx.AsyncClient`` is
reused across sends so connection pool + TLS handshake survive between
calls. Lifespan calls ``aclose_client`` on shutdown.
"""
import logging
from typing import Optional

import httpx

from settings.config import get_runtime_settings

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 10.0
_PUSH_URL = "https://api.line.me/v2/bot/message/push"
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


async def send_line_message(message: str, to: str) -> bool:
    """Push a text message to a LINE group/room/user if enabled in settings.

    to: groupId / roomId / userId captured by the webhook service.
    Returns True when the LINE API accepted the push.
    """
    try:
        cfg = get_runtime_settings()
        token = cfg.get("line_channel_access_token", "")
        if not token or not to:
            logger.warning("LINE push skipped: access token or target not set")
            return False

        payload = {"to": to, "messages": [{"type": "text", "text": message}]}
        resp = await _get_client().post(
            _PUSH_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 200:
            logger.info("LINE message sent successfully")
            return True
        logger.warning(f"LINE API returned {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        logger.error(f"Failed to send LINE message: {e}")
        return False
