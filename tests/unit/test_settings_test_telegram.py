"""F-29: POST /api/settings/test-telegram sends through send_telegram_message
(so a hub-relay site tests the hub), reports the outcome, and
GET /api/settings/telegram-status exposes the last send result. The bot token
never appears in a response or a log line."""
import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

import routers.settings as settings_router
from services import telegram_service

TOKEN = "1234567890:AAbbccddeeffSECRETTOKEN"
HUB_TOKEN = "xvi_hub_SECRET"


@pytest.fixture
def settings_file(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"

    def _write(**values):
        base = {"enable_telegram": True, "telegram_bot_token": TOKEN,
                "telegram_user_id": "975045068"}
        base.update(values)
        path.write_text(json.dumps(base), encoding="utf-8")

    _write()
    monkeypatch.setattr(settings_router, "SETTINGS_FILE", str(path))
    monkeypatch.setattr("settings.config.SETTINGS_FILE", str(path))
    return _write


@pytest.fixture(autouse=True)
def _reset_last_result(monkeypatch):
    monkeypatch.setattr(telegram_service, "last_send_result", {
        "ok": None, "status": None, "description": "", "at": None, "via": None})


def _client(status: int = 200, text: str = '{"ok":true}', exc: Exception | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    client = MagicMock()
    client.post = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=resp)
    return client


def _test_send(client):
    with patch("services.telegram_service._get_client", return_value=client):
        return asyncio.run(settings_router.test_telegram())


def _status():
    return asyncio.run(settings_router.telegram_status())


def test_success_direct(settings_file):
    client = _client(200)

    result = _test_send(client)

    assert result == {"ok": True, "status": 200, "description": "OK", "via": "direct"}
    assert client.post.await_args.kwargs["json"]["chat_id"] == "975045068"


def test_chat_not_found_reports_telegram_description_and_logs_error(settings_file, caplog):
    client = _client(400, '{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}')

    with caplog.at_level(logging.WARNING, logger="services.telegram_service"):
        result = _test_send(client)

    assert result == {"ok": False, "status": 400,
                      "description": "Bad Request: chat not found", "via": "direct"}
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "chat not found" in errors[0].getMessage()


def test_5xx_stays_a_warning(settings_file, caplog):
    with caplog.at_level(logging.WARNING, logger="services.telegram_service"):
        result = _test_send(_client(502, "bad gateway"))

    assert result["ok"] is False and result["status"] == 502
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_hub_relay_path_is_what_gets_tested(settings_file):
    settings_file(telegram_bot_token="", notify_hub_url="https://hub.example",
                  notify_hub_token=HUB_TOKEN)
    client = _client(401, '{"detail":"bad token"}')

    result = _test_send(client)

    assert client.post.await_args.args[0] == "https://hub.example/api/notify"
    assert result == {"ok": False, "status": 401, "description": "bad token", "via": "hub"}


def test_exception_is_reported_without_the_token(settings_file, caplog):
    exc = httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/sendMessage")

    with caplog.at_level(logging.DEBUG, logger="services.telegram_service"):
        result = _test_send(_client(exc=exc))

    assert result["ok"] is False and result["status"] is None
    assert result["description"].startswith("ConnectError")
    assert TOKEN not in json.dumps(result)
    assert TOKEN not in json.dumps(_status())
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def test_missing_chat_id_is_reported(settings_file):
    settings_file(telegram_user_id="")

    result = _test_send(_client(200))

    assert result == {"ok": False, "status": None,
                      "description": "bot_token or chat_id not set", "via": "direct"}


def test_disabled_is_400(settings_file):
    settings_file(enable_telegram=False)

    with pytest.raises(HTTPException) as exc:
        _test_send(_client(200))
    assert exc.value.status_code == 400


def test_status_shape_before_and_after_a_send(settings_file):
    assert _status() == {"ok": None, "status": None, "description": "",
                         "at": None, "via": None}

    _test_send(_client(400, '{"ok":false,"description":"Bad Request: chat not found"}'))

    status = _status()
    assert set(status) == {"ok", "status", "description", "at", "via"}
    assert status["ok"] is False and status["status"] == 400
    assert status["description"] == "Bad Request: chat not found"
    assert status["via"] == "direct" and status["at"]
    assert TOKEN not in json.dumps(status)
