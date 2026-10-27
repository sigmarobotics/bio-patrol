"""Logging gaps from the 2026-10-27 audit: notification / whiteboard / voice /
zigbee loggers reach app.log, and boot failures keep their traceback."""
from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main  # noqa: F401 — import runs _setup_logging()

ROUTED_TO_APP_LOG = [
    "services.notifications.dispatcher",
    "services.notifications.offline_debouncer",
    "services.notifications.mqtt",
    "services.line_service",
    "services.integration.board_publisher",
    "services.announcer",
    "lifespan_state",
    "services.zigbee_mqtt",
    "services.zigbee_snapshot",
    "services.button_db",
    "routers.buttons",
]


def _app_log_handler(name: str) -> RotatingFileHandler | None:
    lg = logging.getLogger(name)
    while lg is not None:
        for h in lg.handlers:
            if isinstance(h, RotatingFileHandler) and h.baseFilename.endswith("app.log"):
                return h
        if not lg.propagate:
            return None
        lg = lg.parent
    return None


@pytest.mark.parametrize("name", ROUTED_TO_APP_LOG)
def test_logger_reaches_app_log(name):
    assert _app_log_handler(name) is not None, f"{name} has no app.log file handler"


def test_record_reaches_app_file_handler():
    handler = _app_log_handler("services.notifications.dispatcher")
    seen = []
    with patch.object(handler, "emit", side_effect=seen.append):
        logging.getLogger("services.notifications.dispatcher").error("Sink x failed")
    assert [r.getMessage() for r in seen] == ["Sink x failed"]


def test_logger_exception_path_keeps_exc_info(caplog):
    with caplog.at_level(logging.ERROR, logger="bio_patrol.main"):
        try:
            raise FileNotFoundError(2, "No such file or directory")
        except FileNotFoundError:
            main.logger.exception("Failed to start bio-sensor MQTT client")
    rec = caplog.records[-1]
    assert rec.exc_info and rec.exc_info[0] is FileNotFoundError


def test_telegram_timeout_log_names_type_and_chat(caplog):
    import httpx
    from services import telegram_service

    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ConnectTimeout(""))
    cfg = {"enable_telegram": True, "telegram_bot_token": "SECRET-TOKEN", "telegram_user_id": "42"}
    with patch.object(telegram_service, "get_runtime_settings", return_value=cfg), \
         patch.object(telegram_service, "_get_client", return_value=client), \
         caplog.at_level(logging.ERROR, logger="services.telegram_service"):
        asyncio.run(telegram_service.send_telegram_message("hi"))
    msg = caplog.records[-1].getMessage()
    assert "ConnectTimeout" in msg and "42" in msg
    assert "SECRET-TOKEN" not in msg


def test_telegram_settings_failure_does_not_raise(caplog):
    from services import telegram_service

    with patch.object(telegram_service, "get_runtime_settings", side_effect=RuntimeError("boom")), \
         caplog.at_level(logging.ERROR, logger="services.telegram_service"):
        asyncio.run(telegram_service.send_telegram_message("hi"))
    assert "RuntimeError boom" in caplog.records[-1].getMessage()
