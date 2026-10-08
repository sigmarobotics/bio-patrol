"""IT-21: synthetic demo readings + the 30-day synthetic history.

Abnormal values are derived from the thresholds in force, so whatever the site
configures, the abnormal seat always crosses them (and the absent seat always
reads as "nobody there" through the real failure evaluator).
"""
from __future__ import annotations

import random
import re
import sqlite3

import pytest

from services import demo_data
from services.bio_sensor_mqtt import is_valid_scan
from services.notifications.evaluator import (
    BioScanFailureEvaluator, ScanOutcome, VitalsOutOfBandEvaluator,
)
from settings.defaults import DEFAULT_SETTINGS


def _cfg(**overrides):
    return {**DEFAULT_SETTINGS, **overrides}


def _outcome(record, valid):
    return ScanOutcome(
        task_id="t", location_id="loc", bed_name="S1",
        valid_record=record if valid else None, retry_count=0,
        last_record_raw=record, last_failure_reason=None if valid else "x",
    )


@pytest.fixture
def demo_db(monkeypatch, tmp_path):
    path = str(tmp_path / "demo_data.db")
    monkeypatch.setattr(demo_data, "DB_PATH", path)
    return path


def test_normal_reading_stays_in_the_normal_band():
    cfg = _cfg()
    for seed in range(200):
        rec = demo_data.synth_record("normal", cfg, random.Random(seed))
        assert rec["status"] == 4
        assert 68 <= rec["bpm"] <= 88
        assert 14 <= rec["rpm"] <= 20
        assert is_valid_scan(rec, cfg["bio_scan_valid_status"])
        assert VitalsOutOfBandEvaluator().evaluate(_outcome(rec, True), cfg) is None


@pytest.mark.parametrize("thresholds", [
    {},                                                   # defaults 50/120, 10/30
    {"vitals_hr_high": 100, "vitals_rr_high": 24},
    {"vitals_hr_high": 160, "vitals_rr_high": 45},
    {"vitals_hr_low": 30, "vitals_hr_high": 70, "vitals_rr_low": 5, "vitals_rr_high": 12},
])
def test_abnormal_reading_crosses_whatever_thresholds_are_configured(thresholds):
    cfg = _cfg(**thresholds)
    for seed in range(100):
        rec = demo_data.synth_record("abnormal", cfg, random.Random(seed))
        assert rec["status"] == 4
        assert cfg["vitals_hr_high"] + 8 <= rec["bpm"] <= cfg["vitals_hr_high"] + 15
        assert cfg["vitals_rr_high"] + 2 <= rec["rpm"] <= cfg["vitals_rr_high"] + 5
        # A valid reading — it is the VITALS evaluator that must catch it.
        assert is_valid_scan(rec, cfg["bio_scan_valid_status"])
        assert VitalsOutOfBandEvaluator().evaluate(_outcome(rec, True), cfg) is not None


def test_absent_reading_is_status_zero_and_reads_as_nobody_there():
    cfg = _cfg()
    rec = demo_data.synth_record("absent", cfg, random.Random(1))
    assert rec == {"status": 0, "bpm": 0, "rpm": 0}
    assert not is_valid_scan(rec, cfg["bio_scan_valid_status"])
    event = BioScanFailureEvaluator().evaluate(_outcome(rec, False))
    assert "偵測不到人" in event.body


SEATS = [{"bed_key": f"S{i}", "location_id": f"loc_S{i}"} for i in (1, 2, 3)]


def _history_rows(path):
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT bed_name, timestamp, status, bpm, rpm, is_valid, task_id, location_id "
        "FROM demo_scan_data WHERE scenario = 'history' ORDER BY bed_name, timestamp",
    ).fetchall()
    conn.close()
    return rows


def test_history_is_30_daily_normal_readings_per_seat(demo_db):
    demo_data.ensure_history(SEATS, _cfg())
    rows = _history_rows(demo_db)
    for seat in SEATS:
        mine = [r for r in rows if r[0] == seat["bed_key"]]
        assert len(mine) == 30
        assert len({r[1][:10] for r in mine}) == 30          # one per day
        for _, _, status, bpm, rpm, valid, _, loc in mine:
            assert status == 4 and valid
            assert 68 <= bpm <= 88 and 14 <= rpm <= 20
            assert loc == seat["location_id"]               # drawer filters on it


def test_history_has_one_real_format_run_per_day(demo_db):
    """The history tab groups by task_id: each synthetic day must be its own
    run, stamped like a real one (YYYYMMDDHHMMSS-xxxxxx) at that day's 10:00,
    shared by every seat of the day."""
    demo_data.ensure_history(SEATS, _cfg())
    rows = _history_rows(demo_db)
    task_ids = {r[6] for r in rows}
    assert len(task_ids) == 30
    for tid in task_ids:
        assert re.fullmatch(r"\d{8}100000-[0-9a-f]{6}", tid)
        day_rows = [r for r in rows if r[6] == tid]
        assert sorted(r[0] for r in day_rows) == ["S1", "S2", "S3"]
        assert {r[1][:10].replace("-", "") for r in day_rows} == {tid[:8]}


def test_history_task_ids_are_deterministic_per_day(monkeypatch, tmp_path):
    a, b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    monkeypatch.setattr(demo_data, "DB_PATH", a)
    demo_data.ensure_history(SEATS[:1], _cfg())
    monkeypatch.setattr(demo_data, "DB_PATH", b)
    demo_data.ensure_history(SEATS[:2], _cfg())
    ids = lambda rows: {r[6] for r in rows}
    assert ids(_history_rows(a)) == ids(_history_rows(b))


def test_history_is_idempotent(demo_db):
    demo_data.ensure_history(SEATS[:2], _cfg())
    first = _history_rows(demo_db)
    demo_data.ensure_history(SEATS[:2], _cfg())
    assert _history_rows(demo_db) == first


def test_history_uses_a_fixed_seed(monkeypatch, tmp_path):
    a, b = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    monkeypatch.setattr(demo_data, "DB_PATH", a)
    demo_data.ensure_history(SEATS[:1], _cfg())
    monkeypatch.setattr(demo_data, "DB_PATH", b)
    demo_data.ensure_history(SEATS[:1], _cfg())
    strip = lambda rows: [(r[0], r[1][:10], r[3], r[4]) for r in rows]
    assert strip(_history_rows(a)) == strip(_history_rows(b))


def test_legacy_shared_history_id_is_replaced_by_per_day_runs(demo_db):
    """A demo DB written by IT-21 holds its history under one 'demo-history'
    task_id; it must not survive to show up as one giant run."""
    conn = sqlite3.connect(demo_db)
    conn.execute(
        "CREATE TABLE demo_scan_data (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL, location_id TEXT NOT NULL, bed_name TEXT NULL, "
        "timestamp TEXT NOT NULL, retry_count INTEGER NOT NULL, status INTEGER, "
        "bpm INTEGER, rpm INTEGER, data_json TEXT, is_valid BOOLEAN DEFAULT FALSE, "
        "details TEXT NULL, scenario TEXT NULL)"
    )
    conn.execute(
        "INSERT INTO demo_scan_data (task_id, location_id, bed_name, timestamp, "
        "retry_count, status, bpm, rpm, data_json, is_valid, details, scenario) "
        "VALUES ('demo-history', '', 'S1', ?, 0, 4, 70, 16, '{}', 1, '量測正常', 'history')",
        (_yesterday_10am(),),
    )
    conn.commit()
    conn.close()
    demo_data._initialised.discard(demo_db)

    demo_data.ensure_history(SEATS[:1], _cfg())
    rows = _history_rows(demo_db)
    assert "demo-history" not in {r[6] for r in rows}
    assert len(rows) == 30


def _yesterday_10am():
    from datetime import timedelta
    from common_types import get_now
    ts = get_now().replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=1)
    return ts.isoformat()


def test_history_is_inserted_run_by_run_like_real_patrols(demo_db):
    """The history tab reads scan-history ORDER BY id DESC LIMIT 500. Real
    runs insert in time order, so the limit drops the oldest runs; a seat-major
    back-fill would instead drop whole seats from every day."""
    demo_data.ensure_history(SEATS, _cfg())
    conn = sqlite3.connect(demo_db)
    ts = [r[0] for r in conn.execute("SELECT timestamp FROM demo_scan_data ORDER BY id")]
    conn.close()
    assert ts == sorted(ts)
