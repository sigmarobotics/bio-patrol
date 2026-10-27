"""F-01: POST /api/settings coerces known keys to their default's type and
refuses the values that silently break a patrol night with 400."""
import asyncio
import json

import pytest
from fastapi import HTTPException

import routers.settings as settings_router


@pytest.fixture
def settings_file(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"robot_ip": "10.0.0.1:26400"}), encoding="utf-8")
    monkeypatch.setattr(settings_router, "SETTINGS_FILE", str(path))
    monkeypatch.setattr("settings.config.SETTINGS_FILE", str(path))

    def _read() -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    return _read


def _post(body: dict):
    return asyncio.run(settings_router.save_settings(body))


def _post_400(body: dict) -> str:
    with pytest.raises(HTTPException) as exc:
        _post(body)
    assert exc.value.status_code == 400
    return exc.value.detail


# ── coercion ────────────────────────────────────────────────────────────────

def test_numeric_string_is_saved_as_int(settings_file):
    _post({"bio_scan_retry_count": "5", "bio_scan_initial_wait": "120.0"})

    saved = settings_file()
    assert saved["bio_scan_retry_count"] == 5 and type(saved["bio_scan_retry_count"]) is int
    assert saved["bio_scan_initial_wait"] == 120 and type(saved["bio_scan_initial_wait"]) is int


def test_bool_strings_are_saved_as_bool(settings_file):
    _post({"enable_telegram": "true", "mqtt_enabled": "false"})

    saved = settings_file()
    assert saved["enable_telegram"] is True
    assert saved["mqtt_enabled"] is False


def test_float_key_accepts_int(settings_file):
    _post({"robot_retry_base_delay": 3})

    assert settings_file()["robot_retry_base_delay"] == 3.0


def test_number_for_string_key_is_saved_as_string(settings_file):
    _post({"telegram_user_id": 975045068})

    assert settings_file()["telegram_user_id"] == "975045068"


def test_list_and_dict_values_pass_through(settings_file):
    _post({"line_group_ids": ["Cabc"], "whiteboard_bed_id_map": {"101-1": "A01"}})

    saved = settings_file()
    assert saved["line_group_ids"] == ["Cabc"]
    assert saved["whiteboard_bed_id_map"] == {"101-1": "A01"}


@pytest.mark.parametrize("key,value", [
    ("bio_scan_retry_count", "abc"),
    ("bio_scan_retry_count", 19.5),   # range(19.5) raised TypeError mid-patrol
    ("bio_scan_retry_count", True),
    ("bio_scan_wait_time", None),
    ("robot_retry_base_delay", "nan"),
    ("enable_telegram", "maybe"),
    ("enable_telegram", 2),
    ("telegram_user_id", ["x"]),
])
def test_uncoercible_value_is_400_naming_the_key(settings_file, key, value):
    detail = _post_400({key: value})

    assert detail.startswith(f"{key}: cannot convert")
    assert key not in settings_file()


# ── unknown keys ────────────────────────────────────────────────────────────

def test_unknown_key_is_dropped_not_400(settings_file):
    _post({"no_such_setting": 1, "bio_scan_wait_time": 10})

    saved = settings_file()
    assert "no_such_setting" not in saved
    assert saved["bio_scan_wait_time"] == 10


# ── range checks ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body,key", [
    ({"bio_scan_retry_count": 0}, "bio_scan_retry_count"),
    ({"bio_scan_initial_wait": 59}, "bio_scan_initial_wait"),
    ({"bio_scan_initial_wait": 0}, "bio_scan_initial_wait"),
    ({"mqtt_port": 0}, "mqtt_port"),
    ({"mqtt_port": 65536}, "mqtt_port"),
    ({"vitals_hr_high": 0}, "vitals_hr_high"),
    ({"vitals_rr_low": -1}, "vitals_rr_low"),
    ({"vitals_hr_low": 120, "vitals_hr_high": 100}, "vitals_hr_low"),
    ({"vitals_rr_low": 20, "vitals_rr_high": 20}, "vitals_rr_low"),
    ({"robot_reboot_window": "3am"}, "robot_reboot_window"),
])
def test_out_of_range_is_400(settings_file, body, key):
    before = settings_file()

    detail = _post_400(body)

    assert detail.startswith(f"{key}: ")
    assert settings_file() == before


def test_one_sided_vitals_post_compares_with_stored_value(settings_file):
    _post({"vitals_hr_high": 100})

    detail = _post_400({"vitals_hr_low": 110})

    assert detail == "vitals_hr_low: must be lower than vitals_hr_high (100)"


def test_one_sided_vitals_post_compares_with_default(settings_file):
    # default vitals_rr_high is 30
    assert _post_400({"vitals_rr_low": 30}).startswith("vitals_rr_low: ")


def test_reboot_window_accepts_format_and_empty(settings_file):
    _post({"robot_reboot_window": "02:50-03:20"})
    assert settings_file()["robot_reboot_window"] == "02:50-03:20"
    _post({"robot_reboot_window": ""})
    assert settings_file()["robot_reboot_window"] == ""


# ── live sites must keep saving ─────────────────────────────────────────────

@pytest.mark.parametrize("wait,retry,initial", [
    (5, 5, 120),    # 板榮
    (10, 10, 120),  # 新民
    (5, 17, 60),    # 新營
])
def test_live_site_scan_values_are_accepted(settings_file, wait, retry, initial):
    _post({
        "bio_scan_wait_time": wait,
        "bio_scan_retry_count": retry,
        "bio_scan_initial_wait": initial,
        "vitals_hr_low": 50, "vitals_hr_high": 120,
        "vitals_rr_low": 10, "vitals_rr_high": 30,
        "mqtt_port": 8883,
    })

    saved = settings_file()
    assert (saved["bio_scan_wait_time"], saved["bio_scan_retry_count"],
            saved["bio_scan_initial_wait"]) == (wait, retry, initial)


def test_robot_ip_still_normalised(settings_file, monkeypatch):
    async def _fake_reregister(ip):
        return {"ok": True}
    monkeypatch.setattr(settings_router, "_reregister_robot", _fake_reregister)

    _post({"robot_ip": "10.0.0.2"})

    assert settings_file()["robot_ip"] == "10.0.0.2:26400"
