"""Unit tests for ad-hoc (task-less) get_valid_scan_data behavior.

Guards the NOT NULL regression fixed in PR #28: a no-arg call must tag
rows as location_id='manual' (bed_name stays None) and actually persist
them, and labeling must never mutate the shared MQTT-thread record dicts.
"""
import asyncio
import sqlite3

from services.bio_sensor_mqtt import BioSensorMQTTClient


FAST_SETTINGS = {
    "bio_scan_wait_time": 0,
    "bio_scan_retry_count": 1,
    "bio_scan_initial_wait": 0,
    "bio_scan_valid_status": 4,
}


def _make_client(tmp_path, monkeypatch):
    import settings.config as config
    monkeypatch.setattr(config, "get_runtime_settings", lambda: FAST_SETTINGS)
    client = BioSensorMQTTClient(db_path=str(tmp_path / "sensor_test.db"))
    client.connected = True  # skip the reconnect nudge
    return client


def test_no_arg_scan_defaults_to_manual_and_persists(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client.latest_data = {"records": [{"status": 4, "bpm": 72, "rpm": 16}]}

    outcome = asyncio.run(client.get_valid_scan_data())

    assert outcome.location_id == "manual"
    assert outcome.bed_name is None
    assert outcome.valid_record["location_id"] == "manual"
    conn = sqlite3.connect(client.db_path)
    rows = conn.execute(
        "SELECT location_id, bed_name, is_valid FROM sensor_scan_data"
    ).fetchall()
    conn.close()
    assert rows == [("manual", None, 1)]


def test_scan_labels_a_copy_not_the_shared_record(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    record = {"status": 4, "bpm": 72, "rpm": 16}
    client.latest_data = {"records": [record]}

    outcome = asyncio.run(
        client.get_valid_scan_data(target_bed="B_101-1", bed_name="101-1")
    )

    assert outcome.valid_record["location_id"] == "B_101-1"
    # The shared dict a concurrent scan would read must stay unlabeled.
    assert record == {"status": 4, "bpm": 72, "rpm": 16}
