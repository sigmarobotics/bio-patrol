"""Hub-relay path in telegram_service — when notify_hub_url/notify_hub_token
are configured, messages go to the hub's /api/notify with a bearer token and
the Telegram API is never called directly.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from services import telegram_service


def _patch_settings(**overrides):
    base = {
        "enable_telegram": True,
        "telegram_bot_token": "",
        "telegram_user_id": "111222333",
        "notify_hub_url": "https://hub.example.run.app",
        "notify_hub_token": "xvi_test",
    }
    base.update(overrides)
    return patch(
        "services.telegram_service.get_runtime_settings", return_value=base
    )


def _fake_client_with_response(status_code: int = 200):
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = ""
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=mock_response)
    return fake_client


@pytest.fixture(autouse=True)
def _reset_module_client():
    telegram_service._client = None
    yield
    telegram_service._client = None


def test_hub_configured_posts_to_hub_with_bearer():
    fake_client = _fake_client_with_response()
    with _patch_settings(), \
         patch("services.telegram_service._get_client", return_value=fake_client):
        asyncio.run(telegram_service.send_telegram_message("hi"))

    assert fake_client.post.await_count == 1
    args, kwargs = fake_client.post.await_args
    assert args[0] == "https://hub.example.run.app/api/notify"
    assert kwargs["headers"]["Authorization"] == "Bearer xvi_test"
    assert kwargs["json"]["text"] == "hi"
    assert kwargs["json"]["chat_id"] == "111222333"


def test_hub_url_trailing_slash_normalized():
    fake_client = _fake_client_with_response()
    with _patch_settings(notify_hub_url="https://hub.example.run.app/"), \
         patch("services.telegram_service._get_client", return_value=fake_client):
        asyncio.run(telegram_service.send_telegram_message("hi"))

    args, _ = fake_client.post.await_args
    assert args[0] == "https://hub.example.run.app/api/notify"


def test_hub_relay_omits_empty_chat_id():
    """No chat_id configured — hub falls back to its own TELEGRAM_CHAT_IDS."""
    fake_client = _fake_client_with_response()
    with _patch_settings(telegram_user_id=""), \
         patch("services.telegram_service._get_client", return_value=fake_client):
        asyncio.run(telegram_service.send_telegram_message("hi"))

    _, kwargs = fake_client.post.await_args
    assert "chat_id" not in kwargs["json"]


def test_hub_not_configured_falls_back_to_direct_telegram():
    fake_client = _fake_client_with_response()
    with _patch_settings(notify_hub_url="", notify_hub_token="",
                         telegram_bot_token="fake-token"), \
         patch("services.telegram_service._get_client", return_value=fake_client):
        asyncio.run(telegram_service.send_telegram_message("hi"))

    args, kwargs = fake_client.post.await_args
    assert args[0].startswith("https://api.telegram.org/bot")
    assert kwargs["json"]["chat_id"] == "111222333"


def test_hub_relay_disabled_short_circuits():
    fake_client = _fake_client_with_response()
    with _patch_settings(enable_telegram=False), \
         patch("services.telegram_service._get_client",
               return_value=fake_client) as mock_get:
        asyncio.run(telegram_service.send_telegram_message("hi"))
        mock_get.assert_not_called()


def test_hub_error_response_does_not_raise():
    fake_client = _fake_client_with_response(status_code=500)
    with _patch_settings(), \
         patch("services.telegram_service._get_client", return_value=fake_client):
        asyncio.run(telegram_service.send_telegram_message("hi"))
    assert fake_client.post.await_count == 1
