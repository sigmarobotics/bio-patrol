"""TODO-008: GET /api/settings must not hand out credentials in the clear.

Secrets go out as ••••<last 4>; the Settings page POSTs the whole form back, so
a field that still holds the mask is dropped instead of overwriting the real
value. Non-secret fields — including the mqtt_tls_* file paths — are untouched.
"""
import asyncio
import json

import pytest

import routers.settings as settings_router


@pytest.fixture
def settings_file(monkeypatch, tmp_path):
    """Point both the router and get_runtime_settings at a temp settings.json."""
    path = tmp_path / "settings.json"

    def _write(data: dict):
        path.write_text(json.dumps(data), encoding="utf-8")

    _write({
        "telegram_bot_token": "1234567890:AAbbccddeeffSECRET",
        "notify_hub_token": "hub_token_wxyz",
        "line_channel_access_token": "",
        "mqtt_tls_key": "wisleep-key/sigmabot.key",
    })
    monkeypatch.setattr(settings_router, "SETTINGS_FILE", str(path))
    monkeypatch.setattr("settings.config.SETTINGS_FILE", str(path))

    def _read() -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    _read.write = _write
    return _read


def _get():
    return asyncio.run(settings_router.get_settings())


def _post(body: dict):
    return asyncio.run(settings_router.save_settings(body))


def test_get_masks_configured_secrets(settings_file):
    data = _get()

    assert data["telegram_bot_token"] == "••••CRET"
    assert data["notify_hub_token"] == "••••wxyz"


def test_get_returns_empty_for_unset_secret(settings_file):
    """The UI needs "not configured" to stay distinguishable from "hidden"."""
    data = _get()

    assert data["line_channel_access_token"] == ""
    assert data["gemini_api_key"] == ""


def test_get_leaves_non_secrets_readable(settings_file):
    data = _get()

    assert data["mqtt_tls_key"] == "wisleep-key/sigmabot.key"
    assert data["mqtt_topic"] == "deviceData-qt/201906078"


def test_post_of_masked_value_keeps_the_stored_secret(settings_file):
    """A save from an untouched form echoes the mask back — it must not stick."""
    _post({"telegram_bot_token": "••••CRET", "notify_hub_token": "••••wxyz",
           "timezone": "Asia/Taipei"})

    saved = settings_file()
    assert saved["telegram_bot_token"] == "1234567890:AAbbccddeeffSECRET"
    assert saved["notify_hub_token"] == "hub_token_wxyz"
    assert saved["timezone"] == "Asia/Taipei"


def test_post_of_a_new_value_is_written(settings_file):
    _post({"telegram_bot_token": "9999999999:NEWTOKEN"})

    assert settings_file()["telegram_bot_token"] == "9999999999:NEWTOKEN"


def test_post_of_empty_value_clears_the_secret(settings_file):
    _post({"telegram_bot_token": ""})

    assert settings_file()["telegram_bot_token"] == ""


def test_post_of_a_new_secret_on_an_unset_field_is_written(settings_file):
    _post({"line_channel_access_token": "brand-new-line-token"})

    assert settings_file()["line_channel_access_token"] == "brand-new-line-token"


def test_post_response_is_masked_too(settings_file):
    res = _post({"telegram_bot_token": "9999999999:NEWTOKEN"})

    assert res["data"]["telegram_bot_token"] == "••••OKEN"
