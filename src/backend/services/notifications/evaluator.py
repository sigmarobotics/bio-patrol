"""Scan outcome + evaluators that turn outcomes into AnomalyEvents.

IT-5 ships v1: a failed scan (no valid record after all retries) is a WARN.
Future iterations add VitalsOutOfBandEvaluator (status==4 but bpm/rpm out of band).
"""
from __future__ import annotations

from dataclasses import dataclass

from services.notifications.events import AnomalyEvent, Severity, Source


@dataclass
class ScanOutcome:
    """Replaces the legacy {task_id, data} return shape of get_valid_scan_data."""
    task_id: str
    location_id: str
    bed_name: str | None
    valid_record: dict | None      # None when retries exhausted without a valid hit
    retry_count: int
    last_record_raw: dict | None
    last_status: int | None
    last_bpm: int | None
    last_rpm: int | None
    last_failure_reason: str | None  # None only when valid_record is not None


class BioScanFailureEvaluator:
    """v1 rule: emit BIO_SCAN_FAILURE / WARN when retries exhaust without a valid hit."""

    def evaluate(self, outcome: ScanOutcome) -> AnomalyEvent | None:
        if outcome.valid_record is not None:
            return None
        bed = outcome.bed_name or outcome.location_id
        return AnomalyEvent(
            severity=Severity.WARN,
            source=Source.BIO_SCAN_FAILURE,
            bed_key=outcome.bed_name,
            task_id=outcome.task_id,
            title=f"⚠️ {bed} 量測失敗",
            body=(
                f"床位：{bed}\n"
                f"原因：{outcome.last_failure_reason}\n"
                f"重試次數：{outcome.retry_count}\n"
                f"最後一筆：status={outcome.last_status}, "
                f"bpm={outcome.last_bpm}, rpm={outcome.last_rpm}"
            ),
            raw=outcome.last_record_raw or {},
        )
