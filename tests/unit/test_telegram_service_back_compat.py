"""Back-compat guard for the three legacy direct-Telegram callers in task_runtime.

After the IT-7 PR-A refactor, telegram_service holds a module-level
``httpx.AsyncClient`` and exposes it via ``_get_client``. Tests patch that
helper to intercept the post call.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

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


def _fake_client_with_response(status_code: int = 200):
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = ""
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=mock_response)
    return fake_client


@pytest.fixture(autouse=True)
def _reset_module_client():
    """Each test starts with a fresh client slot so previous patches don't leak."""
    telegram_service._client = None
    yield
    telegram_service._client = None


def test_legacy_no_kwarg_uses_settings_user_id():
    """Legacy call: send_telegram_message(message) — no kwarg — must hit telegram_user_id."""
    fake_client = _fake_client_with_response()
    with _patch_settings(user_id="user-from-settings"), \
         patch("services.telegram_service._get_client", return_value=fake_client):
        asyncio.run(telegram_service.send_telegram_message("hi"))

    assert fake_client.post.await_count == 1
    kwargs = fake_client.post.await_args.kwargs
    assert kwargs["json"]["chat_id"] == "user-from-settings"
    assert kwargs["json"]["text"] == "hi"


def test_explicit_chat_id_overrides_settings():
    fake_client = _fake_client_with_response()
    with _patch_settings(user_id="user-from-settings"), \
         patch("services.telegram_service._get_client", return_value=fake_client):
        asyncio.run(telegram_service.send_telegram_message("hi", chat_id="explicit-id"))

    kwargs = fake_client.post.await_args.kwargs
    assert kwargs["json"]["chat_id"] == "explicit-id"


def test_disabled_short_circuits():
    fake_client = _fake_client_with_response()
    with _patch_settings(enabled=False), \
         patch("services.telegram_service._get_client", return_value=fake_client) as mock_get:
        asyncio.run(telegram_service.send_telegram_message("hi"))
        mock_get.assert_not_called()


def test_missing_token_or_chat_id_short_circuits():
    fake_client = _fake_client_with_response()
    with _patch_settings(token="", user_id=""), \
         patch("services.telegram_service._get_client", return_value=fake_client) as mock_get:
        asyncio.run(telegram_service.send_telegram_message("hi"))
        mock_get.assert_not_called()


def test_module_level_client_is_reused_across_calls():
    """Two consecutive sends should hit the same _get_client instance, not open a new one each time."""
    fake_client = _fake_client_with_response()
    with _patch_settings(), \
         patch("services.telegram_service._get_client", return_value=fake_client) as mock_get:
        asyncio.run(telegram_service.send_telegram_message("first"))
        asyncio.run(telegram_service.send_telegram_message("second"))

    assert mock_get.call_count == 2
    assert fake_client.post.await_count == 2


def test_get_client_returns_same_instance_until_closed():
    c1 = telegram_service._get_client()
    c2 = telegram_service._get_client()
    assert c1 is c2

    asyncio.run(telegram_service.aclose_client())

    c3 = telegram_service._get_client()
    assert c3 is not c1
