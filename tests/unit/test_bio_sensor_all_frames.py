"""F-55 / BIZ-019: a scan round evaluates every frame since the last round.

The pad publishes every ~2.5 s but a round polls once per bio_scan_wait_time
(5–10 s), so sampling only _latest skipped most frames — including a valid
status-4 frame between two status-2 frames. _ingest now also keeps recent
frames in a bounded deque. The blind initial wait is still blind (only the
newest frame counts at round 0), the deque is cleared at window start, and
rows are saved only for the hit frame or the round's last frame.
"""
import asyncio
import types

import services.bio_sensor_mqtt as bio_sensor_mqtt
from tests.unit.test_bio_sensor_message_dedup import RESTLESS, _rows, _three_round_client

VALID = {"records": [{"status": 4, "bpm": 70, "rpm": 16}]}


def _script_frames(monkeypatch, client, per_sleep):
    """Sleep call i ingests every payload in per_sleep[i], in order. Call 0 is
    the blind initial wait, calls 1..N-1 are the waits between rounds."""
    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def fake_sleep(_delay):
        i = calls["n"]
        calls["n"] += 1
        for payload in per_sleep[i] if i < len(per_sleep) else []:
            client._ingest(payload)
        await real_sleep(0)

    monkeypatch.setattr(bio_sensor_mqtt, "asyncio", types.SimpleNamespace(sleep=fake_sleep))


def test_valid_frame_between_polls_is_found(tmp_path, monkeypatch):
    client = _three_round_client(tmp_path, monkeypatch)
    _script_frames(monkeypatch, client, [[RESTLESS], [RESTLESS, VALID, RESTLESS]])

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert outcome.valid_record["bpm"] == 70
    assert outcome.valid_record["rpm"] == 16
    assert outcome.retry_count == 1
    assert outcome.messages_seen == 4
    # The hit ends the round: the status-2 frame after it is neither
    # evaluated nor saved, so the bed's latest row is the valid one.
    assert _rows(client) == [(0, 2), (1, 4)]


def test_blind_wait_frames_are_not_back_filled(tmp_path, monkeypatch):
    client = _three_round_client(tmp_path, monkeypatch)
    _script_frames(monkeypatch, client, [[VALID, RESTLESS]])

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert outcome.valid_record is None
    assert _rows(client) == [(0, 2)]
    assert outcome.messages_seen == 2


def test_previous_bed_frame_is_cleared_at_window_start(tmp_path, monkeypatch):
    client = _three_round_client(tmp_path, monkeypatch)
    client._ingest(VALID)  # previous bed's window; the pad then goes silent
    _script_frames(monkeypatch, client, [])

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_2", bed_name="2"))

    assert outcome.valid_record is None
    assert outcome.messages_seen == 0
    assert outcome.last_failure_reason == "未收到感測器資料（MQTT無連線或無數據）"
    assert _rows(client) == [(3, None)]
    assert len(client._frames) == 0


def test_one_row_per_round_not_per_frame(tmp_path, monkeypatch):
    client = _three_round_client(tmp_path, monkeypatch)
    three = [RESTLESS, RESTLESS, RESTLESS]
    _script_frames(monkeypatch, client, [three, three, three])

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert outcome.valid_record is None
    assert outcome.messages_seen == 9
    assert _rows(client) == [(0, 2), (1, 2), (2, 2)]


def test_last_frame_of_round_is_saved_and_reported(tmp_path, monkeypatch):
    nobody = {"records": [{"status": 0, "bpm": 0, "rpm": 0}]}
    client = _three_round_client(tmp_path, monkeypatch)
    _script_frames(monkeypatch, client, [[nobody], [nobody, RESTLESS]])

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert _rows(client) == [(0, 0), (1, 2)]
    assert outcome.last_record_raw["status"] == 2
    assert outcome.last_failure_reason == "無有效量測數値"


def test_cleared_deque_still_sees_the_newest_frame(tmp_path, monkeypatch):
    """A concurrent scan's window start can clear _frames; _latest stays the
    floor so the round still evaluates the newest frame, as before F-55."""
    client = _three_round_client(tmp_path, monkeypatch)
    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def fake_sleep(_delay):
        if calls["n"] == 1:
            client._ingest(VALID)
            client._frames.clear()
        calls["n"] += 1
        await real_sleep(0)

    monkeypatch.setattr(bio_sensor_mqtt, "asyncio", types.SimpleNamespace(sleep=fake_sleep))

    outcome = asyncio.run(client.get_valid_scan_data(target_bed="B_1", bed_name="1"))

    assert outcome.valid_record["bpm"] == 70
    assert outcome.retry_count == 1
