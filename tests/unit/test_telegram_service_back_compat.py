"""Back-compat guard for the three legacy direct-Telegram callers in task_runtime."""
from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock

from services import telegram_service


def _patch_settings(token: str = "fake-token", user_id: str = "fake-user", enabled: bool = True):
    return patch(
        "services.telegram_service.get_runtime_settings",
        return_value={
            "enable_telegram": enabled,
            "telegram_bot_token": token,
            "telegram_user_id": user_id,
        },
    )


def test_legacy_no_kwarg_uses_settings_user_id():
    """Legacy call: send_telegram_message(message) — no kwarg — must hit telegram_user_id."""
    with _patch_settings(user_id="user-from-settings"), \
         patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_response = AsyncMock(); mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value.__aenter__.return_value = mock_client

        asyncio.run(telegram_service.send_telegram_message("hi"))

        assert mock_client.post.await_count == 1
        kwargs = mock_client.post.await_args.kwargs
        assert kwargs["json"]["chat_id"] == "user-from-settings"
        assert kwargs["json"]["text"] == "hi"


def test_explicit_chat_id_overrides_settings():
    with _patch_settings(user_id="user-from-settings"), \
         patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_response = AsyncMock(); mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_cls.return_value.__aenter__.return_value = mock_client

        asyncio.run(telegram_service.send_telegram_message("hi", chat_id="explicit-id"))

        kwargs = mock_client.post.await_args.kwargs
        assert kwargs["json"]["chat_id"] == "explicit-id"


def test_disabled_short_circuits():
    with _patch_settings(enabled=False), \
         patch("httpx.AsyncClient") as mock_cls:
        asyncio.run(telegram_service.send_telegram_message("hi"))
        mock_cls.assert_not_called()


def test_missing_token_or_chat_id_short_circuits():
    with _patch_settings(token="", user_id=""), \
         patch("httpx.AsyncClient") as mock_cls:
        asyncio.run(telegram_service.send_telegram_message("hi"))
        mock_cls.assert_not_called()
