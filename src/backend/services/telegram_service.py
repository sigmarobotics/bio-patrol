"""Telegram notification service.

Sends messages via Telegram Bot API when enabled in settings.
The optional ``chat_id`` parameter lets the new AnomalyDispatcher path target
a specific chat; legacy direct callers omit it and fall back to settings.

A single module-level ``httpx.AsyncClient`` is reused across calls so
connection pool + TLS handshake survive between sends. Lifespan calls
``aclose_client`` on shutdown.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from settings.config import get_runtime_settings

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 10.0
_client: Optional[httpx.AsyncClient] = None

# Outcome of the most recent send attempt, for GET /api/settings/telegram-status.
# Rebound (not mutated) on every attempt — read it as
# ``telegram_service.last_send_result``, never via ``from ... import``.
last_send_result: dict = {
    "ok": None, "status": None, "description": "", "at": None, "via": None,
}

# A wrong chat id, a revoked token or a bot nobody /started never heals by
# retrying, so these are errors; 5xx/429 are transient and stay warnings.
_PERMANENT_STATUSES = (400, 401, 403, 404)


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


def _redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _describe(resp) -> str:
    """Telegram's error ``description`` (or the hub's ``detail``), else raw text."""
    text = resp.text if isinstance(resp.text, str) else ""
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, dict):
        desc = body.get("description") or body.get("detail") or body.get("error")
        if desc:
            return str(desc)[:200]
    return text[:200]


def _record(ok: bool, status: int | None, description: str, via: str,
            *secrets: str) -> dict:
    global last_send_result
    last_send_result = {
        "ok": ok,
        "status": status,
        "description": _redact(description, *secrets),
        "at": datetime.now(timezone.utc).isoformat(),
        "via": via,
    }
    return last_send_result


def _record_response(resp, via: str, *secrets: str) -> dict:
    if resp.status_code == 200:
        return _record(True, 200, "OK", via)
    result = _record(False, resp.status_code, _describe(resp), via, *secrets)
    level = logging.ERROR if resp.status_code in _PERMANENT_STATUSES else logging.WARNING
    source = "Hub notify" if via == "hub" else "Telegram API"
    logger.log(level, "%s returned %s: %s", source, resp.status_code, result["description"])
    return result


async def send_telegram_message(message: str, chat_id: str | None = None,
                                bot_token: str | None = None) -> dict | None:
    """Send a Telegram message if enabled in runtime settings.

    chat_id: optional override; defaults to settings.telegram_user_id.
    bot_token: optional override (IT-21 demo bot) — sent straight to the
    Telegram API with this token, never through the hub (the hub owns only
    the real bot).
    When notify_hub_url + notify_hub_token are both set, the message is
    relayed through the cloud hub's /api/notify instead of calling the
    Telegram API directly — the hub owns the bot token.

    Returns the attempt's outcome (also kept in ``last_send_result``), or None
    when Telegram is disabled and nothing was attempted.
    """
    effective_chat_id = chat_id  # for the failure log if settings itself fails
    via = "direct"
    secrets: tuple[str, ...] = (bot_token or "",)
    try:
        cfg = get_runtime_settings()

        if not cfg.get("enable_telegram", False):
            logger.debug("Telegram notifications disabled")
            return None

        effective_chat_id = chat_id or cfg.get("telegram_user_id", "")

        hub_url = (cfg.get("notify_hub_url") or "").rstrip("/")
        hub_token = cfg.get("notify_hub_token") or ""
        secrets = (bot_token or "", hub_token, cfg.get("telegram_bot_token") or "")
        if hub_url and hub_token and not bot_token:
            via = "hub"
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
            return _record_response(resp, via, *secrets)

        token = bot_token or cfg.get("telegram_bot_token", "")

        if not token or not effective_chat_id:
            logger.warning("Telegram enabled but bot_token or chat_id not set")
            return _record(False, None, "bot_token or chat_id not set", via)

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": effective_chat_id, "text": message, "parse_mode": "HTML"}

        resp = await _get_client().post(url, json=payload)
        if resp.status_code == 200:
            logger.info("Telegram message sent successfully")
        return _record_response(resp, via, *secrets)
    except Exception as e:
        # httpx timeouts stringify to "" — name the type and recipient. No
        # exc_info/URL: the direct-Telegram URL embeds the bot token (TODO-047).
        description = _redact(f"{type(e).__name__} {e}", *secrets)
        logger.error(
            "Failed to send Telegram message to %s: %s",
            effective_chat_id or "<unset>", description,
        )
        return _record(False, None, description, via)
