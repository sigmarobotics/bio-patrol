"""Unit tests for the shared bio-scan validity rule."""
from __future__ import annotations

from services.bio_sensor_mqtt import is_valid_scan


VALID_STATUS = 4


def test_valid_record_is_accepted():
    record = {"status": 4, "bpm": 72, "rpm": 16}
    assert is_valid_scan(record, VALID_STATUS) is True


def test_wrong_status_is_rejected():
    record = {"status": 0, "bpm": 72, "rpm": 16}
    assert is_valid_scan(record, VALID_STATUS) is False


def test_zero_bpm_is_rejected():
    record = {"status": 4, "bpm": 0, "rpm": 16}
    assert is_valid_scan(record, VALID_STATUS) is False


def test_zero_rpm_is_rejected():
    record = {"status": 4, "bpm": 72, "rpm": 0}
    assert is_valid_scan(record, VALID_STATUS) is False


def test_missing_bpm_is_rejected():
    record = {"status": 4, "rpm": 16}
    assert is_valid_scan(record, VALID_STATUS) is False


def test_missing_rpm_is_rejected():
    record = {"status": 4, "bpm": 72}
    assert is_valid_scan(record, VALID_STATUS) is False


def test_none_bpm_is_rejected():
    record = {"status": 4, "bpm": None, "rpm": 16}
    assert is_valid_scan(record, VALID_STATUS) is False


def test_custom_valid_status():
    record = {"status": 7, "bpm": 72, "rpm": 16}
    assert is_valid_scan(record, valid_status=7) is True
    assert is_valid_scan(record, valid_status=4) is False
