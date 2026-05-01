"""Unit tests for ScanOutcome + BioScanFailureEvaluator."""
from __future__ import annotations

from services.notifications.evaluator import ScanOutcome, BioScanFailureEvaluator
from services.notifications.events import Severity, Source


def _make_outcome(**overrides):
    base = dict(
        task_id="task-1",
        location_id="loc-101-1",
        bed_name="101-1",
        valid_record=None,
        retry_count=19,
        last_record_raw={"status": 2, "bpm": 0, "rpm": 0, "details": "無有效量測數值"},
        last_status=2,
        last_bpm=0,
        last_rpm=0,
        last_failure_reason="無有效量測數值",
    )
    base.update(overrides)
    return ScanOutcome(**base)


def test_evaluator_returns_none_when_valid_record_present():
    outcome = _make_outcome(valid_record={"status": 4, "bpm": 72, "rpm": 16})
    event = BioScanFailureEvaluator().evaluate(outcome)
    assert event is None


def test_evaluator_emits_warn_event_on_failure():
    outcome = _make_outcome()
    event = BioScanFailureEvaluator().evaluate(outcome)
    assert event is not None
    assert event.severity == Severity.WARN
    assert event.source == Source.BIO_SCAN_FAILURE
    assert event.bed_key == "101-1"
    assert event.task_id == "task-1"
    assert "101-1" in event.title
    assert "量測失敗" in event.title
    assert "status=2" in event.body
    assert "bpm=0" in event.body
    assert "rpm=0" in event.body
    assert "重試次數：19" in event.body
    assert event.raw == {"status": 2, "bpm": 0, "rpm": 0, "details": "無有效量測數值"}


def test_evaluator_uses_location_id_when_bed_name_missing():
    outcome = _make_outcome(bed_name=None)
    event = BioScanFailureEvaluator().evaluate(outcome)
    assert event is not None
    assert "loc-101-1" in event.title


def test_evaluator_no_data_path_uses_standing_failure_reason():
    outcome = _make_outcome(
        last_record_raw=None,
        last_status=None,
        last_bpm=None,
        last_rpm=None,
        last_failure_reason="未收到感測器資料（MQTT無連線或無數據）",
    )
    event = BioScanFailureEvaluator().evaluate(outcome)
    assert event is not None
    assert "未收到感測器資料" in event.body
    assert event.raw == {}
