"""TODO-015: one scan window must only accept data that arrived inside it.

latest_data is overwritten by the paho thread and was never cleared, so a
sensor that went silent left the previous bed's reading in place and the next
bed's window filed it as its own.
"""
import asyncio
import sqlite3

from tests.unit.test_bio_sensor_scan_manual_default import (  # noqa: F401  (reuse helpers)
    _deliver_during_window,
    _make_client,
)


def test_residual_data_from_previous_bed_is_not_attributed(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    # Left over from the previous bed's window; the sensor is now silent.
    client.latest_data = {"records": [{"status": 4, "bpm": 72, "rpm": 16}]}

    outcome = asyncio.run(
        client.get_valid_scan_data(target_bed="B_101-2", bed_name="101-2")
    )

    assert outcome.valid_record is None
    assert outcome.last_failure_reason == "未收到感測器資料（MQTT無連線或無數據）"
    assert client.latest_data is None
    conn = sqlite3.connect(client.db_path)
    rows = conn.execute(
        "SELECT bed_name, bpm, rpm, is_valid FROM sensor_scan_data"
    ).fetchall()
    conn.close()
    assert rows == [("101-2", None, None, 0)]


def test_data_arriving_inside_the_window_is_still_accepted(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.latest_data = {"records": [{"status": 4, "bpm": 99, "rpm": 99}]}  # stale
    _deliver_during_window(monkeypatch, client, {"records": [{"status": 4, "bpm": 72, "rpm": 16}]})

    outcome = asyncio.run(
        client.get_valid_scan_data(target_bed="B_101-2", bed_name="101-2")
    )

    assert outcome.valid_record["bpm"] == 72
    assert outcome.valid_record["rpm"] == 16
