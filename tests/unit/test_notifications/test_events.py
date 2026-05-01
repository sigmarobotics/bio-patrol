"""Unit tests for AnomalyEvent / Severity / Source."""
from __future__ import annotations

from services.notifications.events import AnomalyEvent, Severity, Source


def test_severity_enum_values():
    assert Severity.INFO.value == "info"
    assert Severity.WARN.value == "warn"
    assert Severity.CRITICAL.value == "critical"


def test_source_enum_values():
    assert Source.BIO_SCAN_FAILURE.value == "bio_scan_failure"
    assert Source.SHELF_DROP.value == "shelf_drop"
    assert Source.TASK_SUMMARY.value == "task_summary"
    assert Source.VITALS_OUT_OF_BAND.value == "vitals_out_of_band"


def test_anomaly_event_defaults_are_unique_and_well_formed():
    e1 = AnomalyEvent()
    e2 = AnomalyEvent()
    assert e1.event_id != e2.event_id  # uuid4
    assert len(e1.event_id) == 36  # canonical uuid string
    assert e1.severity == Severity.WARN
    assert e1.source == Source.BIO_SCAN_FAILURE
    assert e1.title == ""
    assert e1.body == ""
    assert e1.bed_key is None
    assert e1.task_id is None
    assert e1.raw == {}


def test_anomaly_event_explicit_fields_round_trip():
    e = AnomalyEvent(
        severity=Severity.CRITICAL,
        source=Source.SHELF_DROP,
        title="貨架掉落",
        body="床位 101-1",
        bed_key="101-1",
        task_id="task-abc",
        raw={"shelf_id": "S_04"},
    )
    assert e.severity == Severity.CRITICAL
    assert e.source == Source.SHELF_DROP
    assert e.bed_key == "101-1"
    assert e.task_id == "task-abc"
    assert e.raw["shelf_id"] == "S_04"
