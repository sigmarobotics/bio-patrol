"""IT-23: a scan round only processes a message it has not seen yet.

latest_data keeps the last payload until the paho thread overwrites it, so a
pad that publishes once and then goes quiet used to be re-saved every retry
round. _ingest bumps a sequence number per arrival; get_valid_scan_data only
handles a round when the sequence advanced and reports how many messages
arrived in the window (messages_seen) so the patrol can tell "pad silent"
from "pad talking but no valid reading".
"""
import asyncio
import sqlite3
import types

import services.bio_sensor_mqtt as bio_sensor_mqtt
from tests.unit.test_bio_sensor_scan_manual_default import (
    _deliver_during_window,
    _deliver_once_during_window,
    _make_client,
)

THREE_ROUNDS = {
    "bio_scan_wait_time": 0,
    "bio_scan_retry_count": 3,
    "bio_scan_initial_wait": 0,
    "bio_scan_valid_status": 4,
}

NOBODY = {"records": [{"status": 0, "bpm": 0, "rpm": 0}]}
RESTLESS = {"records": [{"status": 2, "bpm": 0, "rpm": 0}]}


def _three_round_client(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    import settings.config as config
    monkeypatch.setattr(config, "get_runtime_settings", lambda: THREE_ROUNDS)
    return client


def _script_sleeps(monkeypatch, client, payloads):
    """Sleep call i ingests payloads[i] (None = no message). Call 0 is the
    initial wait, calls 1..N-1 are the waits between retry rounds."""
    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def fake_sleep(_delay):
        i = calls["n"]
        calls["n"] += 1
        if i < len(payloads) and payloads[i] is not None:
            client._ingest(payloads[i])
        await real_sleep(0)

    monkeypatch.setattr(bio_sensor_mqtt, "asyncio", types.SimpleNamespace(sleep=fake_sleep))
    return calls


def _rows(client):
    conn = sqlite3.connect(client.db_path)
    rows = conn.execute(
        "SELECT retry_count, status FROM sensor_scan_data ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def test_same_message_across_rounds_is_stored_once(tmp_path, monkeypatch):
    client = _three_round_client(tmp_path, monkeypatch)
    _deliver_once_during_window(monkeypatch, client, NOBODY)

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert _rows(client) == [(0, 0)]
    assert outcome.valid_record is None
    assert outcome.messages_seen == 1
    # has_any_data stays True: the failure is "no valid reading", not "no data".
    assert outcome.last_failure_reason == "無有效量測數値"
    assert outcome.last_record_raw["status"] == 0


def test_new_message_is_stored_again(tmp_path, monkeypatch):
    client = _three_round_client(tmp_path, monkeypatch)
    _script_sleeps(monkeypatch, client, [NOBODY, None, RESTLESS])

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert _rows(client) == [(0, 0), (2, 2)]
    assert outcome.messages_seen == 2
    assert outcome.last_record_raw["status"] == 2


def test_messages_seen_counts_every_arrival(tmp_path, monkeypatch):
    client = _three_round_client(tmp_path, monkeypatch)
    _deliver_during_window(monkeypatch, client, NOBODY)  # one message per sleep

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert len(_rows(client)) == 3
    assert outcome.messages_seen == 3


def test_valid_hit_reports_messages_seen(tmp_path, monkeypatch):
    client = _three_round_client(tmp_path, monkeypatch)
    _script_sleeps(monkeypatch, client, [NOBODY, {"records": [{"status": 4, "bpm": 70, "rpm": 15}]}])

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert outcome.valid_record["bpm"] == 70
    assert outcome.retry_count == 1
    assert outcome.messages_seen == 2


def test_silent_window_reports_zero_messages(tmp_path, monkeypatch):
    client = _three_round_client(tmp_path, monkeypatch)
    client.latest_data = NOBODY  # stale, from before the window; no seq bump
    _script_sleeps(monkeypatch, client, [])

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert outcome.messages_seen == 0
    assert outcome.last_failure_reason == "未收到感測器資料（MQTT無連線或無數據）"


def test_empty_records_message_is_not_silence(tmp_path, monkeypatch):
    client = _three_round_client(tmp_path, monkeypatch)
    _deliver_once_during_window(monkeypatch, client, {"records": []})

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert outcome.messages_seen == 1


def test_ingest_order_sets_data_seq_and_timestamp(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    assert client._msg_seq == 0
    assert client.last_message_at is None

    client._ingest(NOBODY)

    assert client.latest_data is NOBODY
    assert client._msg_seq == 1
    assert client.last_message_at is not None


def test_on_message_goes_through_ingest(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    msg = types.SimpleNamespace(topic="t", payload=b'{"records": []}')

    client._on_message(None, None, msg)

    assert client.latest_data == {"records": []}
    assert client._msg_seq == 1


# ── wait_for_message ─────────────────────────────────────────────────────────

def test_wait_for_message_fresh_returns_true_without_sleeping(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client._ingest(NOBODY)
    calls = _script_sleeps(monkeypatch, client, [])

    assert asyncio.run(client.wait_for_message(60, 0)) is True
    assert calls["n"] == 0


def test_wait_for_message_stale_timestamp_waits(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client._ingest(NOBODY)
    client.last_message_at -= 120  # older than the 60 s window
    _script_sleeps(monkeypatch, client, [None, NOBODY])

    assert asyncio.run(client.wait_for_message(60, 5)) is True


def test_wait_for_message_arrival_during_wait(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    _script_sleeps(monkeypatch, client, [NOBODY])

    assert asyncio.run(client.wait_for_message(60, 5)) is True


def test_wait_for_message_times_out_in_half_second_steps(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    calls = _script_sleeps(monkeypatch, client, [])

    assert asyncio.run(client.wait_for_message(60, 2)) is False
    assert calls["n"] == 4


def test_zero_retry_rounds_still_count_messages(tmp_path, monkeypatch):
    """bio_scan_retry_count=0 (a blank UI field saves as 0) skips the round
    loop; a message during the initial wait must still count, or every bed
    looks silent and the patrol aborts blaming a healthy pad."""
    client = _make_client(tmp_path, monkeypatch)
    import settings.config as config
    monkeypatch.setattr(config, "get_runtime_settings",
                        lambda: {**THREE_ROUNDS, "bio_scan_retry_count": 0})
    _deliver_once_during_window(monkeypatch, client, NOBODY)

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert outcome.messages_seen == 1


def test_wait_for_message_nudges_a_disconnected_broker(tmp_path, monkeypatch):
    """The gate must not sit out paho's 30 s backoff inside its own 30 s wait:
    a disconnected client gets one reconnect() before polling, exactly like
    the scan path."""
    client = _make_client(tmp_path, monkeypatch)
    client.connected = False
    calls = []
    client.client.reconnect = lambda: calls.append("reconnect")

    async def fake_sleep(_delay):
        client._ingest({"records": []})

    monkeypatch.setattr(bio_sensor_mqtt, "asyncio", types.SimpleNamespace(sleep=fake_sleep))

    assert asyncio.run(client.wait_for_message(fresh_within_s=60, timeout_s=5)) is True
    assert calls == ["reconnect"]


def test_ingest_snapshot_is_atomic_for_readers(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch)
    client._ingest({"records": [{"status": 2}]})
    seq, payload = client._latest
    assert seq == client._msg_seq == 1
    assert payload is client.latest_data
