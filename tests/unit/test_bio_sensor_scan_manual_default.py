"""Unit tests for ad-hoc (task-less) get_valid_scan_data behavior.

Guards the NOT NULL regression fixed in PR #28: a no-arg call must tag
rows as location_id='manual' (bed_name stays None) and actually persist
them, and labeling must never mutate the shared MQTT-thread record dicts.
TODO-024 adds the `manual-<ts>` task_id prefix that keeps ad-hoc runs out
of the patrol task_id namespace.
"""
import asyncio
import re
import sqlite3
import types

import services.bio_sensor_mqtt as bio_sensor_mqtt
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


def _deliver_during_window(monkeypatch, client, payload):
    """Land `payload` the way the paho thread does: inside the scan window.

    get_valid_scan_data clears latest_data at window start (TODO-015), so a
    value assigned before the call is deliberately dropped — tests that need
    a reading must publish it after the window opens, which is what hooking
    the module's sleep does.
    """
    real_sleep = asyncio.sleep

    async def fake_sleep(_delay):
        client.latest_data = payload
        await real_sleep(0)

    monkeypatch.setattr(bio_sensor_mqtt, "asyncio", types.SimpleNamespace(sleep=fake_sleep))


def test_no_arg_scan_defaults_to_manual_and_persists(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    _deliver_during_window(monkeypatch, client, {"records": [{"status": 4, "bpm": 72, "rpm": 16}]})

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
    _deliver_during_window(monkeypatch, client, {"records": [record]})

    outcome = asyncio.run(
        client.get_valid_scan_data(target_bed="B_101-1", bed_name="101-1")
    )

    assert outcome.valid_record["location_id"] == "B_101-1"
    # The shared dict a concurrent scan would read must stay unlabeled.
    assert record == {"status": 4, "bpm": 72, "rpm": 16}


# ── TODO-024: ad-hoc task_id namespace ───────────────────────────────────────

def test_task_less_scan_gets_manual_prefixed_task_id(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    _deliver_during_window(monkeypatch, client, {"records": [{"status": 4, "bpm": 72, "rpm": 16}]})

    outcome = asyncio.run(client.get_valid_scan_data())

    assert re.fullmatch(r"manual-\d{14}", outcome.task_id)
    conn = sqlite3.connect(client.db_path)
    stored = conn.execute("SELECT DISTINCT task_id FROM sensor_scan_data").fetchall()
    conn.close()
    assert stored == [(outcome.task_id,)]


def test_manual_task_id_never_matches_a_same_second_patrol_prefix(tmp_path, monkeypatch):
    """/scan-history filters with `task_id LIKE '<prefix>%'` — the two
    namespaces must stay disjoint even when stamped in the same second."""
    client = _make_client(tmp_path, monkeypatch)
    _deliver_during_window(monkeypatch, client, {"records": [{"status": 4, "bpm": 72, "rpm": 16}]})
    ts = "20260501100000"
    asyncio.run(client.get_valid_scan_data(
        task_id=f"{ts}-ab12cd", target_bed="B_101-1", bed_name="101-1"
    ))
    manual = asyncio.run(client.get_valid_scan_data(task_id=f"manual-{ts}"))

    conn = sqlite3.connect(client.db_path)
    by_patrol = conn.execute(
        "SELECT DISTINCT task_id FROM sensor_scan_data WHERE task_id LIKE ?", (f"{ts}%",)
    ).fetchall()
    by_manual = conn.execute(
        "SELECT DISTINCT task_id FROM sensor_scan_data WHERE task_id LIKE ?", ("manual-%",)
    ).fetchall()
    conn.close()
    assert by_patrol == [(f"{ts}-ab12cd",)]
    assert by_manual == [(manual.task_id,)]
