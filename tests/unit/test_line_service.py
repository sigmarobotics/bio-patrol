"""line_service send contract — mirrors CORNER-004's telegram outbound coverage.

send_line_message's bool return value drives /settings/test-line's ok/error
report, so the HTTP layer's contract (200→True, anything else→False, never
raises) is pinned here without a network.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from services import line_service


def _patch_settings(**overrides):
    base = {"line_channel_access_token": "fake-token"}
    base.update(overrides)
    return patch("services.line_service.get_runtime_settings", return_value=base)


def _fake_response(status_code=200, text="ok"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


def test_send_returns_true_on_200_and_posts_push_payload():
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_fake_response(200))
    with _patch_settings(), \
         patch("services.line_service._get_client", return_value=fake_client):
        assert asyncio.run(line_service.send_line_message("hi", to="Cgid")) is True
    args, kwargs = fake_client.post.call_args
    assert args[0] == "https://api.line.me/v2/bot/message/push"
    assert kwargs["json"] == {"to": "Cgid", "messages": [{"type": "text", "text": "hi"}]}
    assert kwargs["headers"]["Authorization"] == "Bearer fake-token"


def test_send_returns_false_on_non_200():
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_fake_response(400, "invalid to"))
    with _patch_settings(), \
         patch("services.line_service._get_client", return_value=fake_client):
        assert asyncio.run(line_service.send_line_message("hi", to="bad")) is False


def test_send_swallows_httpx_connect_error():
    """Network down — must return False, never raise into the patrol path."""
    fake_client = MagicMock()
    fake_client.post = AsyncMock(side_effect=httpx.ConnectError("nxdomain"))
    with _patch_settings(), \
         patch("services.line_service._get_client", return_value=fake_client):
        assert asyncio.run(line_service.send_line_message("hi", to="Cgid")) is False


def test_send_returns_false_without_token():
    with _patch_settings(line_channel_access_token=""):
        assert asyncio.run(line_service.send_line_message("hi", to="Cgid")) is False


def test_send_returns_false_without_target():
    with _patch_settings():
        assert asyncio.run(line_service.send_line_message("hi", to="")) is False
